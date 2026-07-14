#!/usr/bin/env python3
"""Elastic EC2 worker provisioner for the Switchboard CO fleet (CO-3).

The provisioner is coordination-plane code: it watches durable ``co_fleet``
wake intents, creates one ephemeral launch-template version from a pinned CO-2
base version, launches diversified Spot capacity, and waits for the exact
``host/<instance-id>`` registration with ``allow_work=true``.  It never runs an
agent on the Plan VM and never accepts credential values in a wake or user-data
payload; workers resolve an SSM/Secrets Manager reference at boot.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import scripts.switchboard_path  # noqa: F401 - make src/switchboard importable
from adapters import switchboard_core as sb
from switchboard.domain.coordination.placement import order_wakes_fairly


SCHEMA = "switchboard.co_fleet_receipt.v1"
MANAGED_BY = "switchboard-co-fleet-v1"
PROJECT_TAG = "switchboard-co"
GUARDRAILS_PARAMETER = "/switchboard/co/guardrails"
LAUNCH_SWITCH_PARAMETER = "/switchboard/co/launch-enabled"
TERMINAL_RUNNER_STATES = {
    "completed", "failed", "cancelled", "expired", "lost", "killed", "exited", "stopped",
}
SAFE_SELECTOR = re.compile(r"^[A-Za-z0-9._:/@+\-]{1,160}$")


@dataclass(frozen=True)
class Pool:
    name: str
    launch_template_id: str
    launch_template_version: int
    max_instances: int
    instance_types: tuple[str, ...]
    capabilities: tuple[str, ...]
    max_sessions: int


@dataclass(frozen=True)
class Config:
    project: str
    region: str
    account_id: str
    idle_seconds: int
    drain_timeout_s: int
    registration_timeout_s: int
    poll_seconds: float
    state_path: Path
    lock_path: Path
    pools: dict[str, Pool]


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def load_config(env: dict[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    general = Pool(
        name="co-general",
        launch_template_id=env.get("CO_GENERAL_LT_ID", "lt-06e82fa3ce11f96a8"),
        launch_template_version=int(env.get("CO_GENERAL_LT_VERSION", "5")),
        max_instances=int(env.get("CO_GENERAL_MAX", "4")),
        instance_types=_csv(env.get(
            "CO_GENERAL_INSTANCE_TYPES", "c7i.2xlarge,c7i-flex.2xlarge,c6i.2xlarge")),
        capabilities=("co_fleet", "claude_code", "codex_cli"),
        max_sessions=int(env.get("CO_GENERAL_MAX_SESSIONS", "6")),
    )
    build = Pool(
        name="co-build",
        launch_template_id=env.get("CO_BUILD_LT_ID", "lt-04205361bf23f5fe6"),
        launch_template_version=int(env.get("CO_BUILD_LT_VERSION", "5")),
        max_instances=int(env.get("CO_BUILD_MAX", "2")),
        instance_types=_csv(env.get(
            "CO_BUILD_INSTANCE_TYPES", "c7i.4xlarge,c6i.4xlarge,m7i.4xlarge")),
        capabilities=("co_fleet", "claude_code", "codex_cli", "heavy_build", "native_build"),
        max_sessions=int(env.get("CO_BUILD_MAX_SESSIONS", "2")),
    )
    idle = int(env.get("CO_IDLE_SECONDS", "720"))
    if not 600 <= idle <= 900:
        raise ValueError("CO_IDLE_SECONDS must be between 600 and 900")
    drain_timeout = int(env.get("CO_DRAIN_TIMEOUT_SECONDS", "120"))
    if not 30 <= drain_timeout <= 300:
        raise ValueError("CO_DRAIN_TIMEOUT_SECONDS must be between 30 and 300")
    return Config(
        project=env.get("PM_PROJECT", "switchboard"),
        region=env.get("AWS_REGION", "us-east-1"),
        account_id=env.get("CO_AWS_ACCOUNT_ID", "584673484283"),
        idle_seconds=idle,
        drain_timeout_s=drain_timeout,
        registration_timeout_s=int(env.get("CO_REGISTRATION_TIMEOUT_SECONDS", "180")),
        poll_seconds=float(env.get("CO_POLL_SECONDS", "10")),
        state_path=Path(env.get("CO_STATE_PATH", "/var/lib/switchboard-co-fleet/state.json")),
        lock_path=Path(env.get("CO_LOCK_PATH", "/var/lib/switchboard-co-fleet/provisioner.lock")),
        pools={general.name: general, build.name: build},
    )


class AwsCli:
    """Small injectable AWS CLI adapter; no boto dependency is required on the Plan VM."""

    def __init__(self, region: str, timeout: float = 90):
        self.region = region
        self.timeout = timeout

    def call(self, service: str, operation: str, *args: str, region: bool = True) -> dict[str, Any]:
        cmd = ["aws", service, operation]
        if region:
            cmd += ["--region", self.region]
        cmd += list(args) + ["--output", "json"]
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=self.timeout)
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "aws command failed").strip()
            raise RuntimeError(f"aws {service} {operation}: {message[-3000:]}")
        return json.loads(proc.stdout or "{}")


class SwitchboardClient:
    def __init__(self, project: str):
        self.project = project

    def _get(self, path: str, **query: Any) -> dict[str, Any]:
        query = {"project": self.project, **query}
        return sb._http("GET", f"{path}?{urllib.parse.urlencode(query)}")

    def pending_wakes(self) -> list[dict[str, Any]]:
        result = self._get("/txp/v1/list_wake_intents", status="pending")
        return result.get("wake_intents") or result.get("wakes") or []

    def hosts(self, include_stale: bool = True) -> list[dict[str, Any]]:
        result = self._get("/ixp/v1/agent_hosts", include_stale=str(include_stale).lower())
        return result.get("hosts") or result.get("agent_hosts") or []

    def runners(self, host_id: str) -> list[dict[str, Any]]:
        result = self._get(
            "/ixp/v1/runner_sessions", host_id=host_id, include_stale="true")
        return result.get("sessions") or result.get("runner_sessions") or []

    def claimed_wakes(self, host_id: str) -> list[dict[str, Any]]:
        result = self._get("/txp/v1/list_wake_intents", status="claimed", host_id=host_id)
        return result.get("wake_intents") or result.get("wakes") or []

    def fail_wake(self, wake: dict[str, Any], reason: str, failure_class: str,
                  details: dict[str, Any] | None = None) -> dict[str, Any]:
        result = {
            "started": False,
            "schema": SCHEMA,
            "reason": reason,
            "failure_class": failure_class,
            "escalated": True,
            **(details or {}),
        }
        return sb._http("POST", "/txp/v1/complete_wake", {
            "project": self.project,
            "wake_id": wake.get("wake_id"),
            "result": result,
        })


def secret_reference(value: str) -> tuple[str, str]:
    """Return (provider, identifier) and reject raw or ambiguous credential input."""
    value = str(value or "").strip()
    if value.startswith("ssm:/") and SAFE_SELECTOR.fullmatch(value[4:]):
        return "ssm", value[4:]
    if value.startswith("secretsmanager:arn:") and SAFE_SELECTOR.fullmatch(value[15:]):
        return "secretsmanager", value[15:]
    raise ValueError("runtime_config_ref must be ssm:/path or secretsmanager:arn:...")


def validate_account_binding(wake: dict[str, Any]) -> dict[str, Any] | None:
    """Fail closed for requested BYOA affinity without leaking its identifiers."""
    policy = wake.get("policy") or {}
    binding = policy.get("account_binding")
    if not binding:
        if policy.get("account_binding_required"):
            raise ValueError("required account binding is missing")
        return None
    if not isinstance(binding, dict):
        raise ValueError("account binding must be an object")
    required = (
        "tenant_id", "user_id", "project", "provider", "provider_account_id",
        "credential_reference", "account_affinity_id",
    )
    missing = [key for key in required if not binding.get(key)]
    if missing:
        raise ValueError("account binding missing required fields")
    if binding.get("project") != (wake.get("project") or "switchboard"):
        raise ValueError("account binding project mismatch")
    if binding.get("task_id") != wake.get("task_id"):
        raise ValueError("account binding task mismatch")
    runtime_fields = (
        "claim_id", "work_session_id", "host_id", "runner_session_id",
        "credential_lease_id",
    )
    if any(binding.get(key) for key in runtime_fields):
        raise ValueError("dispatcher cannot pre-bind execution identifiers")
    affinity_source = {key: binding.get(key) for key in (
        "tenant_id", "user_id", "project", "provider", "provider_account_id",
        "credential_reference", "auth_lane",
    )}
    expected = hashlib.sha256(
        json.dumps(affinity_source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if not hmac.compare_digest(str(binding.get("account_affinity_id")), expected):
        raise ValueError("account binding affinity mismatch")
    return binding


def select_pool(wake: dict[str, Any], config: Config) -> Pool:
    selector = wake.get("selector") or {}
    requested = set(selector.get("capabilities") or [])
    explicit = str(selector.get("pool") or "")
    if explicit:
        if explicit not in config.pools:
            raise ValueError(f"unknown CO pool {explicit}")
        pool = config.pools[explicit]
    elif requested & {"heavy_build", "native_build", "large_build"}:
        pool = config.pools["co-build"]
    else:
        pool = config.pools["co-general"]
    missing = requested - set(pool.capabilities) - {"docs", "python", "github", "tests"}
    if missing:
        raise ValueError(f"pool {pool.name} lacks capabilities: {sorted(missing)}")
    return pool


def _safe_field(value: Any, name: str, required: bool = True) -> str:
    value = str(value or "").strip()
    if (required and not value) or (value and not SAFE_SELECTOR.fullmatch(value)):
        raise ValueError(f"unsafe or missing {name}")
    return value


def _tag_specs(existing: list[dict[str, Any]], tags: dict[str, str]) -> list[dict[str, Any]]:
    by_type = {spec.get("ResourceType"): dict(spec) for spec in existing or []}
    for resource_type in ("instance", "volume"):
        spec = by_type.setdefault(resource_type, {"ResourceType": resource_type, "Tags": []})
        merged = {tag["Key"]: tag["Value"] for tag in spec.get("Tags") or []}
        merged.update(tags)
        spec["Tags"] = [{"Key": key, "Value": value} for key, value in sorted(merged.items())]
    return list(by_type.values())


def render_user_data(base_user_data: str, wake: dict[str, Any], pool: Pool) -> str:
    """Replace the CO-2 placeholder with a reference-only runtime bootstrap."""
    selector = wake.get("selector") or {}
    policy = wake.get("policy") or {}
    binding = validate_account_binding(wake) or {}
    wake_id = _safe_field(wake.get("wake_id"), "wake_id")
    task_id = _safe_field(wake.get("task_id"), "task_id")
    runtime = _safe_field(selector.get("runtime"), "runtime")
    lane = _safe_field(selector.get("lane"), "lane")
    provider, identifier = secret_reference(policy.get("runtime_config_ref") or "")
    marker = "# CO-3 will create this SecureString"
    if marker not in base_user_data:
        raise ValueError("base launch-template user data lacks the CO-3 injection marker")
    prefix = base_user_data.split(marker, 1)[0].rstrip()
    # CO-2's serial 18-part S3 loop dominated cold starts.  The objects are
    # independent, so fetch them concurrently, concatenate in numeric order, and
    # retain a connectivity fsck before creating the worktree.
    serial_download = """for part_number in $(seq 0 $((MIRROR_PART_COUNT - 1))); do
  printf -v part_suffix '%03d' "$part_number"
  aws s3 cp "${MIRROR_PARTS_PREFIX}${part_suffix}" "/var/cache/switchboard-co/mirror.part-${part_suffix}" --only-show-errors
  cat "/var/cache/switchboard-co/mirror.part-${part_suffix}" >>/var/cache/switchboard-co/projectplanner.mirror.tar.gz
  rm -f "/var/cache/switchboard-co/mirror.part-${part_suffix}"
