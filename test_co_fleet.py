#!/usr/bin/env python3
"""Executable CO-3 tests: policy, secret boundary, launch shape, and scale-in safety."""
import base64
import json
import os
import tempfile
import time
from pathlib import Path


TMP = Path(tempfile.mkdtemp(prefix="co-fleet-test-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_AUTH_MODE"] = "off"

import co_fleet  # noqa: E402
import dispatch  # noqa: E402
import store  # noqa: E402
from adapters import agent_host  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


config = co_fleet.load_config({
    "CO_IDLE_SECONDS": "600",
    "CO_STATE_PATH": str(TMP / "state.json"),
    "CO_LOCK_PATH": str(TMP / "lock"),
})
ok(config.pools["co-general"].launch_template_version == 5,
   "launch configuration is pinned to an explicit base LT version")
rollback = co_fleet.load_config({
    "CO_IDLE_SECONDS": "900", "CO_GENERAL_LT_VERSION": "4",
    "CO_STATE_PATH": str(TMP / "state-rollback.json"),
    "CO_LOCK_PATH": str(TMP / "lock-rollback"),
})
ok(rollback.pools["co-general"].launch_template_version == 4,
   "an operator can roll back by selecting an older LT version")
try:
    co_fleet.load_config({"CO_IDLE_SECONDS": "599"})
    invalid_idle = False
except ValueError:
    invalid_idle = True
ok(invalid_idle, "idle termination policy cannot be set below 10 minutes")


base_script = """#!/usr/bin/env bash
set -euo pipefail
echo immutable-base
MIRROR_PARTS_PREFIX=s3://example/repo.part-
MIRROR_PART_COUNT=2
: >/var/cache/switchboard-co/projectplanner.mirror.tar.gz
for part_number in $(seq 0 $((MIRROR_PART_COUNT - 1))); do
  printf -v part_suffix '%03d' "$part_number"
  aws s3 cp "${MIRROR_PARTS_PREFIX}${part_suffix}" "/var/cache/switchboard-co/mirror.part-${part_suffix}" --only-show-errors
  cat "/var/cache/switchboard-co/mirror.part-${part_suffix}" >>/var/cache/switchboard-co/projectplanner.mirror.tar.gz
  rm -f "/var/cache/switchboard-co/mirror.part-${part_suffix}"
done
git --git-dir="$REPO_MIRROR" fsck --full
HOME="$RUNTIME_HOME" CLAUDE_CONFIG_DIR="$CLAUDE_RUNTIME_HOME" claude --version
CODEX_HOME="$CODEX_RUNTIME_HOME" codex --version
HOME="$RUNTIME_HOME" gh --version
PYTHONPATH=/opt/projectplanner /opt/projectplanner/.venv/bin/python /opt/projectplanner/adapters/agent_host.py --help
/opt/projectplanner/.venv/bin/python /opt/projectplanner/adapters/codex/supervisor.py --help
test "$(sudo -u switchboard git -C "$WORKTREE" rev-parse HEAD)" = "$SOURCE_SHA"
SWITCHBOARD_CO_SOURCE_SHA=d38563e6d9df76e56f0426e96b011c7f3c6bbd62
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/switchboard-co.json
# CO-3 will create this SecureString only after its provisioner exists.
if runtime_json=old; then echo old; fi
touch /var/lib/switchboard-co/runtime-ready
"""
wake = {
    "wake_id": "wake-0123456789abcdef",
    "task_id": "CO-3",
    "selector": {
        "runtime": "claude-code", "lane": "CO",
        "capabilities": ["co_fleet"],
    },
    "policy": {
        "mode": "co_fleet",
        "runtime_config_ref": "ssm:/switchboard/co/runtime/co-3",
    },
}
general = config.pools["co-general"]
rendered = co_fleet.render_user_data(base_script, wake, general)
ok("ssm:/switchboard/co/runtime/co-3" in rendered,
   "worker user data contains the secret reference")
ok("PM_TASK_ID=CO-3" in rendered and "PM_RUNTIME=claude-code" in rendered
   and "PM_HOST_LANES=CO" in rendered,
   "worker user data binds the task/runtime/lane selector")
ok("PM_AGENT_HOST_ALLOW_WORK=1" in rendered
   and "PM_HOST_CAPABILITIES=co_fleet,claude_code,codex_cli" in rendered,
   "worker registers as work-capable with its host-owned pool capabilities")
ok("runtime_json=old" not in rendered,
   "CO-3 replaces the fixed CO-2 runtime-config placeholder")
ok("mirror_archive" in rendered and "sha256sum -c -" in rendered
   and "xargs -P 8" in rendered and "fsck --connectivity-only" in rendered,
   "cold bootstrap prefers one checksummed archive with a parallel-parts fallback")
ok("claude --version" not in rendered and "codex --version" not in rendered
   and "agent_host.py --help" not in rendered,
   "per-wake bootstrap relies on golden-image validation instead of repeating it")
ok(rendered.index("systemctl enable --now switchboard-co-agent-host.service")
   < rendered.index("amazon-cloudwatch-agent-ctl -a fetch-config"),
   "telemetry startup no longer blocks exact worker registration")
ok('test "$(sudo -u switchboard git -C "$WORKTREE" rev-parse HEAD)" = "$SOURCE_SHA"' in rendered,
   "per-wake bootstrap still verifies the exact checked-out source revision")
ok("SWITCHBOARD_CO_SOURCE_SHA=${SOURCE_SHA}" in rendered
   and "${WORKTREE}/adapters/agent_host.py --interval 10" in rendered,
   "Agent Host executes the exact checked mirror revision, not stale image code")
ok("super-secret-value" not in rendered,
   "no credential value appears in user data")
embedded_python = rendered.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
try:
    compile(embedded_python, "<co-fleet-user-data>", "exec")
    embedded_compiles = True
except SyntaxError:
    embedded_compiles = False
ok(embedded_compiles, "embedded runtime-config parser is syntactically valid Python")
try:
    co_fleet.secret_reference("sk-raw-credential")
    raw_allowed = True
except ValueError:
    raw_allowed = False
ok(not raw_allowed, "raw credential input is rejected")


build_wake = json.loads(json.dumps(wake))
build_wake["selector"]["capabilities"].append("heavy_build")
ok(co_fleet.select_pool(build_wake, config).name == "co-build",
   "heavy-build capability selects co-build")
impossible = json.loads(json.dumps(wake))
impossible["selector"]["capabilities"].append("gpu")
try:
    co_fleet.select_pool(impossible, config)
    unsupported_allowed = True
except ValueError:
    unsupported_allowed = False
ok(not unsupported_allowed, "unsupported capability fails closed")


os.environ["PM_HOST_CAPABILITIES"] = "co_fleet,claude_code"
os.environ["PM_HOST_LANES"] = "CO"
os.environ["PM_AGENT_HOST_ALLOW_WORK"] = "1"
os.environ["PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM"] = "0"
inventory = agent_host.default_inventory()
runtime_inventory = inventory["runtimes"][0]
ok({"co_fleet", "claude_code"}.issubset(set(runtime_inventory["capabilities"])),
   "Agent Host advertises configured fleet capabilities")
os.environ["PM_WAKE_ID"] = "wake-bound"
filtered_wakes = agent_host.wakes_bound_to_host([
    {"wake_id": "wake-other"}, {"wake_id": "wake-bound"},
])
ok([wake["wake_id"] for wake in filtered_wakes] == ["wake-bound"],
   "ephemeral Agent Host can claim only the exact wake that launched it")
os.environ.pop("PM_WAKE_ID", None)
os.environ.pop("PM_HOST_CAPABILITIES", None)
os.environ.pop("PM_HOST_LANES", None)
os.environ.pop("PM_AGENT_HOST_ALLOW_WORK", None)
os.environ.pop("PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM", None)


store.init_db("switchboard")
task = store.create_task({
    "workstream_id": "CO", "workstream_name": "CO", "title": "Fleet proof", "phase": "Build",
}, actor="test", project="switchboard")
tid = task["task_id"]
raw_dispatch = dispatch.dispatch_to_co_fleet(
    tid, project="switchboard", runtime_config_ref="actual-token-value")
ok(not raw_dispatch.get("dispatched") and raw_dispatch.get("error") == "runtime_config_ref required",
   "dispatch refuses a raw runtime credential")
queued = dispatch.dispatch_to_co_fleet(
    tid, project="switchboard", runtime_config_ref="ssm:/switchboard/co/runtime/test")
ok(queued.get("dispatched") and queued.get("execution_mode") == "co_fleet",
   "CO dispatch creates an elastic-fleet wake")
queued_wake = next(item for item in store.list_wake_intents(project="switchboard")
                   if item.get("wake_id") == queued.get("wake_id"))
ok((queued_wake.get("policy") or {}).get("runtime_config_ref")
   == "ssm:/switchboard/co/runtime/test",
   "wake stores only the opaque runtime-config reference")

binding_task = store.create_task({
    "workstream_id": "CO", "workstream_name": "CO", "title": "BYOA fleet proof", "phase": "Build",
}, actor="test", project="switchboard")
incomplete_binding = dispatch.dispatch_to_co_fleet(
    binding_task["task_id"], project="switchboard",
    runtime_config_ref="ssm:/switchboard/co/runtime/test",
    account_binding={"tenant_id": "tenant-1"})
ok(not incomplete_binding.get("dispatched")
   and incomplete_binding.get("error") == "invalid_account_binding",
   "BYOA dispatch fails closed when required account affinity is incomplete")
binding = {
    "tenant_id": "tenant-1", "user_id": "user-1", "provider": "anthropic",
    "provider_account_id": "account-1", "credential_reference": "vault:credential-1",
    "credential_lease_id": "lease-1", "auth_lane": "personal-plan",
    "work_session_id": "worksession-1",
}
bound_dispatch = dispatch.dispatch_to_co_fleet(
    binding_task["task_id"], project="switchboard",
    runtime_config_ref="ssm:/switchboard/co/runtime/test", account_binding=binding)
bound_wake = next(item for item in store.list_wake_intents(project="switchboard")
                  if item.get("wake_id") == bound_dispatch.get("wake_id"))
stored_binding = (bound_wake.get("policy") or {}).get("account_binding") or {}
ok(bound_dispatch.get("dispatched") and stored_binding.get("provider_account_id") == "account-1"
   and stored_binding.get("credential_reference") == "vault:credential-1"
   and stored_binding.get("host_id") is None and stored_binding.get("runner_session_id") is None,
   "durable wake preserves non-secret BYOA affinity for later host/runner binding")
ok(co_fleet.validate_account_binding(bound_wake) == stored_binding,
   "provisioner verifies the task/project/account affinity before launch")
tampered_wake = json.loads(json.dumps(bound_wake))
tampered_wake["policy"]["account_binding"]["provider_account_id"] = "substituted-account"
try:
    co_fleet.validate_account_binding(tampered_wake)
    tamper_allowed = True
except ValueError:
    tamper_allowed = False
ok(not tamper_allowed, "provider-account substitution fails the affinity check")
tool_source = Path("mcp_server.py").read_text(encoding="utf-8")
ok("def dispatch_to_co_fleet(" in tool_source
   and "dispatch_mod.dispatch_to_co_fleet(" in tool_source,
   "MCP exposes the elastic fleet dispatcher instead of requiring an internal Python call")


class RecordingAws:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def call(self, service, operation, *args, **kwargs):
        self.calls.append((service, operation, args, kwargs))
        return self.responses.pop(0) if self.responses else {}


launch_aws = RecordingAws([{
    "FleetId": "fleet-1", "Instances": [{"InstanceIds": ["i-spot"]}], "Errors": [],
}])
launch = co_fleet.launch_capacity(
    launch_aws, general, 6, ["subnet-a", "subnet-b"], on_demand=False)
launch_args = launch_aws.calls[0][2]
configs_json = json.loads(launch_args[launch_args.index("--launch-template-configs") + 1])
overrides = configs_json[0]["Overrides"]
ok(launch["capacity_type"] == "spot" and len(overrides) == 6,
   "Spot request diversifies three instance types across two AZ subnets")
ok(any("capacity-optimized-prioritized" in str(arg) for arg in launch_args),
   "Spot request uses a capacity-aware diversified allocation strategy")


lt_data = {
    "ImageId": "ami-test", "InstanceType": "c7i.2xlarge",
    "UserData": base64.b64encode(base_script.encode()).decode(),
    "TagSpecifications": [],
}
version_aws = RecordingAws([
    {"LaunchTemplateVersions": [{"LaunchTemplateData": lt_data}]},
    {"LaunchTemplateVersion": {"VersionNumber": 6}},
])
derived = co_fleet.create_launch_version(version_aws, wake, general)
create_args = version_aws.calls[1][2]
created_data = json.loads(create_args[create_args.index("--launch-template-data") + 1])
created_script = base64.b64decode(created_data["UserData"]).decode()
created_tags = {tag["Key"]: tag["Value"]
                for spec in created_data["TagSpecifications"]
                if spec["ResourceType"] == "instance" for tag in spec["Tags"]}
ok(derived == 6 and created_tags.get("CO:BaseLTVersion") == "5",
   "per-wake LT derives from and records the pinned base version")
ok(created_tags.get("CO:ConfigRefHash") and "runtime/co-3" not in json.dumps(created_tags),
   "instance tags contain a reference hash, not the secret locator")
ok("ssm:/switchboard/co/runtime/co-3" in created_script,
   "derived LT injects the reference-only bootstrap")


ready_host = {
    "host_id": "host/i-ready", "status": "online", "stale": False,
    "runtimes": [{
        "runtime": "claude-code", "lanes": ["CO"],
        "capabilities": ["docs", "python", "github", "tests", "co_fleet", "claude_code"],
        "policy": {"allow_work": True},
    }],
}
ok(co_fleet._runtime_ready(ready_host, wake),
   "registration gate accepts the exact runtime/lane/capability with allow_work=true")
not_ready = json.loads(json.dumps(ready_host))
not_ready["runtimes"][0]["policy"]["allow_work"] = False
ok(not co_fleet._runtime_ready(not_ready, wake),
   "registration gate rejects allow_work=false")


old = time.time() - 2000
instance = {
    "InstanceId": "i-idle", "State": {"Name": "running"},
    "LaunchTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(old)),
    "Tags": [{"Key": "CO:Pool", "Value": "co-general"}],
}


class ScaleAws(RecordingAws):
    def call(self, service, operation, *args, **kwargs):
        self.calls.append((service, operation, args, kwargs))
        if operation == "describe-instances":
            return {"Reservations": [{"Instances": [instance]}]}
        return {"TerminatingInstances": [{"InstanceId": "i-idle"}]}


class IdleClient:
    def hosts(self, include_stale=True):
        return []

    def runners(self, host_id):
        return []

    def claimed_wakes(self, host_id):
        return []


config.state_path.write_text(json.dumps({"idle_since": {"i-idle": old}}))
scale_aws = ScaleAws()
scaled = co_fleet.scale_in_once(scale_aws, IdleClient(), config, now=time.time())
ok(scaled[0]["action"] == "terminate_idle"
   and any(call[1] == "terminate-instances" for call in scale_aws.calls),
   "worker idle over 10 minutes with zero claim/session/wake is terminated")


class ClaimedClient(IdleClient):
    def runners(self, host_id):
        return [{
            "runner_session_id": "runner-1", "status": "running", "stale": False,
            "claim": {"claim_id": "claim-1", "status": "active", "expires_at": time.time() + 600},
        }]


config.state_path.write_text(json.dumps({"idle_since": {"i-idle": old}}))
protected_aws = ScaleAws()
protected = co_fleet.scale_in_once(protected_aws, ClaimedClient(), config, now=time.time())
ok(protected[0]["action"] == "keep_active"
   and not any(call[1] == "terminate-instances" for call in protected_aws.calls),
   "active runner/claim prevents scale-in even when the instance is old")


print(f"\nCO Fleet: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