done"""
    parallel_download = """MIRROR_ARCHIVE="$(jq -r '.repositories["6th-Element-Labs/projectplanner"].mirror_archive // empty' /var/cache/switchboard-co/cache-manifest.json)"
MIRROR_SHA256="$(jq -r '.repositories["6th-Element-Labs/projectplanner"].mirror_sha256 // empty' /var/cache/switchboard-co/cache-manifest.json)"
download_mirror_part() {
  printf -v part_suffix '%03d' "$1"
  aws s3 cp "${MIRROR_PARTS_PREFIX}${part_suffix}" "/var/cache/switchboard-co/mirror.part-${part_suffix}" --only-show-errors
}
if [ -n "$MIRROR_ARCHIVE" ] && [ -n "$MIRROR_SHA256" ]; then
  aws s3 cp "$MIRROR_ARCHIVE" /var/cache/switchboard-co/projectplanner.mirror.tar.gz --only-show-errors
  printf '%s  %s\n' "$MIRROR_SHA256" /var/cache/switchboard-co/projectplanner.mirror.tar.gz | sha256sum -c -
else
  export -f download_mirror_part
  export MIRROR_PARTS_PREFIX
  seq 0 $((MIRROR_PART_COUNT - 1)) | xargs -P 8 -n 1 bash -c 'download_mirror_part "$1"' _
  for part_number in $(seq 0 $((MIRROR_PART_COUNT - 1))); do
    printf -v part_suffix '%03d' "$part_number"
    cat "/var/cache/switchboard-co/mirror.part-${part_suffix}" >>/var/cache/switchboard-co/projectplanner.mirror.tar.gz
    rm -f "/var/cache/switchboard-co/mirror.part-${part_suffix}"
  done
fi"""
    if serial_download not in prefix:
        raise ValueError("base launch-template user data lacks the expected serial mirror download")
    prefix = prefix.replace(serial_download, parallel_download, 1)
    prefix = prefix.replace(
        'git --git-dir="$REPO_MIRROR" fsck --full',
        'git --git-dir="$REPO_MIRROR" fsck --connectivity-only',
        1,
    )
    # CO-2 validates these binaries while producing the golden image. Repeating
    # the checks on every cold boot costs roughly 24 seconds (primarily Claude's
    # first invocation) without strengthening the immutable-image guarantee.
    image_validation = """HOME="$RUNTIME_HOME" CLAUDE_CONFIG_DIR="$CLAUDE_RUNTIME_HOME" claude --version
CODEX_HOME="$CODEX_RUNTIME_HOME" codex --version
HOME="$RUNTIME_HOME" gh --version
PYTHONPATH=/opt/projectplanner /opt/projectplanner/.venv/bin/python /opt/projectplanner/adapters/agent_host.py --help
/opt/projectplanner/.venv/bin/python /opt/projectplanner/adapters/codex/supervisor.py --help"""
    if image_validation not in prefix:
        raise ValueError("base launch-template user data lacks image validation contract")
    prefix = prefix.replace(image_validation, "", 1)
    # CloudWatch's configuration helper takes about 16 seconds. Keep telemetry,
    # but start it after the Agent Host so it is no longer on the registration
    # critical path.
    cloudwatch_start = "/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/switchboard-co.json"
    if cloudwatch_start not in prefix:
        raise ValueError("base launch-template user data lacks CloudWatch startup contract")
    prefix = prefix.replace(cloudwatch_start, "", 1)
    # The golden image owns dependencies and system services, while the signed S3
    # mirror owns the exact application revision. Run Agent Host from that checked,
    # detached worktree so a fleet roll-forward does not require mutating the image.
    source_line = re.compile(r"^SWITCHBOARD_CO_SOURCE_SHA=[0-9a-f]{40}$", re.MULTILINE)
    if len(source_line.findall(prefix)) != 1:
        raise ValueError("base launch-template user data lacks one pinned source revision")
    prefix = source_line.sub("SWITCHBOARD_CO_SOURCE_SHA=${SOURCE_SHA}", prefix, count=1)
    capabilities = ",".join(pool.capabilities)
    # shlex.quote is defense in depth after the strict selector/reference allowlist.
    values = {
        "ref": shlex.quote(f"{provider}:{identifier}"),
        "wake": shlex.quote(wake_id),
        "task": shlex.quote(task_id),
        "runtime": shlex.quote(runtime),
        "lane": shlex.quote(lane),
        "caps": shlex.quote(capabilities),
        "sessions": pool.max_sessions,
        "tenant": shlex.quote(str(binding.get("tenant_id") or "")),
        "provider": shlex.quote(str(binding.get("provider") or "")),
        "affinity": shlex.quote(str(binding.get("account_affinity_id") or "")),
    }
    extension = f"""
# CO-3 reference-only runtime binding. No secret value is present in this script.
CO_RUNTIME_CONFIG_REF={values['ref']}
CO_WAKE_ID={values['wake']}
CO_TASK_ID={values['task']}
case "$CO_RUNTIME_CONFIG_REF" in
  ssm:*)
    runtime_json="$(aws ssm get-parameter --region {shlex.quote(os.environ.get('AWS_REGION', 'us-east-1'))} --name "${{CO_RUNTIME_CONFIG_REF#ssm:}}" --with-decryption --query Parameter.Value --output text)"
    ;;
  secretsmanager:*)
    runtime_json="$(aws secretsmanager get-secret-value --region {shlex.quote(os.environ.get('AWS_REGION', 'us-east-1'))} --secret-id "${{CO_RUNTIME_CONFIG_REF#secretsmanager:}}" --query SecretString --output text)"
    ;;
  *) echo "invalid runtime config reference" >&2; exit 64 ;;
esac
python3 - "$runtime_json" <<'PY'
import json, shlex, sys
value = json.loads(sys.argv[1])
allowed = {{
    "PM_BASE", "PM_PROJECT", "PM_MCP_TOKEN", "PM_AGENT_WORK_MODULE",
    "PM_VERIFY_COMPLETION_PUSH", "PM_WORK_SESSION_TEST_CMD", "AWS_REGION",
    "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST",
}}
missing = [key for key in ("PM_MCP_TOKEN", "PM_AGENT_WORK_MODULE") if not value.get(key)]
if missing:
    raise SystemExit("runtime config missing required worker fields: " + ", ".join(missing))
forbidden = [key for key in (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "OPENAI_API_KEY",
    "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
) if value.get(key)]
if forbidden:
    raise SystemExit("personal-subscription fleet config contains forbidden fallback fields")
with open("/etc/switchboard-co/agent-host.env", "w", encoding="utf-8") as f:
    for key in sorted(allowed & value.keys()):
        f.write(f"{{key}}={{shlex.quote(str(value[key]))}}\\n")
PY
cat /etc/switchboard-co/pool.env >>/etc/switchboard-co/agent-host.env
cat >>/etc/switchboard-co/agent-host.env <<'EOF'
PM_RUNTIME={values['runtime']}
PM_HOST_LANES={values['lane']}
PM_HOST_CAPABILITIES={values['caps']}
PM_HOST_MAX_SESSIONS={values['sessions']}
PM_HOST_CLASS=ephemeral
PM_HOST_COST_CLASS=ephemeral_variable
PM_HOST_PROJECTS={shlex.quote(str(wake.get('project') or 'switchboard'))}
PM_HOST_TENANTS={values['tenant']}
PM_HOST_PROVIDERS={values['provider']}
PM_HOST_ACCOUNT_AFFINITIES={values['affinity']}
PM_HOST_SUPPORTS_CREDENTIAL_LEASES=1
PM_HOST_REPOSITORIES=6th-Element-Labs/projectplanner
PM_HOST_SESSION_POLICIES=code_strict
PM_HOST_ISOLATION=task_worktree
PM_WAKE_ID={values['wake']}
PM_TASK_ID={values['task']}
PM_AGENT_HOST_ALLOW_WORK=1
PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM=0
PM_CO_DRAIN_IMDS=1
PM_CO_DRAIN_REQUEST_PATH=/run/switchboard-co/drain-request.json
PM_CO_DRAIN_RECEIPT_PATH=/run/switchboard-co/drain-receipt.json
PM_PROVIDER_RUNTIME_ROOT=/var/lib/switchboard-co/provider-runtimes
EOF
printf 'PM_WORKSPACE_ROOT=%s\n' "${{WORKTREE%/*}}" >>/etc/switchboard-co/agent-host.env
chmod 0600 /etc/switchboard-co/agent-host.env
install -d -m 0770 -o switchboard -g switchboard /run/switchboard-co
install -d -m 0700 -o switchboard -g switchboard /var/lib/switchboard-co/provider-runtimes
install -d -m 0755 /etc/systemd/system/switchboard-co-agent-host.service.d
cat >/etc/systemd/system/switchboard-co-agent-host.service.d/10-co-fleet-worktree.conf <<EOF
[Service]
ExecStart=
ExecStart=/opt/projectplanner/.venv/bin/python ${{WORKTREE}}/adapters/agent_host.py --interval 10
Environment=PYTHONPATH=${{WORKTREE}}:${{WORKTREE}}/src
EOF
systemctl daemon-reload
systemctl enable --now switchboard-co-agent-host.service
{cloudwatch_start}
touch /var/lib/switchboard-co/runtime-ready
"""
    return prefix + "\n" + extension.lstrip()


def _tags(instance: dict[str, Any]) -> dict[str, str]:
    return {tag.get("Key"): tag.get("Value") for tag in instance.get("Tags") or []}


def _instance_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [instance for reservation in result.get("Reservations") or []
            for instance in reservation.get("Instances") or []]


def list_managed_instances(aws: AwsCli, include_stopped: bool = False) -> list[dict[str, Any]]:
    states = "pending,running,stopping" + (",stopped" if include_stopped else "")
    result = aws.call(
        "ec2", "describe-instances",
        "--filters", f"Name=tag:Project,Values={PROJECT_TAG}",
        f"Name=tag:CO:ManagedBy,Values={MANAGED_BY}",
        f"Name=instance-state-name,Values={states}",
    )
    return _instance_rows(result)


def capacity_snapshot(aws: AwsCli) -> dict[str, Any]:
    instances = list_managed_instances(aws)
    by_pool: dict[str, int] = {}
    for instance in instances:
        pool = _tags(instance).get("CO:Pool") or "unknown"
        by_pool[pool] = by_pool.get(pool, 0) + 1
    return {"total": len(instances), "by_pool": by_pool,
            "instance_ids": [item.get("InstanceId") for item in instances]}


def read_guardrails(aws: AwsCli) -> dict[str, Any]:
    result = aws.call(
        "ssm", "get-parameter", "--name", GUARDRAILS_PARAMETER,
        "--query", "Parameter.Value",
    )
    value = result
    # With --output json and a scalar --query AWS returns the scalar JSON value.
    if isinstance(result, str):
        value = json.loads(result)
    if not isinstance(value, dict) or value.get("schema") != "switchboard.co_fleet_guardrails.v1":
        raise RuntimeError("invalid CO guardrails parameter")
    return value


def launch_switch_enabled(aws: AwsCli) -> bool:
    result = aws.call(
        "ssm", "get-parameter", "--name", LAUNCH_SWITCH_PARAMETER,
        "--query", "Parameter.Value",
    )
    value = result
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value.strip().lower() in {"1", "true", "yes", "enabled"}
    if isinstance(value, dict):
        return bool(value.get("enabled"))
    return bool(value)


def budgets_allow_launch(aws: AwsCli, config: Config, guardrails: dict[str, Any]) -> dict[str, Any]:
    checks = []
    for cadence in ("monthly", "daily_kill_switch"):
        expected = (guardrails.get("budgets") or {}).get(cadence) or {}
        name = expected.get("name")
        if not name:
            raise RuntimeError(f"guardrails missing {cadence} budget")
        result = aws.call(
            "budgets", "describe-budget", "--account-id", config.account_id,
            "--budget-name", name, region=False,
        )
        budget = result.get("Budget") or {}
        actual = float((((budget.get("CalculatedSpend") or {}).get("ActualSpend") or {}).get("Amount")) or 0)
        limit = float(((budget.get("BudgetLimit") or {}).get("Amount")) or 0)
        if not limit or actual >= limit:
            raise RuntimeError(f"budget {name} blocks launch: actual={actual} limit={limit}")
        checks.append({"name": name, "actual_usd": actual, "limit_usd": limit})
    return {"allowed": True, "checks": checks}


def enforce_capacity(aws: AwsCli, config: Config, pool: Pool,
                     guardrails: dict[str, Any]) -> dict[str, Any]:
    snapshot = capacity_snapshot(aws)
    pool_limit = int((((guardrails.get("pools") or {}).get(pool.name) or {}).get("max_instances")) or 0)
    total_limit = int(guardrails.get("total_max_instances") or 0)
    configured_limit = pool.max_instances
    if not pool_limit or configured_limit != pool_limit:
        raise RuntimeError(
            f"pool cap mismatch for {pool.name}: config={configured_limit} guardrail={pool_limit}")
    if snapshot["by_pool"].get(pool.name, 0) >= pool_limit:
        raise RuntimeError(f"capacity exhausted for {pool.name}: cap={pool_limit}")
    if not total_limit or snapshot["total"] >= total_limit:
        raise RuntimeError(f"total CO capacity exhausted: cap={total_limit}")
    return snapshot


def default_subnets(aws: AwsCli) -> list[str]:
    result = aws.call(
        "ec2", "describe-subnets", "--filters",
        "Name=default-for-az,Values=true", "Name=state,Values=available",
    )
    rows = sorted(result.get("Subnets") or [], key=lambda row: row.get("AvailabilityZone") or "")
    subnets = [row.get("SubnetId") for row in rows if row.get("SubnetId")]
    if len(subnets) < 2:
        raise RuntimeError("diversified Spot requires at least two available subnets/AZs")
    return subnets


def create_launch_version(aws: AwsCli, wake: dict[str, Any], pool: Pool) -> int:
    described = aws.call(
        "ec2", "describe-launch-template-versions",
        "--launch-template-id", pool.launch_template_id,
        "--versions", str(pool.launch_template_version),
    )
    versions = described.get("LaunchTemplateVersions") or []
    if len(versions) != 1:
        raise RuntimeError("pinned launch-template version not found")
    data = dict(versions[0].get("LaunchTemplateData") or {})
    encoded = data.get("UserData") or ""
    try:
        base_user_data = base64.b64decode(encoded).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("base launch-template user data is invalid") from exc
    user_data = render_user_data(base_user_data, wake, pool)
    selector = wake.get("selector") or {}
    ref_hash = hashlib.sha256(
        str((wake.get("policy") or {}).get("runtime_config_ref") or "").encode()).hexdigest()[:16]
    tags = {
        "Project": PROJECT_TAG,
        "SwitchboardTask": str(wake.get("task_id")),
        "CO:Pool": pool.name,
        "CO:ManagedBy": MANAGED_BY,
        "CO:WakeId": str(wake.get("wake_id")),
        "CO:Runtime": str(selector.get("runtime")),
        "CO:Lane": str(selector.get("lane")),
        "CO:ConfigRefHash": ref_hash,
        "CO:BaseLTVersion": str(pool.launch_template_version),
    }
    data["UserData"] = base64.b64encode(user_data.encode()).decode()
    data["TagSpecifications"] = _tag_specs(data.get("TagSpecifications") or [], tags)
    created = aws.call(
        "ec2", "create-launch-template-version",
        "--launch-template-id", pool.launch_template_id,
        "--source-version", str(pool.launch_template_version),
        "--version-description", f"CO-3 {wake.get('wake_id')} {wake.get('task_id')}",
        "--launch-template-data", json.dumps(data, separators=(",", ":")),
    )
    version = int(((created.get("LaunchTemplateVersion") or {}).get("VersionNumber")) or 0)
    if version <= pool.launch_template_version:
        raise RuntimeError("AWS did not create a derived launch-template version")
    return version


def _fleet_instances(result: dict[str, Any]) -> list[str]:
    return [instance_id for group in result.get("Instances") or []
            for instance_id in group.get("InstanceIds") or []]


def launch_capacity(aws: AwsCli, pool: Pool, version: int, subnets: list[str],
                    on_demand: bool = False) -> dict[str, Any]:
    overrides = [{"SubnetId": subnet, "InstanceType": instance_type}
                 for subnet in subnets for instance_type in pool.instance_types]
    configs = [{
        "LaunchTemplateSpecification": {
            "LaunchTemplateId": pool.launch_template_id,
            "Version": str(version),
        },
        "Overrides": overrides,
    }]
    target_type = "on-demand" if on_demand else "spot"
    target = {
        "TotalTargetCapacity": 1,
        "DefaultTargetCapacityType": target_type,
        "OnDemandTargetCapacity": 1 if on_demand else 0,
        "SpotTargetCapacity": 0 if on_demand else 1,
    }
    args = [
        "--type", "instant",
        "--target-capacity-specification", json.dumps(target, separators=(",", ":")),
        "--launch-template-configs", json.dumps(configs, separators=(",", ":")),
    ]
    if on_demand:
        args += ["--on-demand-options", '{"AllocationStrategy":"lowest-price"}']
    else:
        args += ["--spot-options", '{"AllocationStrategy":"capacity-optimized-prioritized"}']
    result = aws.call("ec2", "create-fleet", *args)
    ids = _fleet_instances(result)
    if not ids:
        errors = result.get("Errors") or []
        raise RuntimeError(f"{target_type} capacity unavailable: {json.dumps(errors)[:2000]}")
    return {"instance_id": ids[0], "fleet_id": result.get("FleetId"),
            "capacity_type": target_type, "provider_result": result}


def _runtime_ready(host: dict[str, Any], wake: dict[str, Any]) -> bool:
    if host.get("stale") or host.get("status") not in (None, "online"):
        return False
    selector = wake.get("selector") or {}
    required = set(selector.get("capabilities") or [])
    for runtime in host.get("runtimes") or []:
        policy = runtime.get("policy") or {}
        if runtime.get("runtime") != selector.get("runtime") or not policy.get("allow_work"):
            continue
        if selector.get("lane") not in set(runtime.get("lanes") or []):
            continue
        if not required.issubset(set(runtime.get("capabilities") or [])):
            continue
        return True
    return False


def wait_for_registration(client: SwitchboardClient, instance_id: str,
                          wake: dict[str, Any], timeout_s: int,
                          sleep: Callable[[float], None] = time.sleep,
                          monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    host_id = f"host/{instance_id}"
    deadline = monotonic() + timeout_s
    last_read_error = ""
    while monotonic() < deadline:
        try:
            hosts = client.hosts(include_stale=True)
        except Exception as exc:
            retryable = isinstance(exc, (TimeoutError, ConnectionError, OSError))
            if isinstance(exc, urllib.error.HTTPError):
                retryable = exc.code in (408, 425, 429) or 500 <= exc.code < 600
            elif isinstance(exc, urllib.error.URLError):
                retryable = True
            if not retryable:
                raise
            last_read_error = f"{type(exc).__name__}: {str(exc)[-240:]}"
            hosts = []
        for host in hosts:
            if host.get("host_id") == host_id and _runtime_ready(host, wake):
                return host
        sleep(min(5, max(0.1, deadline - monotonic())))
    suffix = f"; last control-plane read error: {last_read_error}" if last_read_error else ""
    raise RuntimeError(f"registration timeout for {host_id}{suffix}")


def provision_wake(aws: AwsCli, client: SwitchboardClient, config: Config,
                   wake: dict[str, Any]) -> dict[str, Any]:
    started_at = time.time()
    binding = validate_account_binding(wake)
    pool = select_pool(wake, config)
    guardrails = read_guardrails(aws)
    if not launch_switch_enabled(aws):
        raise RuntimeError("CO real-time launch switch is disabled")
    budget = budgets_allow_launch(aws, config, guardrails)
    capacity = enforce_capacity(aws, config, pool, guardrails)
    existing = [item for item in list_managed_instances(aws)
                if _tags(item).get("CO:WakeId") == wake.get("wake_id")]
    if existing:
        instance_id = existing[0]["InstanceId"]
        launch = {"instance_id": instance_id, "fleet_id": None,
                  "capacity_type": _tags(existing[0]).get("CO:CapacityType") or "unknown",
                  "deduplicated": True}
        version = int(_tags(existing[0]).get("CO:DerivedLTVersion") or 0)
    else:
        version = create_launch_version(aws, wake, pool)
        subnets = default_subnets(aws)
        try:
            launch = launch_capacity(aws, pool, version, subnets, on_demand=False)
        except RuntimeError as spot_error:
            if not bool((wake.get("policy") or {}).get("allow_on_demand")):
                raise RuntimeError(f"spot_capacity_failure: {spot_error}") from spot_error
            launch = launch_capacity(aws, pool, version, subnets, on_demand=True)
            launch["spot_error"] = str(spot_error)
        instance_id = launch["instance_id"]
        aws.call(
            "ec2", "create-tags", "--resources", instance_id, "--tags",
            f"Key=CO:DerivedLTVersion,Value={version}",
            f"Key=CO:CapacityType,Value={launch['capacity_type']}",
        )
    timeout_s = int((wake.get("policy") or {}).get("registration_timeout_s")
                    or config.registration_timeout_s)
    try:
        host = wait_for_registration(client, instance_id, wake, timeout_s)
    except Exception:
        # A boot that cannot register is not useful capacity.  Tear it down now
        # instead of waiting for the ordinary 10-15 minute idle policy.
        aws.call("ec2", "terminate-instances", "--instance-ids", instance_id)
        raise
    registered_at = time.time()
    wake_requested_at = float(wake.get("requested_at") or started_at)
    elapsed = registered_at - wake_requested_at
    aws.call(
        "ec2", "create-tags", "--resources", instance_id, "--tags",
        f"Key=CO:RegisteredAt,Value={int(registered_at)}",
        f"Key=CO:WakeToRegisterSeconds,Value={elapsed:.3f}",
    )
    receipt = {
        "schema": SCHEMA,
        "wake_id": wake.get("wake_id"),
        "task_id": wake.get("task_id"),
        "pool": pool.name,
        "runtime": (wake.get("selector") or {}).get("runtime"),
        "host_id": host.get("host_id"),
        "instance_id": instance_id,
        "capacity_type": launch.get("capacity_type"),
        "fleet_id": launch.get("fleet_id"),
        "base_launch_template_version": pool.launch_template_version,
        "derived_launch_template_version": version,
        "wake_to_register_seconds": round(elapsed, 3),
        "registered_allow_work": True,
        "budget": budget,
        "capacity_before_launch": capacity,
    }
    if binding:
        # Preserve account affinity for audit without exposing the provider account,
        # credential reference, or credential lease identifiers in receipts/logs.
        receipt["account_binding"] = {
            "schema": binding.get("schema"),
            "tenant_id": binding.get("tenant_id"),
            "user_id": binding.get("user_id"),
            "project": binding.get("project"),
            "provider": binding.get("provider"),
            "task_id": binding.get("task_id"),
            "work_session_id": binding.get("work_session_id"),
            "auth_lane": binding.get("auth_lane"),
            "account_affinity_id": binding.get("account_affinity_id"),
            "credential_identifiers_redacted": True,
        }
    return receipt


def process_once(aws: AwsCli, client: SwitchboardClient, config: Config) -> list[dict[str, Any]]:
    outcomes = []
    for wake in order_wakes_fairly(client.pending_wakes()):
        if (wake.get("policy") or {}).get("mode") != "co_fleet":
            continue
        placement = wake.get("placement") or {}
        if (placement.get("scheduler_mode") == "hybrid"
                and placement.get("action") in {"assign_persistent", "assign_ephemeral"}):
            outcomes.append({
                "ok": True,
                "wake_id": wake.get("wake_id"),
                "task_id": wake.get("task_id"),
                "action": "defer_to_registered_host",
                "selected_host_id": placement.get("selected_host_id"),
                "reason_code": placement.get("reason_code"),
                "cost_class": placement.get("cost_class"),
            })
            continue
        if (placement.get("scheduler_mode") == "hybrid"
                and placement.get("action") != "provision_ephemeral"):
            if placement.get("action") in {"wait", "wait_for_credential_rebind"}:
                outcomes.append({
                    "ok": True, "wake_id": wake.get("wake_id"),
                    "task_id": wake.get("task_id"), "action": "defer_placement",
                    "reason_code": placement.get("reason_code"),
                })
                continue
            reason = str(placement.get("reason_code") or "hybrid_placement_blocked")
            details = {
                "action": placement.get("action") or "wait",
                "cost_class": placement.get("cost_class"),
            }
            try:
                client.fail_wake(wake, reason, "failed_gate", details)
            except Exception as report_exc:
                details["report_error"] = str(report_exc)
            outcomes.append({
                "ok": False, "wake_id": wake.get("wake_id"),
                "task_id": wake.get("task_id"), "failure_class": "failed_gate",
                "reason": reason, **details,
            })
            continue
        try:
            receipt = provision_wake(aws, client, config, wake)
            outcomes.append({"ok": True, **receipt})
        except Exception as exc:
            reason = str(exc)
            failure_class = "capacity_unavailable" if "capacity" in reason else "failed_gate"
            details = {"pool": None, "error": reason, "failed_at": time.time()}
            try:
                client.fail_wake(wake, reason, failure_class, details)
            except Exception as report_exc:
                details["report_error"] = str(report_exc)
            outcomes.append({"ok": False, "wake_id": wake.get("wake_id"),
                             "task_id": wake.get("task_id"),
                             "failure_class": failure_class, **details})
    return outcomes


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _parse_aws_time(value: str) -> float:
    if not value:
        return 0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _host_has_active_work(client: SwitchboardClient, host: dict[str, Any] | None,
                          host_id: str, now: float) -> tuple[bool, dict[str, Any]]:
    capacity = (host or {}).get("capacity") or {}
    runners = client.runners(host_id)
    claimed_wakes = client.claimed_wakes(host_id)
    active_runners = []
    active_claims = []
    for runner in runners:
        status = str(runner.get("status") or "").lower()
        runner_active = status not in TERMINAL_RUNNER_STATES and not runner.get("stale")
        if runner_active:
            active_runners.append(runner.get("runner_session_id"))
        claim = runner.get("claim") or {}
        if (runner_active and claim.get("status") == "active"
                and float(claim.get("expires_at") or 0) > now):
            active_claims.append(claim.get("claim_id") or runner.get("claim_id"))
    detail = {
        "reported_active_sessions": int(capacity.get("active_sessions") or 0),
        "active_runner_sessions": [item for item in active_runners if item],
        "active_claims": [item for item in active_claims if item],
        "claimed_wakes": [item.get("wake_id") for item in claimed_wakes],
    }
    return bool(detail["reported_active_sessions"] or active_runners
                or active_claims or claimed_wakes), detail


def request_host_drain(aws: AwsCli, instance_id: str,
                       request: dict[str, Any]) -> dict[str, Any]:
    """Write the fixed-schema drain marker through SSM without credential material."""
    payload = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.b64encode(payload).decode()
    command = (
        "install -d -m 0770 -o switchboard -g switchboard /run/switchboard-co && "
        f"printf %s {shlex.quote(encoded)} | base64 -d "
        ">/run/switchboard-co/drain-request.json.tmp && "
        "chown switchboard:switchboard /run/switchboard-co/drain-request.json.tmp && "
        "chmod 0600 /run/switchboard-co/drain-request.json.tmp && "
        "mv /run/switchboard-co/drain-request.json.tmp "
        "/run/switchboard-co/drain-request.json"
    )
    result = aws.call(
        "ssm", "send-command",
        "--instance-ids", instance_id,
        "--document-name", "AWS-RunShellScript",
        "--comment", f"Switchboard CO drain {request['request_id']}",
        "--parameters", json.dumps({"commands": [command]}, separators=(",", ":")),
    )
    return {
        "request_id": request["request_id"],
        "requested_at": request["requested_at"],
        "deadline": request["deadline"],
        "reason": request["reason"],
        "command_id": (result.get("Command") or {}).get("CommandId"),
    }


def _host_drain_receipt(host: dict[str, Any] | None,
                        request_id: str) -> dict[str, Any] | None:
    receipt = (((host or {}).get("capacity") or {}).get("drain_receipt") or {})
    if receipt.get("request_id") != request_id:
        return None
    return receipt


def _terminate(aws: AwsCli, instance_id: str) -> None:
    aws.call("ec2", "terminate-instances", "--instance-ids", instance_id)


def scale_in_once(aws: AwsCli, client: SwitchboardClient, config: Config,
                  now: float | None = None) -> list[dict[str, Any]]:
    """Drain idle workers before termination; force only after the bounded deadline."""
    now = time.time() if now is None else now
    state = _load_state(config.state_path)
    idle_since = state.setdefault("idle_since", {})
    drains = state.setdefault("drains", {})
    hosts = {host.get("host_id"): host for host in client.hosts(include_stale=True)}
    outcomes = []
    seen = set()
    for instance in list_managed_instances(aws):
        if (instance.get("State") or {}).get("Name") != "running":
            continue
        instance_id = instance.get("InstanceId")
        seen.add(instance_id)
        host_id = f"host/{instance_id}"
        draining = drains.get(instance_id)
        if draining:
            receipt = _host_drain_receipt(hosts.get(host_id), draining["request_id"])
            if receipt and receipt.get("status") == "drained":
                # Re-read all live control-plane state immediately before termination.
                final_hosts = {host.get("host_id"): host
                               for host in client.hosts(include_stale=True)}
                active, detail = _host_has_active_work(
                    client, final_hosts.get(host_id), host_id, now)
                if active:
                    outcomes.append({"instance_id": instance_id,
                                     "action": "wait_drain_race", **detail})
                    continue
                _terminate(aws, instance_id)
                idle_since.pop(instance_id, None)
                drains.pop(instance_id, None)
                outcomes.append({
                    "instance_id": instance_id,
                    "action": "terminate_drained",
                    "request_id": draining["request_id"],
                    "durable_acknowledged": True,
                    **detail,
                })
                continue
            if now >= float(draining.get("deadline") or 0):
                _terminate(aws, instance_id)
                idle_since.pop(instance_id, None)
                drains.pop(instance_id, None)
                outcomes.append({
                    "instance_id": instance_id,
                    "action": "terminate_forced_timeout",
                    "request_id": draining["request_id"],
                    "durable_acknowledged": False,
                    "drain_status": (receipt or {}).get("status") or "unacknowledged",
                })
                continue
            outcomes.append({
                "instance_id": instance_id,
                "action": "wait_drain",
                "request_id": draining["request_id"],
                "deadline": draining["deadline"],
                "drain_status": (receipt or {}).get("status") or "pending",
            })
            continue
        active, detail = _host_has_active_work(client, hosts.get(host_id), host_id, now)
        if active:
            idle_since.pop(instance_id, None)
            outcomes.append({"instance_id": instance_id, "action": "keep_active", **detail})
            continue
        first_idle = float(idle_since.setdefault(instance_id, now))
        launch_time = _parse_aws_time(instance.get("LaunchTime") or "")
        idle_age = now - first_idle
        instance_age = now - launch_time if launch_time else 0
        if idle_age < config.idle_seconds or instance_age < config.idle_seconds:
            outcomes.append({"instance_id": instance_id, "action": "keep_idle",
                             "idle_seconds": round(idle_age, 1), **detail})
            continue
        # Final read immediately before the destructive call closes the poll race.
        active, detail = _host_has_active_work(client, hosts.get(host_id), host_id, now)
        if active:
            idle_since.pop(instance_id, None)
            outcomes.append({"instance_id": instance_id, "action": "keep_race", **detail})
            continue
        request = {
            "schema": "switchboard.co_drain.request.v1",
            "request_id": "drain-" + uuid.uuid4().hex[:16],
            "reason": "planned_scale_in",
            "termination_kind": "ephemeral_instance",
            "requested_at": now,
            "deadline": now + config.drain_timeout_s,
        }
        drains[instance_id] = request_host_drain(aws, instance_id, request)
        outcomes.append({
            "instance_id": instance_id,
            "action": "request_drain",
            "request_id": request["request_id"],
            "deadline": request["deadline"],
            "idle_seconds": round(idle_age, 1),
            **detail,
        })
    for stale_id in set(idle_since) - seen:
        idle_since.pop(stale_id, None)
        drains.pop(stale_id, None)
    state["updated_at"] = now
    _save_state(config.state_path, state)
    return outcomes


def run_iteration(aws: AwsCli, client: SwitchboardClient, config: Config) -> dict[str, Any]:
    provision = process_once(aws, client, config)
    try:
        scale_in = scale_in_once(aws, client, config)
    except Exception as exc:
        # Provision receipts must survive a transient scale-in read failure (and
        # vice versa); the daemon retries both surfaces on its next iteration.
        scale_in = [{"ok": False, "failure_class": "broken_connection", "error": str(exc)}]
    return {
        "schema": SCHEMA,
        "provision": provision,
        "scale_in": scale_in,
        "at": time.time(),
    }


def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Switchboard CO Fleet provisioner")
    parser.add_argument("command", choices=("run-once", "daemon", "scale-in-once", "inspect"))
    args = parser.parse_args(argv)
    config = load_config()
    aws = AwsCli(config.region)
    client = SwitchboardClient(config.project)
    try:
        lock = _locked(config.lock_path)
    except BlockingIOError:
        print(json.dumps({"error": "another CO Fleet provisioner holds the lock"}))
        return 75
    with lock:
        if args.command == "inspect":
            print(json.dumps({"capacity": capacity_snapshot(aws),
                              "config": {name: pool.__dict__ for name, pool in config.pools.items()}},
                             indent=2, sort_keys=True, default=list))
            return 0
        if args.command == "scale-in-once":
            print(json.dumps(scale_in_once(aws, client, config), indent=2, sort_keys=True))
            return 0
        if args.command == "run-once":
            print(json.dumps(run_iteration(aws, client, config), indent=2, sort_keys=True))
            return 0
        while True:
            try:
                print(json.dumps(run_iteration(aws, client, config), sort_keys=True), flush=True)
            except Exception as exc:
                print(json.dumps({"error": str(exc), "at": time.time()}), flush=True)
            time.sleep(config.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
