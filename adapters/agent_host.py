#!/usr/bin/env python3
"""Agent Host daemon — wake-intent consumer (AGENT-HOST-SPEC §7, ADAPTER-9, decision #5).

The always-on process on an agent host. It is the layer between the durable-but-pull-based bus
and the runtime adapters: it registers host inventory, polls Switchboard wake intents, and for
each eligible one launches/reuses a supervised run_agent session via supervisor.py — or lets the
substrate record that no eligible host answered.

    register_host
    loop every N s:
        heartbeat_host(capacity)
        pull eligible pending wake intents
        claim one (if capacity)  → launch supervised run_agent → confirm start → complete_wake
        reap exited sessions

Substrate endpoints (register_host / request_wake / claim_wake / complete_wake …) are Codex's
lane (store/app); this only CONSUMES them. Built fail-open against the spec's operation names —
a missing/!200 endpoint logs and is skipped, never crashes the daemon — so it is ready the moment
those land. Pin REST paths below once Codex publishes them. Config via env: PM_BASE, PM_PROJECT,
PM_MCP_TOKEN, PM_HOST_ID, PM_REPO_ROOT, PM_AGENT_HOST_SOURCE_REPO_ROOT,
PM_HOST_MAX_SESSIONS, PM_AGENT_WORK_MODULE (real work_fn;
absent -> --dry, which claims+abandons safely), PM_AGENT_HOST_ALLOW_WORK,
PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM.
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_SRC = os.path.join(_ROOT, "src")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
import switchboard_core as sb  # noqa: E402  (reuses _http + agent_id, same contract)
import co_drain  # noqa: E402
import host_attestation  # noqa: E402
import host_self_update  # noqa: E402
import relay_auth  # noqa: E402
from agent_host_enrollment import (  # noqa: E402
    ACCOUNT_AFFINITIES_FILENAME,
    ACCOUNT_AFFINITY_IDS_KEY,
    host_heartbeat_ttl_s,
    preflight_claude_local_auth,
    preflight_codex_local_auth,
)
from codex.cloud_adapter import launch_wake as launch_codex_cloud_wake  # noqa: E402
from switchboard.connect import (  # noqa: E402
    Ack,
    Assignment,
    HostRuntimeConfig,
    ResourceLimits,
    build_launch_spec,
)
from switchboard.connect.verification_profile import (  # noqa: E402
    VerificationRuntimeError,
    failure as verification_failure,
    prove as prove_verification_profile,
)
from switchboard.domain.coordination.runtime_profile import (  # noqa: E402
    RUNTIME_BINARIES,
    build_runtime_profile,
    runtime_env_key,
)
from repository_workspace import (  # noqa: E402
    MaterializedWorkspace,
    WorkspaceMaterializationError,
    materialize as materialize_repository_workspace,
    materialize_host_worktree,
    revoke as revoke_repository_workspace,
    safe_receipt as safe_workspace_receipt,
    verify as verify_repository_workspace,
    verify_host_worktree,
)

PROJECT = os.environ.get("PM_PROJECT", "switchboard")
SUPERVISOR = os.path.join(_HERE, "codex", "supervisor.py")
RUN_AGENT = os.path.join(_HERE, "run_agent.py")
DIRECT_CODEX_SESSION = os.path.join(_HERE, "direct_codex_session.py")

# Spec operation → REST path. Centralized so Codex's published paths get pinned in ONE place.
P_REGISTER_HOST = "/ixp/v1/register_host"
P_HEARTBEAT_HOST = "/ixp/v1/heartbeat_host"
P_LIST_WAKES = "/txp/v1/list_wake_intents"
P_CLAIM_WAKE = "/txp/v1/claim_wake"
P_COMPLETE_WAKE = "/txp/v1/complete_wake"
P_REGISTER_RUNNER = "/ixp/v1/register_runner_session"
P_HEARTBEAT_RUNNER = "/ixp/v1/heartbeat_runner_session"
P_MINT_HOST_TUNNEL_URL = "/ixp/v1/mint_host_tunnel_url"
P_LIST_RUNNER_CONTROLS = "/ixp/v1/runner_controls"
P_CLAIM_RUNNER_CONTROL = "/ixp/v1/claim_runner_control"
P_COMPLETE_RUNNER_CONTROL = "/ixp/v1/complete_runner_control"
P_LIST_RUNNERS = "/ixp/v1/runner_sessions"
P_LIST_WORK_SESSIONS = "/ixp/v1/work_sessions"
P_PREFLIGHT_WORK_SESSION = "/ixp/v1/work_sessions/{work_session_id}/preflight"
P_DIRECT_SESSION_MCP_TOKEN = "/ixp/v1/direct_assignments/mcp_token"
P_RUNNER_LEASE_DUE = "/ixp/v1/runner_lease_due"
P_TALLY_SPEND = "/tally/v1/spend/ingest"
MESSAGE_ONLY_LANE = "__MESSAGE_ONLY__"
RUNTIME_PROVIDERS = {
    "codex": "openai",
    "claude-code": "anthropic",
    "cursor": "cursor",
}
AGENT_HOST_VERSION = os.environ.get("PM_AGENT_HOST_VERSION", "0.2.0")
RELEASE_MANAGEMENT_SIGNED_BUNDLE = "signed_bundle"
RELEASE_MANAGEMENT_DEPLOYMENT = "deployment_managed"
# Advertised when this build can serve browser Watch/Chat (supervisor PTY +
# outbound relay). Placement keys off this instead of sniffing version strings.
RUNNER_WATCH_CAPABILITY = "runner_watch"
RUNNER_LEASE_CAPABILITIES = ("execution_lease_v2", "runner_lease_enforcement")
_INFLIGHT_LAUNCHES = set()
_INFLIGHT_LAUNCHES_LOCK = threading.Lock()
# Completion receipts are maintenance, not presence authority. Retry a small
# rotating slice every daemon tick so an old cross-project backlog cannot delay
# the next host heartbeat. Retries continue indefinitely; this is a throughput
# bound, not an attempt cap or Human escalation.
_PENDING_COMPLETION_RETRIES_PER_TICK = 4
_PENDING_COMPLETION_PROJECT_CURSOR = 0


def host_serves_runner_watch():
    """True only when this host can really deliver browser Watch/Chat.

    BUG-91: the capability gates placement, so a false positive puts work on a
    host whose runner nobody can watch -- the exact failure it exists to prevent.
    It therefore proves the relay path by importing the modules that carry it,
    the same ones _ensure_host_bridge needs at runtime. An image missing them
    advertises nothing and is skipped rather than silently accepting the work.
    """
    try:
        from switchboard.application import runner_pty_relay  # noqa: F401
        from codex.pty_host_ws_client import open_host_bridge  # noqa: F401
    except Exception:
        try:
            root = os.path.abspath(os.path.join(_HERE, ".."))
            for candidate in (root, os.path.join(root, "src")):
                if candidate not in sys.path:
                    sys.path.insert(0, candidate)
            from switchboard.application import runner_pty_relay  # noqa: F401,F811
            from codex.pty_host_ws_client import open_host_bridge  # noqa: F401,F811
        except Exception:
            return False
    return True
_LOCAL_AUTH_LAST_PROBE_AT = 0.0
_BOUND_FINALIZERS_LOCK = threading.Lock()
_BOUND_FINALIZERS = {}
_BOUND_FINALIZER_RESULTS = []

_RUNNER_TRANSPORT_METADATA_FIELDS = {
    "pty", "stream_url", "relay_url", "transport", "browser_safe",
    "relay_required",
}


def _csv(value):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").replace("\n", ",").split(",") if x.strip()]


def _host_projects(inventory=None):
    """Projects this host must poll, preserving configured priority."""
    configured = ((inventory or {}).get("placement") or {}).get("projects")
    projects = configured if isinstance(configured, list) else []
    if not projects:
        projects = _csv(os.environ.get("PM_HOST_PROJECTS", PROJECT))
    return list(dict.fromkeys(str(project).strip() for project in projects
                              if str(project).strip()))


def _fair_wake_order(wakes, projects):
    """Interleave projects so one cold repository cannot starve another."""
    preferred = [PROJECT]
    preferred.extend(project for project in projects if project != PROJECT)
    buckets = {project: [] for project in preferred}
    extras = []
    for wake in wakes:
        project = _wake_project(wake)
        (buckets[project] if project in buckets else extras).append(wake)
    ordered = []
    while any(buckets.values()):
        for project in preferred:
            if buckets[project]:
                ordered.append(buckets[project].pop(0))
    ordered.extend(extras)
    return ordered


def _wake_project(wake):
    """Return the project attached by the project-scoped wake poll."""
    return str((wake or {}).get("_host_project")
               or (wake or {}).get("project_id")
               or (wake or {}).get("project")
               or PROJECT)


def _repository_identity(value, *, topology=False):
    """Normalize repository authority as host/owner/repo.

    Project topology historically stores a bare GitHub owner/repo slug. Origins
    must name their host explicitly; matching only the trailing path would let
    a same-named GitLab or local repository impersonate the canonical source.
    """
    raw = str(value or "").strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]
    if topology and re.fullmatch(r"[^/@:]+/[^/@:]+", raw):
        return f"github.com/{raw}".lower()
    scp = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", raw)
    if scp and "://" not in raw:
        host, path = scp.group(1), scp.group(2)
    else:
        parsed = urllib.parse.urlparse(raw)
        host, path = parsed.hostname or "", parsed.path
    parts = [part for part in str(path).replace("\\", "/").split("/") if part]
    return (f"{host}/{'/'.join(parts[-2:])}".lower()
            if host and len(parts) >= 2 else "")


def _source_origin_identity(source_root):
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (_repository_identity(result.stdout, topology=False)
            if result.returncode == 0 else "")


def _project_source_repo_roots_from_env():
    try:
        value = json.loads(os.environ.get("PM_HOST_PROJECT_SOURCE_REPO_ROOTS", "{}"))
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(project): str(root) for project, root in value.items()
            if str(project).strip() and str(root).strip()}


def _safe_identity(value):
    """Return a stable git-ref component for server-owned identifiers."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip(".-")
    if not safe:
        raise WorkspaceMaterializationError(
            "invalid_execution_identity",
            "project, task, and execution identifiers must be non-empty")
    return safe[:80]


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def effective_work_modules(runtimes):
    """Return each runtime's effective module after the documented fallback."""
    fallback = str(os.environ.get("PM_AGENT_WORK_MODULE") or "").strip()
    out = {}
    for runtime in runtimes or []:
        runtime = str(runtime or "").strip()
        if not runtime:
            continue
        out[runtime] = str(os.environ.get(runtime_env_key(runtime)) or fallback).strip()
    return out


def effective_runtime_profile(runtimes, runner_watch=None):
    """Probe the current process environment and finishing toolchain."""
    normalized = [str(runtime or "").strip() for runtime in runtimes or []
                  if str(runtime or "").strip()]
    binary_names = {"git", "gh"}
    binary_names.update(
        RUNTIME_BINARIES[runtime] for runtime in normalized
        if runtime in RUNTIME_BINARIES
    )
    return build_runtime_profile(
        runtimes=normalized,
        work_modules=effective_work_modules(normalized),
        auto_work_session=_truthy(os.environ.get("PM_AUTO_WORK_SESSION")),
        agent_host_version=AGENT_HOST_VERSION,
        binaries={name: bool(shutil.which(name)) for name in binary_names},
        runner_watch=(host_serves_runner_watch()
                      if runner_watch is None else bool(runner_watch)),
    )


def _memory_resources():
    total = available = None
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total = int(os.sysconf("SC_PHYS_PAGES")) * page_size
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as source:
            values = {}
            for line in source:
                key, _, raw = line.partition(":")
                values[key] = int((raw.strip().split() or ["0"])[0]) * 1024
        total = values.get("MemTotal") or total
        available = values.get("MemAvailable")
    except (OSError, TypeError, ValueError):
        pass
    return {
        "memory_mb_total": round(total / 1024 / 1024, 1) if total else None,
        "memory_mb_available": round(available / 1024 / 1024, 1) if available else None,
    }


def _redacted_local_auth(runtime):
    """Advertise local personal-auth readiness without returning account material."""
    available = _truthy(os.environ.get("PM_HOST_LOCAL_AUTH_AVAILABLE"))
    mode = str(os.environ.get("PM_HOST_LOCAL_AUTH_MODE") or "").strip()
    raw_proof = str(os.environ.get("PM_HOST_LOCAL_AUTH_ACCOUNT_PROOF") or "").strip()
    fingerprint = ""
    if raw_proof:
        fingerprint = raw_proof if re.fullmatch(r"acct-[0-9a-f]{16}", raw_proof) else (
            "acct-" + hashlib.sha256(
                f"switchboard-local-auth:{runtime}:{raw_proof}".encode()).hexdigest()[:16])
    return {
        "available": available,
        "runtime": runtime,
        "auth_mode": mode or ("local" if available else "unavailable"),
        "account_fingerprint": fingerprint or None,
        "credential_values_redacted": True,
        "provider_credential_exported": False,
    }


def refresh_local_auth_inventory(inventory, *, now=None, force=False):
    """Re-probe personal local auth and atomically refresh admission inventory."""
    global _LOCAL_AUTH_LAST_PROBE_AT
    runtimes = inventory.get("runtimes") or []
    if len(runtimes) != 1 or runtimes[0].get("runtime") not in {"codex", "claude-code"}:
        return False
    runtime = runtimes[0]["runtime"]
    personal_mode = "chatgpt_personal" if runtime == "codex" else "oauth_personal"
    current = dict(runtimes[0].get("local_auth") or {})
    if current.get("auth_mode") not in {personal_mode, "unavailable"}:
        return False
    checked_at = time.time() if now is None else float(now)
    try:
        interval = max(5.0, float(os.environ.get(
            "PM_HOST_LOCAL_AUTH_PROBE_INTERVAL_S", "30")))
    except ValueError:
        interval = 30.0
    if not force and checked_at - _LOCAL_AUTH_LAST_PROBE_AT < interval:
        return False
    _LOCAL_AUTH_LAST_PROBE_AT = checked_at
    try:
        if runtime == "codex":
            proof = preflight_codex_local_auth(
                codex_executable=os.environ.get("PM_CODEX_EXECUTABLE") or "")
        else:
            proof = preflight_claude_local_auth(
                claude_executable=os.environ.get("PM_CLAUDE_EXECUTABLE") or "")
        if proof.get("authenticated") is not True:
            raise RuntimeError(f"native {runtime} local auth is unavailable")
        refreshed = {
            "available": True,
            "runtime": runtime,
            "auth_mode": personal_mode,
            "account_fingerprint": proof.get("account_fingerprint") or None,
            "credential_values_redacted": True,
            "provider_credential_exported": False,
        }
    except Exception as exc:
        refreshed = {
            "available": False,
            "runtime": runtime,
            "auth_mode": personal_mode,
            "account_fingerprint": None,
            "credential_values_redacted": True,
            "provider_credential_exported": False,
            "unavailable_reason": type(exc).__name__,
        }
    runtimes[0]["local_auth"] = refreshed
    inventory.setdefault("capacity", {})["local_auth"] = refreshed
    return current != refreshed


def _identity_inventory():
    generation = str(os.environ.get("PM_HOST_IDENTITY_GENERATION") or "").strip()
    return {
        "schema": "switchboard.agent_host_identity_proof.v1",
        "enrollment_id": os.environ.get("PM_HOST_ENROLLMENT_ID") or None,
        "identity_generation": int(generation) if generation.isdigit() else None,
        "public_key_fingerprint": os.environ.get("PM_HOST_PUBLIC_KEY_FINGERPRINT") or None,
        "credential_values_redacted": True,
    }


def _declared_account_affinities():
    """Read CO-6 account fingerprints this host's own bearer has declared locally
    (see `agent_host_enrollment.py declare-account`). Only the already-authenticated
    host process reads/writes this file, so a remote caller can never inject an
    affinity — it can only ever reflect what this host already asserted about itself."""
    config_path = str(os.environ.get("PM_AGENT_HOST_CONFIG_PATH") or "").strip()
    if not config_path:
        return []
    declarations_path = os.path.join(
        os.path.dirname(config_path), ACCOUNT_AFFINITIES_FILENAME)
    try:
        with open(declarations_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    fingerprints = data.get(ACCOUNT_AFFINITY_IDS_KEY) if isinstance(data, dict) else None
    if not isinstance(fingerprints, list):
        return []
    return [str(item).strip() for item in fingerprints if str(item or "").strip()]


def placement_inventory(repo, runtime, policy):
    """Build the truthful, non-secret host-placement advertisement used by CO-9."""
    try:
        disk = shutil.disk_usage(repo)
        disk_values = {
            "disk_gb_total": round(disk.total / 1024 ** 3, 2),
            "disk_gb_available": round(disk.free / 1024 ** 3, 2),
        }
    except OSError:
        disk_values = {"disk_gb_total": None, "disk_gb_available": None}
    binary_names = {"git", "python3", "gh"}
    binary_names.add("claude" if runtime == "claude-code" else runtime)
    binaries = sorted(name for name in binary_names if name and shutil.which(name))
    bound_wake_id = str(os.environ.get("PM_WAKE_ID") or "").strip()
    ephemeral = bool(bound_wake_id)
    scheduler_class = os.environ.get(
        "PM_HOST_CLASS", "ephemeral" if ephemeral else "persistent")
    supports_leases = _truthy(os.environ.get("PM_HOST_SUPPORTS_CREDENTIAL_LEASES"))
    # Capability taxonomy (CO-15). Scheduler class stays persistent/ephemeral.
    if os.environ.get("PM_AUTH_HOST_CLASSES"):
        auth_host_classes = _csv(os.environ.get("PM_AUTH_HOST_CLASSES"))
    elif ephemeral or scheduler_class == "ephemeral":
        auth_host_classes = ["managed_or_ephemeral_worker"]
    elif supports_leases:
        auth_host_classes = ["trusted_private_worker", "user_owned_persistent"]
    else:
        auth_host_classes = ["managed_or_user_owned_worker"]
    default_trust_zone = (
        "cloud_ephemeral" if scheduler_class == "ephemeral" else "org_shared")
    isolation_modes = _csv(os.environ.get("PM_HOST_ISOLATION", "task_worktree"))
    workspace_backends = _csv(os.environ.get(
        "PM_HOST_WORKSPACE_BACKENDS",
        "worktree" if "task_worktree" in isolation_modes else ",".join(isolation_modes)))
    return {
        "schema": "switchboard.agent_host_placement.v1",
        "host_class": scheduler_class,
        "auth_host_classes": auth_host_classes,
        "cost_class": os.environ.get(
            "PM_HOST_COST_CLASS", "ephemeral_variable" if ephemeral else "already_paid"),
        "wakeable": True,
        # A provisioned CO worker is launched for exactly one wake.  Advertising the
        # non-secret wake id lets the coordinator exclude it from later placement;
        # the host-side queue filter remains the final enforcement boundary.
        "bound_wake_id": bound_wake_id or None,
        "drain_state": "accepting" if policy.get("allow_work") else "message_only",
        "tenant_ids": _csv(os.environ.get("PM_HOST_TENANTS", "")),
        # Provider-native enrollment is accepted only when this trusted host
        # explicitly attests the owning Switchboard user for the account affinity.
        # PM_HOST_OWNER_USERS (fleet/static) and PM_HOST_OWNER_USER_ID (ADAPTER-18
        # personal enrollment, one owner) are two producers of the same list.
        "owner_user_ids": sorted(set(
            _csv(os.environ.get("PM_HOST_OWNER_USERS", ""))
            + _csv(os.environ.get("PM_HOST_OWNER_USER_ID", ""))
        )),
        "projects": _csv(os.environ.get("PM_HOST_PROJECTS", PROJECT)),
        "trust_zone": os.environ.get("PM_HOST_TRUST_ZONE", default_trust_zone),
        "providers": _csv(os.environ.get("PM_HOST_PROVIDERS", "")),
        "account_affinity_ids": sorted(set(
            _csv(os.environ.get("PM_HOST_ACCOUNT_AFFINITIES", ""))
            + _declared_account_affinities()
        )),
        "supports_credential_leases": supports_leases,
        "repositories": _csv(os.environ.get(
            "PM_HOST_REPOSITORIES", "6th-Element-Labs/projectplanner")),
        "supports_scm_materialization": (
            _truthy(os.environ.get("PM_HOST_SUPPORTS_SCM_MATERIALIZATION", "1"))
            and "git" in binaries
        ),
        "scm_providers": _csv(os.environ.get("PM_HOST_SCM_PROVIDERS", "github_app,github")),
        "isolation_modes": isolation_modes,
        "workspace_backends": workspace_backends,
        "runtime_binaries": binaries,
        "provider_capacity_mode": "external_account_admission",
        "resources": {
            "cpu_total": os.cpu_count(),
            # CPU availability is scheduler input only when a host monitor supplies it;
            # total logical CPUs are not a truthful measure of current headroom.
            "cpu_available": (
                float(os.environ["PM_HOST_CPU_AVAILABLE"])
                if os.environ.get("PM_HOST_CPU_AVAILABLE") else None
            ),
            **_memory_resources(),
            **disk_values,
        },
        "concurrency": {
            "max_sessions": int(os.environ.get("PM_HOST_MAX_SESSIONS", "2")),
        },
    }


def host_policy_from_env(lanes):
    allow_work = _truthy(os.environ.get("PM_AGENT_HOST_ALLOW_WORK"))
    allow_global = _truthy(os.environ.get("PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM"))
    if not allow_work:
        mode = "message_only"
    elif allow_global:
        mode = "global_claim_allowed"
    elif lanes:
        mode = "lane_scoped"
    else:
        mode = "unconfigured_no_lanes"
    return {
        "mode": mode,
        "allow_message_only": True,
        "allow_work": allow_work,
        "allow_global_claim": allow_global,
        "allowed_lanes": lanes,
    }


def _try(method, path, body=None):
    """Fail-open REST: returns dict on success, None on any error (endpoint absent yet, etc.)."""
    try:
        return sb._http(method, path, body)
    except Exception as e:
        detail = str(e).replace("\n", " ")[:500]
        print(
            f"[agent_host] {method} {path} unavailable "
            f"({type(e).__name__}: {detail}); skipping",
            flush=True,
        )
        return None


# HARDEN-79: the mint is the credential-bearing half of the Watch path. When it
# 401s the companion's redial loop can never converge, so the failures have to
# be counted here rather than discarded by _try's fail-open print.
_RELAY_AUTH_FAULTS = []
_RELAY_AUTH_FAULTS_LOCK = threading.Lock()
_MINT_AUTH_POLICY = None


def _record_relay_auth_fault(fault):
    with _RELAY_AUTH_FAULTS_LOCK:
        _RELAY_AUTH_FAULTS.append(dict(fault))
    print(
        f"[agent_host] relay auth fault reason={fault.get('reason')} "
        f"attempts={fault.get('attempt_count')} "
        f"first_failure_at={fault.get('first_failure_at')} "
        f"credential_source={fault.get('credential_source')} "
        f"restart_required={fault.get('restart_required')}",
        flush=True,
    )


def drain_relay_auth_faults():
    """Take the faults raised since the last heartbeat (one per episode)."""
    with _RELAY_AUTH_FAULTS_LOCK:
        drained = list(_RELAY_AUTH_FAULTS)
        _RELAY_AUTH_FAULTS.clear()
    return drained


def _mint_auth_policy():
    global _MINT_AUTH_POLICY
    if _MINT_AUTH_POLICY is None:
        _MINT_AUTH_POLICY = relay_auth.RelayAuthFaultTracker(
            label="mint_host_tunnel_url", on_fault=_record_relay_auth_fault)
    return _MINT_AUTH_POLICY


def mint_host_tunnel_url(runner_session_id, host_id):
    """Ask Switchboard for a fresh relay URL without exposing its signing key.

    A rejection here is retried exactly once, and only after re-reading the
    bearer from its on-disk source: a rotation that already landed then heals
    without an operator restart. A rejection that survives that reload is a
    stale-credential fault, not a blip to print and forget.
    """
    body = {
        "project": PROJECT,
        "runner_session_id": str(runner_session_id or ""),
        "host_id": str(host_id or ""),
    }
    policy = _mint_auth_policy()
    for attempt in (1, 2):
        try:
            result = sb._http("POST", P_MINT_HOST_TUNNEL_URL, body) or {}
        except Exception as exc:  # noqa: BLE001 — fail-open stays, loud now
            kind = relay_auth.classify_relay_failure(exc)
            print(
                f"[agent_host] POST {P_MINT_HOST_TUNNEL_URL} failed "
                f"({type(exc).__name__}, {kind}); skipping", flush=True)
            decision = policy.record_failure(kind, f"{type(exc).__name__}: {exc}")
            if attempt == 1 and decision.get("healed"):
                continue  # rotated credential adopted — retry with it once
            return {}
        policy.record_success()
        return dict(result.get("server_relay") or {})
    return {}


def _fresh_server_relay(server_relay, runner_session_id, host_id):
    """Use an attached capability, pulling one when the bridge has none."""
    relay = dict(server_relay or {})
    if relay.get("host_url"):
        return relay
    return mint_host_tunnel_url(runner_session_id, host_id) or relay


def _collect_companion_relay_auth_fault(runner_session_id):
    """Pick up a fault the executor companion could not report itself."""
    try:
        from codex import supervisor as _sup
        fault = relay_auth.consume_fault(
            _sup._session_dir(str(runner_session_id or "")))
    except Exception:  # noqa: BLE001 — a missing session dir is not a fault
        return None
    if not fault:
        return None
    fault.setdefault("runner_session_id", str(runner_session_id or ""))
    fault.setdefault("origin", "executor_companion")
    _record_relay_auth_fault(fault)
    return fault


def _consume_host_relay_refresh_request(runner_session_id, host_id):
    """Use the enrolled-host credential for a companion-requested refresh."""
    try:
        from codex import supervisor as _sup
        request_path = _sup._session_dir(
            str(runner_session_id or "")) / "host_relay.refresh"
        if not request_path.exists():
            return {}
        relay = mint_host_tunnel_url(runner_session_id, host_id)
        if relay.get("host_url"):
            request_path.unlink(missing_ok=True)
        return relay
    except Exception as exc:  # noqa: BLE001
        print(
            f"[agent_host] host relay refresh failed "
            f"runner_session_id={runner_session_id} error={type(exc).__name__}",
            flush=True,
        )
        return {}


def _http_status_from_exception(exc):
    """Extract an HTTP status when sb._http raised RuntimeError(HTTP N ...) or HTTPError."""
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, urllib.error.HTTPError):
        return int(cause.code or 0)
    if isinstance(exc, urllib.error.HTTPError):
        return int(exc.code or 0)
    match = re.match(r"HTTP\s+(\d{3})\b", str(exc or ""))
    return int(match.group(1)) if match else 0


def _error_code_from_exception(exc, default="runner_bind_incomplete"):
    text = str(exc or "")
    match = re.search(r"error_code=([A-Za-z0-9_.-]+)", text)
    return match.group(1) if match else default


def _require(method, path, body=None):
    """Fail-closed REST used for COORD-34 claim-bound runner registration.

    Transport/local exceptions (URLError, timeout, 5xx) are broken_connection.
    HTTP 4xx registry/policy refusals (including narrow-host 403 raised by
    sb._http as RuntimeError) stay refused / non-transport so Connect does not
    retry them as blips.
    """
    try:
        return sb._http(method, path, body)
    except Exception as e:
        print(f"[agent_host] {method} {path} failed ({type(e).__name__}): {e}", flush=True)
        status = _http_status_from_exception(e)
        if 400 <= status < 500:
            error_code = _error_code_from_exception(e)
            return {
                "error": error_code,
                "error_code": error_code,
                "failure_class": "failed_gate",
                "refused": True,
                "transport_error": False,
                "http_status": status,
                "message": f"{method} {path} failed: {type(e).__name__}: {e}",
            }
        return {
            "error": "control_plane_unavailable",
            "error_code": "control_plane_unavailable",
            "failure_class": "broken_connection",
            "refused": False,
            "transport_error": True,
            "http_status": status or None,
            "message": f"{method} {path} failed: {type(e).__name__}: {e}",
        }


def _is_transport_registration_error(reg):
    """True when runner registration failed because the control plane was unreachable."""
    if reg is None:
        return True
    if not isinstance(reg, dict):
        return False
    if reg.get("transport_error"):
        return True
    if reg.get("error_code") == "control_plane_unavailable":
        return True
    if reg.get("failure_class") == "broken_connection":
        return True
    return False


def _classify_connect_registration_failure(reg):
    """Separate control-plane transport blips from registry policy refusals."""
    if _is_transport_registration_error(reg):
        message = (
            (reg or {}).get("message")
            or "Control plane unavailable during Connect runner registration"
        )
        return {
            "failure_class": "broken_connection",
            "provider_error": str(message)[:500],
            "reason": "connect_runner_registration_transport_failed",
        }
    return {
        "failure_class": "failed_gate",
        "provider_error": "Connect runner registry rejected the launch",
        "reason": "connect_runner_registration_failed",
    }


def _complete_wake_with_retry(body, attempts=3, delay_s=0.5):
    """Durably post one exact complete_wake receipt.

    A launch can fail before a runner exists, so there is no runner heartbeat
    available to finish this capacity transaction later.  Persist the callback
    before the first attempt and retain it after bounded inline retries; the
    host loop replays it until the control plane returns a terminal readback.
    """
    _persist_pending_wake_receipt(body)
    last = None
    total = max(1, int(attempts or 1))
    for i in range(total):
        last = _try("POST", P_COMPLETE_WAKE, body)
        if _wake_completion_recorded(last):
            _delete_pending_wake_receipt(
                (body or {}).get("project"), (body or {}).get("wake_id"))
            return last
        if i + 1 < total:
            print(
                f"[agent_host] POST {P_COMPLETE_WAKE} incomplete; "
                f"retry {i + 1}/{total}",
                flush=True,
            )
            time.sleep(max(0.0, float(delay_s or 0)))
    print(
        f"[agent_host] POST {P_COMPLETE_WAKE} exhausted retries; "
        f"durable receipt retained wake_id={(body or {}).get('wake_id')}",
        flush=True,
    )
    return last


def _wake_completion_recorded(response):
    if not isinstance(response, dict) or response.get("error") \
            or response.get("error_code"):
        return False
    if response.get("reason") == "exact_binding_denied":
        return False
    return bool(
        response.get("ok") is True
        or response.get("status") in {"completed", "failed", "cancelled"}
        or response.get("note") in {
            "already terminal", "idempotent terminal readback",
        }
    )


def _pending_wake_receipt_dir():
    root = Path(os.environ.get("PM_RUNNER_DIR", ".switchboard/runner")).resolve()
    path = root / "_pending_wake_completions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pending_wake_receipt_path(project, wake_id):
    identity = f"{project or PROJECT}__{wake_id or ''}"
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", identity)
    return _pending_wake_receipt_dir() / f"{safe_id}.json"


def _persist_pending_wake_receipt(receipt):
    path = _pending_wake_receipt_path(
        (receipt or {}).get("project"), (receipt or {}).get("wake_id"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _delete_pending_wake_receipt(project, wake_id):
    try:
        _pending_wake_receipt_path(project, wake_id).unlink()
    except FileNotFoundError:
        pass


def _pending_receipt_replay_limit():
    """Bound callback work so stale receipts cannot consume the host tick."""
    try:
        configured = int(
            os.environ.get("PM_PENDING_RECEIPT_REPLAY_LIMIT") or "16")
    except (TypeError, ValueError):
        configured = 16
    return max(1, min(100, configured))


def _receipt_refusal_is_irrecoverable(response):
    """Return true only for receipt-specific refusals that retry cannot repair."""
    if not isinstance(response, dict):
        return False
    if response.get("reason") == "exact_binding_denied":
        return True
    status = int(response.get("http_status") or 0)
    # Authentication, rate limiting, and request timeout can recover without
    # changing the immutable receipt. Malformed/missing/conflicting bindings
    # cannot.
    return status in {400, 404, 409, 410, 422}


def _safe_receipt_error(response):
    """Return bounded, non-secret failure evidence for host logs/readback."""
    if not isinstance(response, dict):
        return "control_plane_unavailable"
    code = (
        response.get("error_code")
        or response.get("error")
        or response.get("reason")
    )
    status = int(response.get("http_status") or 0)
    if code and status:
        return f"http_{status}:{str(code)[:120]}"
    if code:
        return str(code)[:120]
    if status:
        return f"http_{status}"
    return "control_plane_unavailable"


def _archive_pending_receipt(path, *, kind, response):
    """Move one irrecoverable receipt out of the active replay queue."""
    archive = path.parent.parent / "_terminal_receipt_archive"
    archive.mkdir(parents=True, exist_ok=True)
    suffix = f".{int(time.time() * 1000)}.{kind}.json"
    target = archive / f"{path.stem}{suffix}"
    os.replace(path, target)
    print(
        f"[agent_host] archived irrecoverable {kind} receipt "
        f"path={target.name} error={_safe_receipt_error(response)}",
        flush=True,
    )
    return target


def _pending_receipt_paths(directory):
    """Oldest attempted receipt first; transient failures rotate to the back."""
    paths = list(directory.glob("*.json"))
    return sorted(
        paths,
        key=lambda path: (
            path.stat().st_mtime if path.exists() else float("inf"),
            path.name,
        ),
    )


def _drain_pending_wake_receipts():
    """Replay pre-registration terminal receipts before accepting fresh work."""
    outcomes = []
    paths = _pending_receipt_paths(_pending_wake_receipt_dir())
    for path in paths[:_pending_receipt_replay_limit()]:
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _archive_pending_receipt(
                path, kind="wake_completion",
                response={"error_code": "invalid_receipt_json",
                          "http_status": 400})
            continue
        terminal = _require("POST", P_COMPLETE_WAKE, receipt)
        recorded = _wake_completion_recorded(terminal)
        if recorded:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        elif _receipt_refusal_is_irrecoverable(terminal):
            _archive_pending_receipt(
                path, kind="wake_completion", response=terminal)
        else:
            path.touch()
        outcomes.append({
            "wake_id": receipt.get("wake_id"),
            "task_id": ((receipt.get("result") or {}).get("task_id")),
            "reason": "pending_wake_completion_retry",
            "completed": recorded,
            "archived": (
                not recorded and _receipt_refusal_is_irrecoverable(terminal)),
            "error": None if recorded else _safe_receipt_error(terminal),
        })
    return outcomes


CONNECT_REGISTER_ATTEMPTS = 3
CONNECT_REGISTER_RETRY_DELAY_S = 0.5


def default_inventory():
    repo = (os.environ.get("PM_AGENT_HOST_SOURCE_REPO_ROOT")
            or os.environ.get("PM_REPO_ROOT") or _git_root())
    host_id = os.environ.get("PM_HOST_ID") or f"host/{socket.gethostname().split('.')[0]}"
    env_lanes = _csv(os.environ.get("PM_HOST_LANES", ""))
    policy = host_policy_from_env(env_lanes)
    runtime_lanes = env_lanes or ([MESSAGE_ONLY_LANE] if not policy["allow_work"] else [])
    runtime = os.environ.get("PM_RUNTIME", "claude-code")
    provider = os.environ.get("PM_PROVIDER") or RUNTIME_PROVIDERS.get(runtime, runtime)
    cloud_enabled = runtime == "codex" and bool(os.environ.get("PM_CODEX_CLOUD_ENVIRONMENT_ID"))
    profiles = ["ixp.v1", "txp.dispatch.v0"]
    capabilities = ["docs", "python", "github", "tests"]
    capabilities.extend(RUNNER_LEASE_CAPABILITIES)
    # Fleet workers advertise a host-owned capability profile.  The wake payload may
    # select from this inventory, but it cannot add capabilities to the host.  Keeping
    # this in configuration lets co-general/co-build use the same immutable AMI while
    # still failing closed when a heavy-build wake lands on a general worker.
    capabilities.extend(_csv(os.environ.get("PM_HOST_CAPABILITIES", "")))
    # BUG-91: self-declare Watch/Chat only when this host can genuinely serve it.
    # The claim must mean "I can deliver PTY output, accept input, and reach the
    # relay" -- not merely "I am a newer build". A version number would be the
    # easy signal and the wrong one: it says nothing about whether the relay
    # modules are actually installed on this image.
    runner_watch_proven = host_serves_runner_watch()
    if runner_watch_proven:
        capabilities.append(RUNNER_WATCH_CAPABILITY)
    capabilities = list(dict.fromkeys(capabilities))
    if cloud_enabled:
        profiles.append("cloud_execution")
        capabilities.append("cloud_execution")
    placement = placement_inventory(repo, runtime, policy)
    local_auth = _redacted_local_auth(runtime)
    runtime_profile = effective_runtime_profile([runtime], runner_watch=runner_watch_proven)
    release_management = (
        RELEASE_MANAGEMENT_SIGNED_BUNDLE
        if str(os.environ.get("PM_AGENT_HOST_STATE_PATH") or "").strip()
        else RELEASE_MANAGEMENT_DEPLOYMENT
    )
    owner = {
        "user_id": os.environ.get("PM_HOST_OWNER_USER_ID") or None,
        "tenant_allowlist": placement.get("tenant_ids") or [],
        "project_allowlist": placement.get("projects") or [],
        "provider_allowlist": placement.get("providers") or [],
    }
    return {
        "project": PROJECT, "host_id": host_id, "hostname": socket.gethostname(),
        "agent_host_version": AGENT_HOST_VERSION, "repo_root": repo,
        "project_source_repo_roots": _project_source_repo_roots_from_env(),
        "policy": policy,
        "runtimes": [{
            "runtime": runtime,
            "provider": provider,
            "launcher": "codex cloud exec" if cloud_enabled else (
                "codex" if runtime == "codex" else sys.executable),
            "profiles": profiles,
            "control": {"mode": "hook_deny", "runner_kill": True, "host_policy": policy["mode"]},
            "policy": policy,
            "lanes": runtime_lanes,
            "capabilities": capabilities,
            "local_auth": local_auth,
        }],
        "limits": {"max_sessions": int(os.environ.get("PM_HOST_MAX_SESSIONS", "2"))},
        "capacity": {
            "active_sessions": 0,
            "headroom": int(os.environ.get("PM_HOST_MAX_SESSIONS", "2")),
            "drain_state": placement.get("drain_state"),
            "placement": placement,
            "identity": _identity_inventory(),
            "owner": owner,
            "local_auth": local_auth,
            "runtime_profile": runtime_profile,
            # A signed, enrolled desktop Host can replace its own bundle. The
            # source-deployed VM Host is updated by the deployment service and
            # must never be offered the desktop package as though it could.
            "release_management": release_management,
        },
        "heartbeat_ttl_s": host_heartbeat_ttl_s(
            os.environ.get("PM_HOST_HEARTBEAT_TTL_S")),
    }


#: In-process only, and deliberately so: a successful update restarts the
#: service, so the next process starts clean and finds itself already current.
#: Persisting the phase would let it outlive the update it described.
_UPDATE_STATE: dict = {}


def heartbeat_capacity(inventory):
    """Return the full non-secret admission record for each heartbeat."""
    active = active_session_count(inventory)
    reserved = reserved_launch_count()
    occupied = active + reserved
    maximum = int((inventory.get("limits") or {}).get("max_sessions") or 0)
    capacity = dict(inventory.get("capacity") or {})
    capacity.update({
        "active_sessions": active,
        "reserved_launches": reserved,
        "occupied_sessions": occupied,
        "headroom": max(0, maximum - occupied),
        "allow_work": bool((inventory.get("policy") or {}).get("allow_work")),
        "drain_state": ((capacity.get("placement") or {}).get("drain_state")
                        or capacity.get("drain_state") or "accepting"),
        # Re-probe on every registration/heartbeat.  A daemon-start snapshot
        # would miss an in-place binary removal or PATH/profile correction.
        "runtime_profile": effective_runtime_profile([
            entry.get("runtime") for entry in inventory.get("runtimes") or []
            if isinstance(entry, dict)
        ]),
        "release_management": (inventory.get("capacity") or {}).get(
            "release_management", RELEASE_MANAGEMENT_DEPLOYMENT),
        # What this bundle can actually DO, as opposed to the fact that it is
        # alive. A heartbeat proved liveness and nothing proved compatibility,
        # which is how a green 0.4.15 host ate three Wave A missions on
        # 2026-07-31. Sibling of runtime_profile, never inside it: profile
        # components are canonically hashed for placement eligibility.
        "host_attestation": host_attestation.attestation(
            update_state=str(_UPDATE_STATE.get("phase") or ""),
            update_error=str(_UPDATE_STATE.get("failed_error") or "")),
    })
    return capacity


def reserved_launch_count():
    """Capacity already claimed for a runner whose process is not live yet."""
    with _INFLIGHT_LAUNCHES_LOCK:
        return len(_INFLIGHT_LAUNCHES)


def capacity_occupancy(inventory):
    return active_session_count(inventory) + reserved_launch_count()


def _reserve_launch(wake_id):
    key = str(wake_id or f"anonymous-{threading.get_ident()}")
    with _INFLIGHT_LAUNCHES_LOCK:
        _INFLIGHT_LAUNCHES.add(key)
    return key


def _release_launch(key):
    with _INFLIGHT_LAUNCHES_LOCK:
        _INFLIGHT_LAUNCHES.discard(key)


def _bounded_materialization_timeout():
    """Keep the whole launch inside the server's claimed-wake hold window."""
    try:
        requested = float(
            os.environ.get("PM_CONNECT_MATERIALIZE_TIMEOUT_SECONDS") or "45")
    except (TypeError, ValueError):
        requested = 45.0
    try:
        claim_hold = float(
            os.environ.get("PM_CONNECT_CLAIM_HOLD_SECONDS") or "90")
    except (TypeError, ValueError):
        claim_hold = 90.0
    claim_hold = max(5.0, claim_hold)
    safety = max(2.0, min(10.0, claim_hold * 0.1))
    return max(0.1, min(max(0.1, requested), claim_hold - safety))


def _heartbeat_while_materializing(stop, inventory, interval_s):
    """Preserve Capacity control while one bounded repository operation runs."""
    while not stop.wait(interval_s):
        capacity = heartbeat_capacity(inventory)
        body = {
            "project": PROJECT,
            "host_id": inventory["host_id"],
            "active_sessions": capacity["active_sessions"],
            "capacity": capacity,
        }
        for project in _host_projects(inventory):
            _try("POST", P_HEARTBEAT_HOST, {**body, "project": project})
        # These are Capacity-plane operations only: renew the exact live
        # execution leases and service explicit runner controls. Task status,
        # review, remediation, merge, and Done remain outside this loop.
        renew_live_direct_runners(inventory)
        handle_runner_controls(inventory)


def _materialize_for_launch(materialize_workspace, workspace_request,
                            wake, inventory):
    timeout_s = _bounded_materialization_timeout()
    reservation = _reserve_launch((wake or {}).get("wake_id"))
    stop = threading.Event()
    interval_s = max(0.05, min(10.0, timeout_s / 3.0))
    heartbeat = threading.Thread(
        target=_heartbeat_while_materializing,
        args=(stop, inventory, interval_s),
        name=f"agent-host-materialize-{reservation}",
        daemon=True,
    )
    heartbeat.start()
    try:
        return materialize_workspace(
            **workspace_request, timeout_s=timeout_s)
    finally:
        stop.set()
        heartbeat.join(timeout=min(1.0, interval_s + 0.1))
        _release_launch(reservation)


def registration_inventory(inventory, drain_request=None):
    """Build a host advertisement from live supervisor capacity.

    Registration is periodically renewed, so it must be just as current as a
    heartbeat.  Reusing the inventory constructed at process startup resets a
    busy host to 0 active sessions on every renewal.
    """
    advertised = dict(inventory)
    advertised["capacity"] = heartbeat_capacity(inventory)
    if drain_request:
        advertised = co_drain.inventory_for_drain(advertised)
        placement = ((advertised.get("capacity") or {}).get("placement") or {})
        placement["drain_state"] = "draining"
    return advertised


def apply_required_host_release(inventory, response, capacity):
    """Keep this host on the release the server says it should be running.

    The server already computed the answer; before this, nothing on the host
    read it. A contract-breaking change therefore surfaced as a fleet-wide
    launch outage that an operator had to diagnose by hand and repair by
    re-running the installer.

    Returns the live plan when an update is in flight, so the caller can stop
    claiming work for this tick. Draining is the whole reason the drain ends:
    the server withholds work from a host in ``draining``, and this stops it
    asking for any.
    """
    required = (response or {}).get("required_host_release") or {}
    plan = host_self_update.decide(
        required=required,
        installed_digest=host_attestation.bundle_digest(),
        installed_version=AGENT_HOST_VERSION,
        state=_UPDATE_STATE,
        enrolled=bool(str(os.environ.get("PM_AGENT_HOST_STATE_PATH") or "").strip()),
    )
    if plan.get("abandon"):
        # The drain never quiesced. Go back to work on the installed bundle and
        # keep the reason visible: a host that silently gives up looks identical
        # to one that succeeded.
        print(f"[agent_host] abandoned self-update: {plan.get('error')}", flush=True)
        _UPDATE_STATE.clear()
        _UPDATE_STATE["failed_error"] = str(plan.get("error") or "")
        return None
    if not plan.act:
        _UPDATE_STATE.pop("phase", None)
        return None

    _UPDATE_STATE.setdefault("started_at", plan.get("started_at") or time.time())
    if plan.get("update_request_id"):
        _UPDATE_STATE["update_request_id"] = str(plan.get("update_request_id"))
    plan = host_self_update.advance(
        plan=plan, active_sessions=int(capacity.get("active_sessions") or 0))
    _UPDATE_STATE["phase"] = plan["phase"]

    if plan["phase"] != host_self_update.INSTALLING:
        print(f"[agent_host] draining for update to "
              f"{plan.get('target_version') or plan.get('target_digest')}: "
              f"{capacity.get('active_sessions')} runner(s) still live", flush=True)
        return plan

    try:
        result = host_self_update.install(plan)
        print(f"[agent_host] installed {result.get('version')}; restarting", flush=True)
    except Exception as exc:
        # Record the digest that failed so the next heartbeat does not retry the
        # same bad bundle forever. The operator sees the reason on the host card.
        print(f"[agent_host] self-update failed: {exc}", flush=True)
        failed_request_id = str(
            plan.get("update_request_id") or _UPDATE_STATE.get("update_request_id") or "")
        _UPDATE_STATE.clear()
        _UPDATE_STATE["failed_digest"] = str(plan.get("target_digest") or "")
        _UPDATE_STATE["failed_request_id"] = failed_request_id
        _UPDATE_STATE["failed_error"] = str(exc)
    return plan


def apply_authoritative_execution_policy(inventory, response):
    """Hot-apply the authenticated server policy to one enrolled personal host.

    The enrollment record is the durable authority.  Local installer environment
    values are only bootstrap defaults, so an operator can broaden or tighten lane
    scope and concurrency without rotating credentials or touching launchd.
    """
    policy = dict((response or {}).get("authoritative_execution_policy") or {})
    if not policy:
        return False
    runtimes = inventory.get("runtimes") or []
    advertised_runtime = runtimes[0].get("runtime") if len(runtimes) == 1 else None
    # CO-23: an enrollment authorizes exactly one runtime. The policy must name
    # the same supported runtime this host advertises; anything else is refused.
    if (policy.get("runtime") not in {"codex", "claude-code"}
            or policy.get("runtime") != advertised_runtime
            or policy.get("allow_global_claim") is not False):
        print("[agent_host] refused invalid authoritative execution policy", flush=True)
        return False
    try:
        maximum = int(policy.get("max_sessions"))
    except (TypeError, ValueError):
        return False
    if not 1 <= maximum <= 32:
        return False
    lane_mode = str(policy.get("lane_mode") or "explicit")
    lanes = sorted({str(item).strip() for item in policy.get("lanes") or []
                    if str(item).strip()})
    if lane_mode not in {"explicit", "all_project_lanes"}:
        return False
    if lane_mode == "explicit" and not lanes:
        return False
    if lane_mode == "all_project_lanes":
        lanes = []
    runtime = runtimes[0]
    before = json.dumps({
        "lanes": runtime.get("lanes"),
        "capabilities": runtime.get("capabilities"),
        "policy": runtime.get("policy"),
        "max_sessions": (inventory.get("limits") or {}).get("max_sessions"),
    }, sort_keys=True, default=str)
    host_policy = dict(runtime.get("policy") or {})
    host_policy.update({
        "mode": "project_wide" if lane_mode == "all_project_lanes" else "lane_scoped",
        "allow_message_only": True,
        "allow_work": bool(policy.get("allow_work")),
        "allow_global_claim": False,
        "allowed_lanes": lanes,
        "lane_mode": lane_mode,
    })
    # BUG-91: runner_watch is a host-PROVEN fact, not an operator-grantable
    # permission. The authoritative policy selects every other capability, but
    # it can neither grant Watch to a host that cannot serve it (work would land
    # on a host whose runner nobody can watch) nor strip it from one that can
    # (registration advertised it, then the first heartbeat's policy replaced
    # the list wholesale and silently un-advertised it — which would starve
    # placement the moment PM_COORD_REQUIRE_RUNNER_WATCH is enforced).
    capabilities = [item for item in (policy.get("capabilities") or [])
                    if str(item).strip().lower() != RUNNER_WATCH_CAPABILITY]
    # SIMPLIFY-20 / BUG-161: these are host-proven execution facts, not
    # operator-grantable permissions. An older enrolled policy must not strip
    # them after startup and make an enforcement-capable host ineligible.
    capabilities.extend(RUNNER_LEASE_CAPABILITIES)
    if host_serves_runner_watch():
        capabilities.append(RUNNER_WATCH_CAPABILITY)
    runtime.update({
        "lanes": lanes,
        "capabilities": list(dict.fromkeys(capabilities)),
        "policy": host_policy,
    })
    runtime.setdefault("control", {})["host_policy"] = host_policy["mode"]
    inventory["policy"] = host_policy
    inventory.setdefault("limits", {})["max_sessions"] = maximum
    capacity = inventory.setdefault("capacity", {})
    capacity["headroom"] = max(0, maximum - capacity_occupancy(inventory))
    placement = capacity.setdefault("placement", {})
    placement.setdefault("concurrency", {})["max_sessions"] = maximum
    after = json.dumps({
        "lanes": runtime.get("lanes"),
        "capabilities": runtime.get("capabilities"),
        "policy": runtime.get("policy"),
        "max_sessions": inventory["limits"]["max_sessions"],
    }, sort_keys=True, default=str)
    changed = before != after
    if changed:
        print(
            f"[agent_host] applied policy revision {policy.get('revision') or '?'}: "
            f"lane_mode={lane_mode} max_sessions={maximum}", flush=True)
    return changed


def validate_personal_wake_binding(wake, inventory):
    """Fail closed when a personal-host wake opts into the exact-bind contract."""
    policy = (wake or {}).get("policy") or {}
    personal = (policy.get("execution_mode") == "personal_agent_host"
                or policy.get("require_exact_host_binding") is True)
    if not personal:
        return {"required": False, "valid": True}
    selector = (wake or {}).get("selector") or {}
    binding = policy.get("account_binding") or {}
    execution = policy.get("execution_binding") or {}
    expected_runner_session_id = _runner_session_id_for_wake(
        wake or {}, str(inventory.get("host_id") or ""))
    sources = {
        "wake_id": [(wake or {}).get("wake_id"), execution.get("wake_id")],
        "task_id": [
            (wake or {}).get("task_id"), binding.get("task_id"), execution.get("task_id")],
        "claim_id": [binding.get("claim_id"), execution.get("claim_id")],
        "work_session_id": [
            binding.get("work_session_id"), execution.get("work_session_id")],
        "runner_session_id": [
            binding.get("runner_session_id"), execution.get("runner_session_id"),
            expected_runner_session_id],
        "host_id": [
            inventory.get("host_id"), binding.get("host_id"), execution.get("host_id")],
        "agent_id": [selector.get("agent_id"), binding.get("agent_id"),
                     execution.get("agent_id")],
        "execution_connection_id": [
            policy.get("execution_connection_id"),
            execution.get("execution_connection_id")],
        "source_sha": [policy.get("source_sha"), execution.get("source_sha")],
    }
    missing = sorted(
        f"{key}[{index}]"
        for key, candidates in sources.items()
        for index, value in enumerate(candidates)
        if not str(value or "").strip()
    )
    if selector.get("runtime") != "codex":
        missing.append("selector.runtime=codex")
    if missing:
        return {"required": True, "valid": False, "error": "wake_binding_incomplete",
                "failure_class": "unbound_identity", "missing": sorted(set(missing))}

    normalized = {
        key: [str(value).strip() for value in candidates]
        for key, candidates in sources.items()
    }
    mismatches = sorted(
        key for key, candidates in normalized.items() if len(set(candidates)) != 1)
    opaque_fields = (
        "wake_id", "task_id", "claim_id", "work_session_id", "runner_session_id",
        "host_id", "agent_id", "execution_connection_id",
    )
    malformed = sorted(
        key for key in opaque_fields
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,255}", value)
               for value in normalized[key])
    )
    if any(not re.fullmatch(r"[0-9a-f]{40}", value)
           for value in normalized["source_sha"]):
        malformed.append("source_sha")
    if mismatches or malformed:
        return {
            "required": True,
            "valid": False,
            "error": "wake_binding_inconsistent",
            "failure_class": "unbound_identity",
            "mismatches": mismatches,
            "malformed": sorted(set(malformed)),
        }
    return {"required": True, "valid": True,
            "binding": {key: candidates[0] for key, candidates in normalized.items()}}


def _git_root():
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or os.getcwd()
    except Exception:
        return os.getcwd()


def eligible_runtime(wake, inventory):
    """Return the host runtime entry that can serve this wake, else None (skip → don't claim)."""
    sel = (wake or {}).get("selector") or {}
    want_rt, want_lane = sel.get("runtime"), sel.get("lane")
    want_provider = sel.get("provider")
    want_caps = set(_csv(sel.get("capabilities") or []))
    requested_mode = str(((wake or {}).get("policy") or {}).get("mode") or "").strip()
    wants_claim = requested_mode in {"claim_next", "direct_task"} or bool(
        want_lane and requested_mode != "message_only")
    for rt in inventory["runtimes"]:
        if want_rt and rt["runtime"] != want_rt:
            continue
        host_provider = rt.get("provider") or RUNTIME_PROVIDERS.get(
            str(rt.get("runtime") or ""), rt.get("runtime"))
        if want_provider and host_provider != want_provider:
            continue
        rt_policy = {**(inventory.get("policy") or {}), **(rt.get("policy") or {})}
        rt_lanes = set(rt.get("lanes") or [])
        if wants_claim:
            if not rt_policy.get("allow_work"):
                continue
            if want_lane:
                if (rt_policy.get("lane_mode") != "all_project_lanes"
                        and want_lane not in rt_lanes):
                    continue
            elif not rt_policy.get("allow_global_claim"):
                continue
        elif want_lane and rt_lanes and want_lane not in rt_lanes and MESSAGE_ONLY_LANE not in rt_lanes:
            continue
        if want_caps and not want_caps.issubset(set(rt.get("capabilities") or [])):
            continue
        return rt
    return None


def wakes_bound_to_host(wakes):
    """Restrict an ephemeral fleet host to the exact wake that launched it.

    Persistent Agent Hosts do not set ``PM_WAKE_ID`` and retain the shared eligible
    queue behavior. A fleet worker does set it; accepting another same-lane wake would
    break the provisioner's task/runtime/credential affinity guarantee.
    """
    bound_wake_id = str(os.environ.get("PM_WAKE_ID") or "").strip()
    if not bound_wake_id:
        return list(wakes or [])
    return [wake for wake in (wakes or []) if wake.get("wake_id") == bound_wake_id]


def wake_mode(wake, inventory=None):
    """Choose the safe launch mode for a wake.

    Lane-scoped wakes may enter the claim_next loop. Lane-less wakes are message-only by
    construction: they can register and read inbox, but must never ask for global work.
    """
    policy = (wake or {}).get("policy") or {}
    selector = (wake or {}).get("selector") or {}
    explicit = (policy.get("mode") or "").strip()
    if explicit == "connect":
        return "connect"
    if explicit == "direct_task":
        return "direct_task"
    if explicit == "cloud_execution" or policy.get("kind") == "cloud_execution":
        return "cloud_execution"
    if explicit in ("inbox_only", "message_only"):
        return "inbox_only"
    if explicit == "claim_next" and selector.get("lane"):
        return "claim_next"
    if explicit == "claim_next":
        inv_policy = (inventory or {}).get("policy") or {}
        return "claim_next" if inv_policy.get("allow_global_claim") else "refused"
    if selector.get("lane"):
        return "claim_next"
    return "inbox_only"


def active_session_count(inventory):
    """Best-effort live session count from the supervisor (capacity gate). 0 on any error."""
    try:
        out = subprocess.run(
            [sys.executable, SUPERVISOR, "list"],
            capture_output=True, text=True, timeout=10)
        data = json.loads(out.stdout or "[]")
        sessions = data if isinstance(data, list) else data.get("sessions", [])
        return sum(1 for s in sessions if s.get("status") == "running")
    except Exception:
        return 0


def active_codex_cloud_session_count():
    """Count centrally bound non-terminal Codex cloud sessions; None fails capacity closed."""
    result = _try(
        "GET",
        f"{P_LIST_RUNNERS}?project={PROJECT}&runtime=codex&include_stale=false",
    )
    if result is None:
        return None
    sessions = result.get("sessions") if isinstance(result, dict) else result
    if not isinstance(sessions, list):
        return None
    active = 0
    for session in sessions:
        metadata = session.get("metadata") or {}
        if metadata.get("vendor_id") != "openai-codex-cloud" or session.get("stale"):
            continue
        if str(session.get("status") or "").lower() not in _TERMINAL_RUNNER_STATES:
            active += 1
    return active


CONNECT_RUNTIME_DEFAULTS = {
    "codex": ("codex", "--dangerously-bypass-approvals-and-sandbox"),
    "claude-code": ("claude", "--dangerously-skip-permissions"),
    "cursor": ("cursor-agent", "--force"),
}


def _ensure_codex_workspace_trusted(workspace_path: str) -> None:
    """Seed exact-path trust so Connect Codex skips the interactive trust TUI.

    Parent-directory trust entries are not enough: Codex prompts per cwd. An
    unanswered "1 or 2" prompt blocks the session and starves runner heartbeats
    until the lease expires — Autopilot must boot with no human at the keyboard.
    """
    raw = str(workspace_path or "").strip()
    if not raw:
        return
    try:
        workspace = str(Path(raw).expanduser().resolve())
    except OSError:
        return
    if not Path(workspace).is_dir():
        return
    home_raw = (
        os.environ.get("CODEX_HOME")
        or os.environ.get("PM_AGENT_HOST_CODEX_HOME")
        or ""
    ).strip()
    try:
        # launchd starts the host with a minimal environment, so an env-only
        # lookup silently no-ops exactly where the seeding matters most.
        codex_home = (Path(home_raw).expanduser().resolve() if home_raw
                      else Path.home() / ".codex")
    except OSError:
        return
    config_path = codex_home / "config.toml"
    if not config_path.parent.is_dir():
        return
    escaped = workspace.replace("\\", "\\\\").replace('"', '\\"')
    header = f'[projects."{escaped}"]'
    try:
        text = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    except OSError:
        return
    if header in text:
        return
    try:
        with open(config_path, "a", encoding="utf-8") as fh:
            fh.write(f'\n{header}\ntrust_level = "trusted"\n')
        print(f"[agent_host] seeded Codex trust for {workspace}", flush=True)
    except OSError as exc:
        print(f"[agent_host] failed to seed Codex trust: {exc}", flush=True)


def _ensure_claude_workspace_trusted(workspace_path: str) -> None:
    """Seed exact-path trust so Connect Claude skips the interactive trust TUI.

    Claude Code asks "Is this a project you trust?" for each new working
    directory, and every Connect run gets a fresh managed workspace. Nobody
    answers that prompt on a headless host, so the runner sits on the dialog
    until its lease expires — the same failure Codex hits above.
    ``--dangerously-skip-permissions`` governs tool permissions and does NOT
    dismiss the folder-trust dialog.

    The config is the operator's live ``~/.claude.json``, so this merges into a
    parsed copy and writes atomically: a partial or clobbering write here would
    break every other Claude session on the machine. An unreadable or malformed
    config is left exactly as-is.
    """
    raw = str(workspace_path or "").strip()
    if not raw:
        return
    try:
        workspace = str(Path(raw).expanduser().resolve())
    except OSError:
        return
    if not Path(workspace).is_dir():
        return
    config_path = Path.home() / ".claude.json"
    try:
        if config_path.is_file():
            config = json.loads(config_path.read_text(encoding="utf-8"))
        else:
            config = {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[agent_host] failed to read Claude config for trust seeding: {exc}",
              flush=True)
        return
    if not isinstance(config, dict):
        print("[agent_host] Claude config is not an object; not seeding trust",
              flush=True)
        return
    projects = config.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    entry = projects.get(workspace)
    if not isinstance(entry, dict):
        entry = {}
    if entry.get("hasTrustDialogAccepted") is True:
        return
    entry["hasTrustDialogAccepted"] = True
    projects[workspace] = entry
    config["projects"] = projects
    temporary = config_path.with_name(f".claude.json.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(config, indent=2), encoding="utf-8")
        os.replace(temporary, config_path)
        print(f"[agent_host] seeded Claude trust for {workspace}", flush=True)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        print(f"[agent_host] failed to seed Claude trust: {exc}", flush=True)


def _connect_mcp_endpoint():
    """Public MCP URL the host already uses for Switchboard Communicate."""
    base = str(os.environ.get("PM_BASE") or "https://plan.taikunai.com").rstrip("/")
    return f"{base}/mcp?{urllib.parse.urlencode({'project': PROJECT})}"


def _connect_codex_mcp_argv(*, verification_runtime=None):
    """Codex overrides required for readable, Switchboard-bound sessions."""
    endpoint = _connect_mcp_endpoint()
    overrides = (
        # The enrolled Host deliberately has an isolated CODEX_HOME containing
        # auth, not the operator's mutable config. Pin the normal agent effort
        # explicitly so Watch does not degrade to a no-reasoning transcript
        # dominated by raw command output.
        "-c", 'model_reasoning_effort="high"',
        "-c", f"mcp_servers.taikun_plan.url={json.dumps(endpoint)}",
        "-c", 'mcp_servers.taikun_plan.bearer_token_env_var='
              '"SWITCHBOARD_CONNECT_SESSION_TOKEN"',
        "-c", "mcp_servers.taikun_plan.required=true",
    )
    if verification_runtime:
        # macOS /etc/zprofile runs path_helper for login shells and can move
        # the Host-proven virtualenv behind /usr/bin.  The child already has
        # the verified PATH; keep Codex shell tools non-login so that exact
        # Capacity-owned environment reaches commands unchanged.
        overrides += ("-c", "allow_login_shell=false")
    return overrides


def _project_python_runtime(execution_context, *, require_isolated=False):
    """Prove the Python runtime inherited by projectplanner Connect sessions.

    Fresh Git workspaces intentionally do not contain the operator checkout's
    ignored ``.venv`` directory.  The enrolled Host already runs inside the
    locked project environment, so expose that exact interpreter to its child
    instead of letting a CLI fall through to macOS ``/usr/bin/python3``.

    This is a Capacity preflight only.  It neither selects work nor changes a
    lifecycle role.  Other repositories are left untouched until they declare
    their own verification profile.
    """
    repository = str((execution_context or {}).get("repository") or "").lower()
    if repository != "6th-element-labs/projectplanner":
        return None
    version = tuple(int(part) for part in sys.version_info[:3])
    if version < (3, 12, 0):
        raise WorkspaceMaterializationError(
            "verification_runtime_unavailable",
            "projectplanner requires the Host's locked Python 3.12+ runtime",
            diagnostic_cause="python_version_unsupported",
            required_python=">=3.12",
            observed_python=".".join(str(part) for part in version),
        )
    executable = Path(sys.executable).expanduser()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise WorkspaceMaterializationError(
            "verification_runtime_unavailable",
            "the Host Python interpreter is not executable",
            diagnostic_cause="python_executable_unavailable",
            required_python=">=3.12",
        )
    if require_isolated and sys.prefix == sys.base_prefix:
        raise WorkspaceMaterializationError(
            "verification_runtime_unavailable",
            "the verification Python is not inside an isolated environment",
            diagnostic_cause="test_environment_not_isolated",
            python_executable=str(executable),
        )
    bin_dir = executable.parent
    return {
        "schema": "switchboard.host_python_runtime.v1",
        "python_version": ".".join(str(part) for part in version),
        # Keep the venv path as the executable instruction. Resolving the
        # symlink would bypass pyvenv.cfg and silently lose locked packages.
        "python_executable": str(executable),
        "python_realpath": str(executable.resolve()),
        "environment": {
            "PATH": os.pathsep.join(filter(None, (
                str(bin_dir), os.environ.get("PATH", "")))),
            **({"VIRTUAL_ENV": str(Path(sys.prefix).resolve())}
               if sys.prefix != sys.base_prefix else {}),
        },
    }


def _verification_failure(cause, message, **details):
    return verification_failure(cause, message, **details)


def _prove_verification_profile(profile, workspace, execution_context, assignment):
    return prove_verification_profile(
        profile,
        workspace,
        execution_context,
        assignment,
        python_runtime_provider=lambda context: _project_python_runtime(
            context, require_isolated=True),
    )


def _issue_connect_session_mcp_token(wake, inventory, runner_session_id):
    """Mint the task principal used by a Connect session's MCP client."""
    result = sb._http("POST", P_DIRECT_SESSION_MCP_TOKEN, {
        "project": _wake_project(wake),
        "wake_id": str(wake.get("wake_id") or ""),
        "host_id": str(inventory.get("host_id") or ""),
        "runner_session_id": str(runner_session_id or ""),
    })
    token = str((result or {}).get("token") or "").strip()
    if (result or {}).get("issued") is not True or not token.startswith("dst-"):
        raise RuntimeError("Connect Switchboard MCP authentication was denied")
    return token


def _agent_host_state_root():
    return Path(os.environ.get(
        "PM_AGENT_HOST_STATE_DIR",
        str(Path.home() / ".local" / "share" / "switchboard-agent-host"),
    )).expanduser()


def connect_workspace_request(wake, inventory):
    """The exact materialize/verify arguments for one Connect wake.

    Materialization, pre-process verification, and revocation all address the
    same private workspace, so they all derive their arguments here.  A
    context-less wake uses the enrolled checkout only as a local git source;
    the resulting binding has the same lifecycle as a contextual workspace.
    """
    policy = wake.get("policy") or {}
    context = dict(policy.get("execution_context") or {})
    lifecycle = dict(policy.get("lifecycle") or {})
    execution_id = str(lifecycle.get("execution_id") or "")
    task_id = str(wake.get("task_id") or "")
    generation = int(lifecycle.get("generation") or 0)
    state_root = _agent_host_state_root()
    project_id = str(context.get("project_id") or _wake_project(wake))
    execution_assignment = dict(policy.get("execution_assignment") or {})
    desired_role = str(
        execution_assignment.get("desired_role")
        or lifecycle.get("role")
        or "implementation"
    ).strip().lower()
    exact_head_role = desired_role in {"review_merge", "remediation"}
    existing_pr_branch = (
        str(lifecycle.get("pr_branch") or "").strip()
        if exact_head_role
        else ""
    )
    if exact_head_role and not existing_pr_branch:
        raise WorkspaceMaterializationError(
            "workspace_pr_branch_missing",
            "review/remediation workspace requires the persisted PR branch",
            role=desired_role,
        )
    compatibility_checkout_sha = ""
    if exact_head_role and not context:
        lifecycle_head = str(lifecycle.get("head_sha") or "").strip().lower()
        assignment_head = str(
            execution_assignment.get("exact_head_sha") or ""
        ).strip().lower()
        if (not re.fullmatch(r"[0-9a-f]{40}", lifecycle_head)
                or lifecycle_head != assignment_head):
            raise WorkspaceMaterializationError(
                "workspace_exact_head_mismatch",
                "lifecycle head and assignment head must agree",
                role=desired_role,
                lifecycle_head_sha=lifecycle_head or None,
                assignment_head_sha=assignment_head or None,
            )
        compatibility_checkout_sha = lifecycle_head
    if context:
        base_sha = str(context.get("base_sha") or "").strip().lower()
        checkout_sha = str(context.get("checkout_sha") or "").strip().lower()
        expected_checkout = base_sha
        if exact_head_role:
            lifecycle_head = str(lifecycle.get("head_sha") or "").strip().lower()
            assignment_head = str(
                execution_assignment.get("exact_head_sha") or ""
            ).strip().lower()
            expected_checkout = assignment_head
            if not (
                re.fullmatch(r"[0-9a-f]{40}", checkout_sha)
                and checkout_sha == lifecycle_head == assignment_head
            ):
                raise WorkspaceMaterializationError(
                    "workspace_exact_head_mismatch",
                    "checkout SHA, lifecycle head, and assignment head must agree",
                    role=desired_role,
                    checkout_sha=checkout_sha or None,
                    lifecycle_head_sha=lifecycle_head or None,
                    assignment_head_sha=assignment_head or None,
                )
        if checkout_sha and checkout_sha != expected_checkout:
            raise WorkspaceMaterializationError(
                "workspace_exact_head_mismatch",
                "execution checkout SHA disagrees with the assigned target",
                role=desired_role,
                checkout_sha=checkout_sha,
                expected_checkout_sha=expected_checkout,
            )
    common = {
        "task_id": task_id,
        "execution_id": execution_id,
        "branch": (
            existing_pr_branch
            or (
                f"agent/{_safe_identity(project_id)}/"
                f"{_safe_identity(task_id)}/"
                f"{_safe_identity(execution_id)}-g{generation}"
            )
        ),
        "workspace_root": os.environ.get(
            "PM_AGENT_HOST_WORKSPACE_ROOT", str(state_root / "workspaces")),
    }
    if context:
        return {
            "execution_context": context,
            **common,
            "cache_root": os.environ.get(
                "PM_AGENT_HOST_REPO_CACHE_ROOT",
                str(state_root / "repository-cache")),
        }
    binding = dict(policy.get("repository_binding") or {})
    expected_project = _wake_project(wake)
    expected_repository = _repository_identity(
        binding.get("repository"), topology=True)
    if (binding.get("schema") != "switchboard.repository_binding.v1"
            or str(binding.get("project") or "") != expected_project
            or str(binding.get("repo_role") or "") != "canonical"
            or not expected_repository):
        raise WorkspaceMaterializationError(
            "project_source_repository_unbound",
            "policy-optional launch requires the project's canonical repository binding",
            project=expected_project,
        )
    roots = dict((inventory or {}).get("project_source_repo_roots") or {})
    source_repo_root = str(
        roots.get(expected_project) or (inventory or {}).get("repo_root") or "")
    actual_repository = _source_origin_identity(source_repo_root)
    if not actual_repository:
        raise WorkspaceMaterializationError(
            "legacy_source_repo_invalid",
            "the selected host source is not a usable Git checkout",
            project=expected_project,
            source_repo_root=source_repo_root or None,
        )
    if actual_repository != expected_repository:
        raise WorkspaceMaterializationError(
            "project_source_repository_unbound",
            "the host has no source checkout bound to the wake's canonical repository",
            project=expected_project,
            expected_repository=expected_repository,
            actual_repository=actual_repository,
        )
    return {
        "project_id": project_id,
        "generation": generation,
        "source_repo_root": source_repo_root,
        "checkout_sha": compatibility_checkout_sha,
        **common,
    }


def _workspace_binding_path(runner_session_id):
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(runner_session_id or ""))
    return _agent_host_state_root() / "workspace-bindings" / f"{safe_id}.json"


def _record_workspace_binding(runner_session_id, workspace, request):
    """Remember which workspace a runner owns so teardown can revoke it later.

    The runner may outlive this daemon process, so the binding is durable rather
    than in-memory: a restarted host must still be able to revoke.
    """
    if not runner_session_id or not workspace:
        return None
    path = _workspace_binding_path(runner_session_id)
    payload = {
        "runner_session_id": str(runner_session_id),
        "workspace_path": str(workspace.path),
        "receipt_path": str(workspace.receipt_path),
        "branch": workspace.branch,
        "head_sha": workspace.head_sha,
        "cache_path": str(workspace.cache_path),
        # Revocation removes directories from a file written by an earlier
        # process. Persist the boundary it is allowed to act inside so a stale
        # or corrupted binding cannot point teardown at an unrelated path.
        "workspace_root": str((request or {}).get("workspace_root") or ""),
        "task_id": str((request or {}).get("task_id") or ""),
        "execution_id": str((request or {}).get("execution_id") or ""),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        print(f"[agent_host] workspace binding not recorded: {exc}", flush=True)
        return None
    return payload


def _revoke_launch_workspace(workspace, runner_session_id, reason):
    """Revoke a workspace whose launch failed, and drop its runner binding."""
    try:
        result = revoke_repository_workspace(
            workspace, reason=reason, quarantine=True)
    except WorkspaceMaterializationError as exc:
        result = {"revoked": False, "error": exc.code}
    except OSError as exc:
        result = {"revoked": False, "error": type(exc).__name__}
    try:
        _workspace_binding_path(runner_session_id).unlink()
    except (FileNotFoundError, OSError):
        pass
    return result


def revoke_runner_workspace(runner_session_id, reason):
    """Deny further writes by a terminated runner's workspace.

    Returns None when this runner never owned an isolated workspace (every
    non-Connect mode), so callers can treat it as an ordinary no-op.
    """
    path = _workspace_binding_path(runner_session_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    root = str(payload.get("workspace_root") or "")
    if not root:
        return {"revoked": False, "error": "workspace_root_missing",
                "reason": str(reason or "")}
    workspace = MaterializedWorkspace(
        Path(str(payload.get("workspace_path") or "")),
        str(payload.get("branch") or ""),
        str(payload.get("head_sha") or ""),
        Path(str(payload.get("cache_path") or "")),
        Path(str(payload.get("receipt_path") or "")),
        {},
        workspace_root=Path(root),
    )
    try:
        result = revoke_repository_workspace(workspace, reason=str(reason or "terminal"))
    except WorkspaceMaterializationError as exc:
        return {"revoked": False, "error": exc.code, "reason": str(reason or "")}
    except OSError as exc:
        # Revocation runs inside the terminal-acknowledgement path. A directory
        # that will not delete must be reported, never allowed to strand a
        # killed runner in Stopping.
        return {"revoked": False, "error": type(exc).__name__,
                "reason": str(reason or "")}
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return {**result, "runner_session_id": str(runner_session_id)}


def connect_execution_generation(wake):
    """The one generation a Connect wake is allowed to execute at."""
    policy = wake.get("policy") or {}
    lifecycle = dict(policy.get("lifecycle") or {})
    assignment = dict(policy.get("execution_assignment") or {})
    generation = int(lifecycle.get("generation") or 0)
    if generation <= 0:
        raise ValueError("connect lifecycle generation is missing")
    if assignment and int(assignment.get("generation") or 0) != generation:
        raise ValueError("connect execution assignment generation mismatch")
    return generation


def require_connect_generation_binding(wake):
    """Refuse a Connect launch whose authorities disagree on the generation."""
    from switchboard.application.commands import execution_context as _context

    policy = wake.get("policy") or {}
    context = dict(policy.get("execution_context") or {})
    if not context:
        raise ValueError("connect execution context is missing")
    binding = dict(policy.get("account_binding") or {})
    try:
        # The runtime itself is already fenced by launch_command against the
        # context's registry name; this gate owns generation and credential.
        return _context.require_generation_binding(
            context,
            generation=connect_execution_generation(wake),
            credential_reference=str(binding.get("credential_reference") or ""),
        )
    except _context.ExecutionContextError as exc:
        raise ValueError(f"connect generation binding refused: {exc.code}") from exc


def launch_command(
        wake, inventory, runner_session_id="", workspace_path="",
        verification_runtime=None):
    """Build the supervisor command for a wake without executing it."""
    sel = wake.get("selector") or {}
    eligible = eligible_runtime(wake, inventory)
    if not eligible:
        raise ValueError("wake is not eligible for this host policy/runtime inventory")
    agent_id = sel.get("agent_id") or sel.get("runtime") or "claude-code"
    lane = sel.get("lane") or ""
    runtime = sel.get("runtime") or eligible.get("runtime") or "claude-code"
    runtime_key = re.sub(r"[^A-Z0-9]+", "_", str(runtime).upper()).strip("_")
    work_mod = os.environ.get(f"PM_AGENT_WORK_MODULE_{runtime_key}", "").strip()
    if not work_mod:
        work_mod = os.environ.get("PM_AGENT_WORK_MODULE", "").strip()
    runtime_markers = {
        "codex": ("codex",),
        "claude-code": ("claude",),
        "cursor": ("cursor",),
    }
    markers = runtime_markers.get(str(runtime), ())
    if work_mod and markers and not any(marker in work_mod.lower() for marker in markers):
        raise ValueError(
            f"work module {work_mod!r} does not match requested runtime {runtime!r}")
    mode = wake_mode(wake, inventory)
    if mode == "refused":
        raise ValueError("wake asks for global claim_next but host policy forbids global work")
    if mode == "connect":
        connect_policy = wake.get("policy") or {}
        assignment_data = dict(connect_policy.get("assignment") or {})
        assignment_schema = assignment_data.pop("schema", "")
        if assignment_schema != "switchboard.connect.assignment.v1":
            raise ValueError("connect assignment schema is invalid")
        limits = assignment_data.get("limits") or {}
        assignment_data["limits"] = ResourceLimits(**limits)
        assignment = Assignment(**assignment_data)
        if assignment.runtime != runtime:
            raise ValueError("connect assignment runtime mismatch")
        # Operator Connect boots an interactive CLI inside the supervised PTY.
        # Mission Bot Codex work is different: its durable mission_key already
        # identifies a coordination-owned one-shot decision, so launch it with
        # ``codex exec`` and let child-process exit be Capacity's terminal
        # signal.  Do not infer process death from claims, messages, or task
        # state. PM_CONNECT_<RT>_ARGS still overrides per host.
        if runtime not in CONNECT_RUNTIME_DEFAULTS:
            # Guessing "<runtime> --prompt" for an unknown runtime launches a
            # process that is not a supported provider CLI and cannot be
            # watched, chatted with, or completed. Refuse the runtime instead.
            raise ValueError(
                f"connect runtime {runtime!r} has no supported provider CLI")
        executable_default, args_default = CONNECT_RUNTIME_DEFAULTS[runtime]
        executable = str(os.environ.get(
            f"PM_CONNECT_{runtime_key}_EXECUTABLE", executable_default)).strip()
        before = tuple(shlex.split(str(os.environ.get(
            f"PM_CONNECT_{runtime_key}_ARGS", args_default))))
        # Host-side Communicate attachment (not Connect assignment content):
        # require the same taikun_plan MCP surface Direct already uses so
        # "via Switchboard" means MCP tools, not improvised REST/curl.
        if runtime == "codex":
            before = before + _connect_codex_mcp_argv(
                verification_runtime=verification_runtime)
            if str((connect_policy.get("lifecycle") or {}).get(
                    "mission_key") or "").strip():
                before = ("exec",) + before
        config = HostRuntimeConfig(
            runtime=runtime,
            provider=assignment.provider,
            executable=executable,
            arguments_before_note=before,
        )
        lifecycle = dict(connect_policy.get("lifecycle") or {})
        execution_assignment = dict(
            connect_policy.get("execution_assignment") or {})
        execution_context = dict(connect_policy.get("execution_context") or {})
        has_execution_context = bool(execution_context)
        if (has_execution_context and execution_context.get("schema")
                != "switchboard.execution_context.v1"):
            raise ValueError("connect execution context contract is invalid")
        if has_execution_context and int(
                execution_context.get("generation") or 0) != int(
                execution_assignment.get("generation") or 0):
            raise ValueError("connect execution context generation mismatch")
        context_runtime = str(
            (execution_context.get("runtime") or {}).get("registry_name") or "")
        if has_execution_context and context_runtime not in {
                runtime, "claude_code" if runtime == "claude-code" else runtime}:
            raise ValueError("connect execution context runtime mismatch")
        if not execution_assignment:
            raise ValueError("connect execution assignment contract is missing")
        # One generation owns the workspace, the provider credential, and the
        # control-plane identity (runner/claim/Work Session/MCP principal). If
        # any of them describes a different generation, or the provider
        # connection was revoked since the wake was queued, nothing launches.
        # A wake WITHOUT an Execution Context is the compatibility source path
        # for projects that have not opted into an execution policy.  It still
        # has the server-owned lifecycle and execution-assignment generation;
        # only provider/SCM context binding remains conditional.
        if has_execution_context:
            require_connect_generation_binding(wake)
        from switchboard.connect.execution_assignment import (
            ExecutionAssignmentError,
            build_execution_assignment,
            require_exact_execution_assignment,
        )
        try:
            expected = build_execution_assignment(
                task_id=str(wake.get("task_id") or ""),
                assignment=assignment_data,
                lifecycle=lifecycle,
                execution_context=connect_policy.get("execution_context") or {},
            )
            require_exact_execution_assignment(execution_assignment, expected)
        except ExecutionAssignmentError as exc:
            raise ValueError(
                "connect execution assignment disagrees with persisted lease: "
                f"{exc.code}") from exc
        now = time.time()
        if not str(workspace_path or "").strip():
            raise ValueError(
                "connect launch requires a verified private workspace")
        spec = build_launch_spec(
            Ack(
                lease_id=str(wake.get("wake_id") or assignment.assignment_id),
                runner_id=runner_session_id or _runner_session_id_for_wake(
                    wake, inventory.get("host_id") or ""),
                assignment=assignment,
                host_id=str(inventory.get("host_id") or ""),
                issued_at=now,
                expires_at=now + assignment.limits.max_runtime_seconds,
                heartbeat_interval_seconds=30,
                last_heartbeat_at=now,
            ),
            config,
            workspace_path=str(workspace_path),
            completion_contract=execution_assignment,
        )
        child = list(spec.argv)
    elif mode == "direct_task":
        if runtime != "codex" or not wake.get("task_id"):
            raise ValueError("direct task assignment requires a task-bound Codex runtime")
        child = [sys.executable, DIRECT_CODEX_SESSION]
    elif mode == "inbox_only":
        idle = os.environ.get("PM_AGENT_HOST_INBOX_IDLE_SECONDS", "6")
        child = [sys.executable, RUN_AGENT, "--runtime", runtime,
                 "--inbox-only", "--idle-seconds", idle]
        if _truthy(os.environ.get("PM_AGENT_HOST_ACK_INBOX_ONLY", "1")):
            child.append("--ack-inbox")
    else:
        child = [sys.executable, RUN_AGENT, "--runtime", runtime, "--max-tasks", "1"]
        if lane:
            child += ["--lanes", lane]
        elif not (inventory.get("policy") or {}).get("allow_global_claim"):
            raise ValueError("global claim_next requires PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM=1")
        idle = os.environ.get("PM_AGENT_HOST_CLAIM_IDLE_SECONDS", "6")
        child += ["--idle-seconds", idle]
        if (wake.get("task_id")
                and (wake.get("policy") or {}).get("require_runner_bind") is True):
            # Task-bound Autopilot wakes must take the exact bootstrap route. Do
            # not rely on an inherited default that can fall through to globally
            # forbidden claim_next for a narrow Agent Host principal.
            child.append("--auto-work-session")
        child += (["--work-module", work_mod] if work_mod else ["--dry"])
    cmd = [sys.executable, SUPERVISOR, "start", "--agent-id", agent_id,
           "--cwd", (spec.cwd if mode == "connect" else inventory["repo_root"])]
    if runner_session_id:
        cmd += ["--runner-session-id", runner_session_id]
    if wake.get("wake_id"):
        cmd += ["--wake-id", str(wake.get("wake_id"))]
    if mode:
        cmd += ["--wake-mode", str(mode)]
    if wake.get("task_id"):
        cmd += ["--task-id", wake.get("task_id")]
    cmd += ["--"] + child
    return cmd, mode


def launch(wake, inventory, runner_session_id="", extra_env=None):
    """Spawn a supervised run_agent for this wake via supervisor.py (the proven CLI). Returns the
    supervisor session record (with runner_session_id, pid) or None on failure."""
    mode = wake_mode(wake, inventory)
    if mode == "cloud_execution":
        selector = wake.get("selector") or {}
        if selector.get("runtime") != "codex":
            return {"started": False, "cloud_session": True, "wake_mode": mode,
                    "reason": "cloud_runtime_unsupported", "failure_class": "invalid_input"}
        count = active_codex_cloud_session_count()
        if count is None:
            return {"started": False, "cloud_session": True, "wake_mode": mode,
                    "reason": "cloud_capacity_readback_unavailable",
                    "failure_class": "broken_connection"}
        rec = launch_codex_cloud_wake(wake, inventory, active_sessions=count)
        rec["host_id"] = inventory.get("host_id")
        return rec
    materialized_workspace = None
    workspace_request = None
    verify_workspace = None
    verification_runtime = None
    verification_toolchain_receipt = None
    mode = wake_mode(wake, inventory)
    workspace_path = ""
    if mode == "connect":
        execution_context = dict(
            (wake.get("policy") or {}).get("execution_context") or {})
        execution_assignment = dict(
            (wake.get("policy") or {}).get("execution_assignment") or {})
        verification_profile = str(
            execution_assignment.get("verification_profile") or ""
        ).strip().lower()
        task_id = str(wake.get("task_id") or "")
        try:
            workspace_request = connect_workspace_request(wake, inventory)
            materialize_workspace = (
                materialize_repository_workspace
                if execution_context else materialize_host_worktree)
            verify_workspace = (
                verify_repository_workspace
                if execution_context else verify_host_worktree)
            # Capacity owns this bounded physical launch operation. One shared
            # deadline covers lock acquisition and every git subprocess, so a
            # timeout unwinds and releases the cache lock instead of orphaning
            # a worker behind this heartbeat loop.
            materialized_workspace = _materialize_for_launch(
                materialize_workspace, workspace_request, wake, inventory)
            workspace_path = str(materialized_workspace.path)
            if verification_profile:
                (
                    verification_runtime,
                    verification_toolchain_receipt,
                ) = _prove_verification_profile(
                    verification_profile,
                    materialized_workspace,
                    execution_context,
                    execution_assignment,
                )
            else:
                # Compatibility behavior for tasks that do not select a test
                # profile: preserve BUG-283's locked shell Python, but never
                # claim a toolchain receipt or substitute an alternate command.
                verification_runtime = _project_python_runtime(execution_context)
        except (WorkspaceMaterializationError, VerificationRuntimeError) as exc:
            failure = {
                "runner_session_id": runner_session_id or None,
                "started": False,
                "wake_mode": mode,
                "host_id": inventory.get("host_id"),
                "runtime": (wake.get("selector") or {}).get("runtime") or "",
                "task_id": task_id,
                "reason": exc.code,
                "failure_class": "failed_gate",
                "provider_error": exc.message,
            }
            evidence_key = (
                "verification_runtime"
                if isinstance(exc, VerificationRuntimeError)
                or exc.code == "verification_runtime_unavailable"
                else "workspace_materialization"
            )
            failure[evidence_key] = exc.as_dict()
            return failure
    cmd, mode = launch_command(
        wake, inventory, runner_session_id=runner_session_id,
        workspace_path=workspace_path,
        verification_runtime=verification_runtime)
    try:
        env = os.environ.copy()
        if mode == "connect":
            assignment = dict((wake.get("policy") or {}).get("assignment") or {})
            execution_assignment = dict(
                (wake.get("policy") or {}).get("execution_assignment") or {})
            execution_context = dict(
                (wake.get("policy") or {}).get("execution_context") or {})
            env.update({
                "SWITCHBOARD_CONNECT_ASSIGNMENT_ID": str(
                    assignment.get("assignment_id") or ""),
                "SWITCHBOARD_CONNECT_PRINCIPAL_REF": str(
                    assignment.get("principal_ref") or ""),
                "SWITCHBOARD_CONNECT_WORK_REF": str(assignment.get("work_ref") or ""),
                "SWITCHBOARD_CONNECT_WORKSPACE_REF": str(
                    assignment.get("workspace_ref") or ""),
                "SWITCHBOARD_CONNECT_LEASE_ID": str(wake.get("wake_id") or ""),
                "SWITCHBOARD_CONNECT_RUNNER_ID": str(runner_session_id or ""),
            })
            encoded_assignment = json.dumps(
                execution_assignment, sort_keys=True, separators=(",", ":"))
            env["SWITCHBOARD_EXECUTION_ASSIGNMENT_JSON"] = encoded_assignment
            env["SWITCHBOARD_COMPLETION_CONTRACT_JSON"] = encoded_assignment
            env["SWITCHBOARD_EXECUTION_CONTEXT_JSON"] = json.dumps(
                execution_context, sort_keys=True, separators=(",", ":"))
            # Never expose the enrolled host bearer to the child.  It only has
            # host-management authority; the session receives an exact,
            # short-lived task principal minted after claim_wake.  The session's
            # pre-configured Switchboard MCP client reads its bearer from
            # PM_MCP_TOKEN (bearer_token_env_var), so the minted principal MUST
            # override the inherited host bearer there — otherwise the child
            # still authenticates as the narrow host and register_agent/claims
            # stay forbidden (the exact BUG-139 symptom).
            session_token = _issue_connect_session_mcp_token(
                wake, inventory, runner_session_id)
            env["SWITCHBOARD_CONNECT_SESSION_TOKEN"] = session_token
            env["PM_MCP_TOKEN"] = session_token
            env.pop("SWITCHBOARD_TOKEN", None)
        env.update({str(k): str(v) for k, v in (extra_env or {}).items()})
        if verification_runtime:
            # Capacity's verified runtime wins over inherited/caller PATH. A
            # test hook or service environment must not reintroduce the system
            # Python fallback after the preflight passed.
            env.update(verification_runtime["environment"])
            env["PM_VERIFICATION_PYTHON"] = str(
                verification_runtime["python_executable"])
        if materialized_workspace:
            # Last gate before any process exists: re-prove the directory the
            # CLI is about to be handed is still the authorized checkout. The
            # supervisor would otherwise chdir into whatever now occupies that
            # path, and a deleted or rewound workspace would surface as an
            # unexplained provider crash instead of a named refusal.
            try:
                materialized_workspace = verify_workspace(**workspace_request)
            except WorkspaceMaterializationError as exc:
                return {
                    "runner_session_id": runner_session_id or None,
                    "started": False,
                    "wake_mode": mode,
                    "host_id": inventory.get("host_id"),
                    "runtime": (wake.get("selector") or {}).get("runtime") or "",
                    "task_id": wake.get("task_id") or "",
                    "reason": exc.code,
                    "failure_class": "failed_gate",
                    "provider_error": exc.message,
                    "workspace_verification": exc.as_dict(),
                }
            env["SWITCHBOARD_WORKSPACE_RECEIPT"] = str(
                materialized_workspace.receipt_path)
            _record_workspace_binding(
                runner_session_id, materialized_workspace, workspace_request)
        connect_runtime = str((wake.get("selector") or {}).get("runtime") or "")
        if workspace_path and mode == "connect":
            if connect_runtime == "codex":
                _ensure_codex_workspace_trusted(workspace_path)
            elif connect_runtime == "claude-code":
                _ensure_claude_workspace_trusted(workspace_path)
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, env=env)
        if out.returncode != 0 or not (out.stdout or "").strip():
            detail = (out.stderr or out.stdout or "supervisor emitted no receipt")[-4000:]
            print(
                f"[agent_host] supervisor start failed rc={out.returncode} "
                f"stderr={detail!r}", flush=True)
            if materialized_workspace:
                _revoke_launch_workspace(
                    materialized_workspace, runner_session_id,
                    "supervisor-start-failed")
            return {
                "runner_session_id": runner_session_id or None,
                "started": False,
                "wake_mode": mode,
                "host_id": inventory.get("host_id"),
                "runtime": (wake.get("selector") or {}).get("runtime") or "",
                "task_id": wake.get("task_id") or "",
                "reason": "supervisor_start_failed",
                "failure_class": "failed_gate",
                "provider_error": detail,
            }
        rec = json.loads(out.stdout)
        if isinstance(rec, dict):
            rec["wake_mode"] = mode
            rec["host_id"] = inventory.get("host_id")
            rec["runtime"] = (wake.get("selector") or {}).get("runtime") or ""
            rec["task_id"] = rec.get("task_id") or wake.get("task_id") or ""
            if materialized_workspace:
                rec["cwd"] = workspace_path
                rec.setdefault("metadata", {})["workspace_receipt"] = (
                    safe_workspace_receipt(materialized_workspace.receipt))
            if verification_runtime:
                rec.setdefault("metadata", {})["verification_runtime"] = {
                    key: value for key, value in verification_runtime.items()
                    if key != "environment"
                }
            if verification_toolchain_receipt:
                rec.setdefault("metadata", {})[
                    "verification_toolchain_receipt"
                ] = verification_toolchain_receipt
        return rec
    except Exception as e:
        if materialized_workspace:
            _revoke_launch_workspace(
                materialized_workspace, runner_session_id,
                "runtime-launch-exception")
        print(f"[agent_host] launch failed: {e}", flush=True)
        return {
            "runner_session_id": runner_session_id or None,
            "started": False,
            "wake_mode": mode,
            "host_id": inventory.get("host_id"),
            "runtime": (wake.get("selector") or {}).get("runtime") or "",
            "task_id": wake.get("task_id") or "",
            "reason": "runtime_launch_exception",
            "failure_class": "failed_gate",
            "provider_error": str(e)[:4000],
        }


def confirm_started(rec, grace_s=4.0):
    """Confirm the launched process is alive after a short grace (proxy for 'runtime came up')."""
    if (rec or {}).get("cloud_session"):
        return bool(rec.get("started") and rec.get("provider_session_id") and rec.get("session_url"))
    pid = (rec or {}).get("pid")
    if not pid:
        return False
    deadline = time.time() + grace_s
    while time.time() < deadline:
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            return False
        time.sleep(0.5)
    return True


def _tail_json_result(log_path):
    """Best-effort parse of a launched job's own last JSON line from its log. Returns
    the parsed dict, or None if the file is missing/empty/unparsable."""
    if not log_path:
        return None
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def register_runner_session(rec, wake, inventory):
    """Publish the supervisor session to Switchboard's central runner registry.

    COORD-34: claimed/watchable registrations must carry task/claim/host/wake/
    work_session bind fields. Incomplete bind returns a typed error payload.
    """
    if not rec or not rec.get("runner_session_id"):
        return None
    policy = wake.get("policy") or {}
    binding = (policy.get("account_binding") or {})
    execution = policy.get("execution_binding") or {}
    assignment = policy.get("assignment") or {}
    lifecycle = policy.get("lifecycle") or {}
    execution_context = policy.get("execution_context") or {}
    connect_assignment = (
        assignment.get("schema") == "switchboard.connect.assignment.v1")
    metadata = {
        "wake_id": wake.get("wake_id"),
        "wake_mode": rec.get("wake_mode"),
        "log_path": rec.get("log_path"),
        "command": rec.get("command"),
        "pty": bool(rec.get("pty")),
        "work_session_id": (
            (rec.get("metadata") or {}).get("work_session_id")
            or binding.get("work_session_id")
            or rec.get("work_session_id")
        ),
        "credential_lease_id": binding.get("credential_lease_id"),
        "provider": assignment.get("provider") or binding.get("provider"),
        "account_affinity_id": binding.get("account_affinity_id"),
        **({
            "connect_assignment": True,
            "assignment_schema": assignment.get("schema"),
            "assignment_id": assignment.get("assignment_id"),
            "principal_ref": assignment.get("principal_ref"),
            "work_ref": assignment.get("work_ref"),
            "workspace_ref": assignment.get("workspace_ref"),
            "execution_id": lifecycle.get("execution_id"),
            "execution_generation": lifecycle.get("generation"),
            "execution_role": lifecycle.get("role"),
            "execution_head_sha": lifecycle.get("head_sha"),
            "lease_epoch": lifecycle.get("fence_epoch"),
            "execution_context_digest": execution_context.get("digest"),
            "execution_context_authority_digest": execution_context.get(
                "authority_digest"),
            "execution_repository": execution_context.get("repository"),
            "execution_default_branch": execution_context.get("default_branch"),
            "execution_base_sha": execution_context.get("base_sha"),
            "execution_checkout_sha": execution_context.get("checkout_sha"),
        } if connect_assignment else {
            "role": assignment.get("role") or lifecycle.get("role") or "implementation",
            "lifecycle_role": assignment.get("role") or lifecycle.get("role") or "implementation",
        }),
        **(rec.get("metadata") or {}),
        "source_sha": (execution.get("source_sha") or assignment.get("source_sha")
                       or lifecycle.get("source_sha")),
        "execution_connection_id": execution.get("execution_connection_id"),
    }
    if connect_assignment:
        # Connect Mac PTYs are native host execution. Without this flag the host
        # renew loop skips the session and the 60s launch lease kills a live Codex.
        metadata["native_host_execution"] = True
        metadata["connect_assignment"] = True
    host_preflight = _host_repo_preflight(rec, inventory, metadata)
    if host_preflight:
        metadata["host_repo_preflight"] = host_preflight
    # Prefer explicit host/<instance-id> from inventory; never invent task-row EC2 ids.
    host_id = inventory.get("host_id") or ""
    reported_cwd = str(rec.get("cwd") or "")
    if not reported_cwd and wake_mode(wake, inventory) != "connect":
        reported_cwd = str(inventory.get("repo_root") or "")
    body = {
        "project": _wake_project(wake),
        "runner_session_id": rec.get("runner_session_id"),
        "host_id": host_id,
        "agent_id": rec.get("agent_id") or (wake.get("selector") or {}).get("agent_id"),
        "runtime": rec.get("runtime") or (wake.get("selector") or {}).get("runtime"),
        "task_id": rec.get("task_id") or wake.get("task_id") or "",
        "claim_id": rec.get("claim_id") or binding.get("claim_id") or "",
        "pid": rec.get("pid"),
        "status": rec.get("status") or "running",
        # A Connect row without a materialized cwd is honestly "not started".
        # Never make its preclaim projection impersonate a process in repo_root.
        "cwd": reported_cwd,
        "control": rec.get("control") or {"tier": "T3", "runner_kill": True,
                                           "managed_process": True},
        "metadata": metadata,
        # Connect and direct CLIs need the same renewable lease; 60s launch TTL
        # was killing live PTYs when a single renew tick was missed.
        "heartbeat_ttl_s": (3600 if rec.get("cloud_session") else
                            180 if (
                                rec.get("wake_mode") in {"direct_task", "connect"}
                                or connect_assignment
                            ) else 60),
    }
    # Use hard POST when this registration claims to be claim-bound / watchable so
    # agent hosts fail closed instead of silently skipping (_try returns None).
    require_bind = bool(
        body.get("claim_id")
        or metadata.get("credential_admission_phase") == "claim_bound"
        or rec.get("require_task_bind")
    )
    if require_bind:
        body["require_task_bind"] = True
        return _require("POST", P_REGISTER_RUNNER, body)
    if connect_assignment:
        return _require("POST", P_REGISTER_RUNNER, body)
    return _try("POST", P_REGISTER_RUNNER, body)


def _host_repo_preflight(rec, inventory, metadata=None):
    """Return a host-attested Git snapshot for a host-local Work Session.

    The coordinator cannot stat a Mac/AWS worker path.  The authenticated Agent
    Host already owns the runner heartbeat, so attach the supervisor's local Git
    snapshot to that same heartbeat and let the coordinator validate the binding.
    """
    rec = dict(rec or {})
    metadata = dict(metadata or rec.get("metadata") or {})
    runner_session_id = str(rec.get("runner_session_id") or "").strip()
    work_session_id = str(
        metadata.get("work_session_id") or rec.get("work_session_id") or "").strip()
    if not runner_session_id or not work_session_id:
        return None
    try:
        result = supervisor_action("snapshot", runner_session_id)
    except Exception:
        return None
    if not isinstance(result, dict) or result.get("error"):
        return None
    snap = dict(result.get("last_snapshot") or result.get("snapshot") or result)
    cwd = str(snap.get("cwd") or rec.get("cwd") or "").strip()
    branch = str(snap.get("branch") or "").strip()
    head_sha = str(snap.get("head_sha") or "").strip().lower()
    status_porcelain = str(snap.get("status_porcelain") or "")
    diff_check = str(snap.get("diff_check") or "")
    findings = []
    if not cwd or not branch or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        findings.append({
            "code": "host_git_snapshot_incomplete",
            "message": "Agent Host could not resolve workspace, branch, and head SHA.",
            "failure_class": "missing_data", "severity": "high", "blocking": True,
        })
    if status_porcelain:
        findings.append({
            "code": "dirty_worktree",
            "message": "Agent Host reports uncommitted workspace changes.",
            "failure_class": "dirty_work_session", "severity": "high", "blocking": True,
        })
    if diff_check:
        findings.append({
            "code": "git_diff_check_failed",
            "message": "Agent Host git diff --check failed.",
            "failure_class": "conflict_markers", "severity": "high", "blocking": True,
        })
    blocking = any(item.get("blocking") for item in findings)
    return {
        "schema": "switchboard.repo_preflight.v1",
        "attestation_schema": "switchboard.agent_host_repo_preflight.v1",
        "source": "agent_host_attestation",
        "captured_at": float(snap.get("captured_at") or time.time()),
        "host_id": str((inventory or {}).get("host_id") or rec.get("host_id") or ""),
        "runner_session_id": runner_session_id,
        "work_session_id": work_session_id,
        "task_id": str(rec.get("task_id") or snap.get("task_id") or "").upper(),
        "agent_id": str(rec.get("agent_id") or snap.get("agent_id") or ""),
        "repo_path": cwd,
        "branch": branch,
        "head_sha": head_sha,
        "origin_url": str(snap.get("origin_url") or ""),
        "upstream": str(snap.get("upstream") or ""),
        "dirty": bool(status_porcelain),
        "conflict_marker_count": 1 if diff_check else 0,
        "findings": findings,
        "verdict": "deny" if blocking else "pass",
        "ok": not blocking,
    }


def report_cloud_usage(rec, wake):
    receipt = (rec or {}).get("usage_receipt") or {}
    if not receipt:
        return None
    return _try("POST", P_TALLY_SPEND, {
        "project": _wake_project(wake),
        "source": receipt.get("source") or "agent_report",
        "confidence": receipt.get("confidence") or "unknown",
        "task_id": receipt.get("task_id") or wake.get("task_id"),
        "claim_id": rec.get("claim_id") or "",
        "agent_id": rec.get("agent_id") or (wake.get("selector") or {}).get("agent_id"),
        "runtime": "codex",
        "provider": "openai",
        "call_site": "cloud_execution",
        "total_tokens": 0,
        "cost_usd": 0,
        "status": "unknown",
        "request_id": f"codex-cloud:{receipt.get('provider_session_id')}",
        "metadata": receipt,
    })


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError):
        return False


def _tcp_port_open(host, port, timeout_s=0.5):
    import socket
    try:
        with socket.create_connection((str(host), int(port)), timeout=float(timeout_s)):
            return True
    except OSError:
        return False


# SIMPLIFY-9: one HostBridgeSession per live runner_session_id — the host
# tunnel WS. PTY I/O is owned by the executor (master_fd + file log); there is
# no LocalPtyRelayBridge / localhost /stream+/control hop on the Watch path.
_HOST_BRIDGES = {}
_HOST_BRIDGES_LOCK = threading.Lock()
# BUG-162: remember the applied host-tunnel ticket expiry so heartbeat mints
# (fresh JWT every ~10s tick) do not tear down a healthy live WebSocket.
_HOST_RELAY_APPLIED = {}
# Rotate when the *applied* ticket has this many seconds (or fewer) remaining.
# Server tickets are ttl=900; Agent Host ticks every ~10s, so 120s leaves many
# renewal chances without flashing Watch Detached on every heartbeat.
HOST_RELAY_ROTATE_SKEW_S = 120.0


def _host_relay_needs_rotation(expires_at, *, now=None, skew_s=None) -> bool:
    """True when a live host tunnel should accept a freshly minted host_url.

    Gate on the *currently applied* ticket's expiry, not the newly minted one
    (every heartbeat returns expires_at≈now+900). Missing/invalid expiry fails
    closed to rotate so a tunnel cannot go dark from a missing ledger entry.
    """
    try:
        exp = float(expires_at or 0)
    except (TypeError, ValueError):
        return True
    if exp <= 0:
        return True
    clock = time.time() if now is None else float(now)
    window = HOST_RELAY_ROTATE_SKEW_S if skew_s is None else float(skew_s)
    return (exp - clock) <= max(0.0, window)


def _publish_host_relay_url(runner_session_id, relay_ws_url) -> None:
    """Best-effort publish for the executor companion (host_relay.url)."""
    if not relay_ws_url:
        return
    try:
        from codex import supervisor as _sup
        relay_path = _sup._session_dir(runner_session_id) / "host_relay.url"
        relay_path.parent.mkdir(parents=True, exist_ok=True)
        relay_path.write_text(relay_ws_url, encoding="utf-8")
    except Exception:
        pass


def _record_applied_host_relay(runner_session_id, relay_ws_url, expires_at) -> None:
    sid = str(runner_session_id or "").strip()
    if not sid or not relay_ws_url:
        return
    try:
        exp = float(expires_at or 0)
    except (TypeError, ValueError):
        exp = 0.0
    # Server host tickets default to ttl=900. When a caller attaches without an
    # expires_at, assume a full lifetime so we do not immediately re-enter the
    # rotate path on every heartbeat (needs_rotation(0) is True by design for
    # *missing* ledger rows, not for a successfully applied URL).
    if exp <= 0:
        exp = time.time() + 900.0
    _HOST_RELAY_APPLIED[sid] = {"url": str(relay_ws_url), "expires_at": exp}


def _drop_host_bridge(runner_session_id):
    sid = str(runner_session_id or "").strip()
    with _HOST_BRIDGES_LOCK:
        session = _HOST_BRIDGES.pop(sid, None)
        _HOST_RELAY_APPLIED.pop(sid, None)
    if session is not None:
        try:
            session.stop()
        except Exception:
            pass


def _ensure_host_bridge(*, runner_session_id, host_id, binding, public_base,
                         host_relay_url="", master_fd=None, child_pid=0,
                         log_path="", expires_at=None, force_rotate=False):
    """Idempotently ensure a live host tunnel for this session.

    Starting *is* opening: dial /pty/host immediately. Optional master_fd makes
    this process the executor (PTY I/O + stdout.log). Re-entrant across
    poll-loop iterations: a healthy existing bridge is a no-op; a dead one is
    replaced. No localhost stream/control URLs are required.

    BUG-162: a freshly minted ``host_relay_url`` only rotates an already-live
    tunnel when the *applied* ticket is within ``HOST_RELAY_ROTATE_SKEW_S`` of
    expiry (or unknown). Mid-lifetime heartbeat mints must not close the WS.
    Pass ``force_rotate=True`` for an explicit companion refresh request.
    """
    relay_ws_url = str(host_relay_url or "").strip()
    sid = str(runner_session_id or "").strip()
    with _HOST_BRIDGES_LOCK:
        existing = _HOST_BRIDGES.get(sid)
        if existing is not None and existing.is_alive():
            if relay_ws_url:
                applied = _HOST_RELAY_APPLIED.get(sid) or {}
                if force_rotate or _host_relay_needs_rotation(applied.get("expires_at")):
                    # Ticket renewal must reach an already-running executor.
                    # The companion watches host_relay.url and reconnects; an
                    # in-process bridge rotates directly via update_relay_url.
                    _publish_host_relay_url(sid, relay_ws_url)
                    try:
                        existing.update_relay_url(relay_ws_url)
                    except Exception:
                        pass
                    _record_applied_host_relay(sid, relay_ws_url, expires_at)
            return existing
    if existing is not None:
        _drop_host_bridge(sid)

    try:
        from switchboard.application import runner_pty_relay as pty_relay
        from codex.pty_host_ws_client import open_host_bridge
    except ModuleNotFoundError:
        _root = os.path.abspath(os.path.join(_HERE, ".."))
        if os.path.join(_root, "src") not in sys.path:
            sys.path.insert(0, os.path.join(_root, "src"))
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        from switchboard.application import runner_pty_relay as pty_relay
        from codex.pty_host_ws_client import open_host_bridge

    minted_expires_at = expires_at
    if not relay_ws_url:
        # Legacy/in-process compatibility. Real enrolled hosts receive a
        # server-minted one-session URL in the claimed control request because
        # they must never possess the server relay signing secret.
        host_ticket, host_payload = pty_relay.mint_host_tunnel_ticket(
            binding, ttl_seconds=3600)
        relay_ws_url = pty_relay.public_host_relay_url(
            public_base, runner_session_id, host_ticket)
        relay_ws_url = relay_ws_url + "&" + urllib.parse.urlencode({"host_id": host_id})
        if minted_expires_at is None:
            minted_expires_at = host_payload.get("exp")

    # Publish the host relay URL for the executor companion (same Mac/AWS binary)
    # so it can dial without a localhost HTTP hop. The companion owns master_fd
    # and is the single outbound WS speaker when master_fd is not in-process.
    _publish_host_relay_url(sid, relay_ws_url)

    session = open_host_bridge(
        runner_session_id=runner_session_id,
        relay_ws_url=relay_ws_url,
        master_fd=master_fd,
        child_pid=int(child_pid or 0),
        log_path=str(log_path or ""),
        on_close=lambda reason: _drop_host_bridge(runner_session_id),
    )
    with _HOST_BRIDGES_LOCK:
        _HOST_BRIDGES[sid] = session
        _record_applied_host_relay(sid, relay_ws_url, minted_expires_at)
    return session


def supervisor_action(action, runner_session_id, options=None):
    options = options or {}
    if action == "snapshot":
        cmd = [sys.executable, SUPERVISOR, "snapshot", runner_session_id]
    elif action == "health":
        cmd = [sys.executable, SUPERVISOR, "status", runner_session_id]
    elif action == "logs":
        cmd = [sys.executable, SUPERVISOR, "snapshot", runner_session_id]
    elif action == "kill":
        cmd = [sys.executable, SUPERVISOR, "kill", runner_session_id,
               "--grace-seconds", str(options.get("grace_seconds") or 5.0),
               "--signal", options.get("signal") or "TERM"]
    elif action == "open":
        status_cmd = [sys.executable, SUPERVISOR, "status", runner_session_id]
        try:
            out = subprocess.run(status_cmd, capture_output=True, text=True, timeout=15)
            if out.returncode != 0:
                return {"error": "supervisor_failed", "stderr": (out.stderr or "")[-4000:]}
            meta = json.loads(out.stdout or "{}")
        except Exception as e:
            return {"error": type(e).__name__, "message": str(e)}
        control = meta.get("control") or {}
        public_base = str(
            os.environ.get("PM_RUNNER_PTY_RELAY_PUBLIC_BASE")
            or os.environ.get("PM_SWITCHBOARD_PUBLIC_BASE")
            or ""
        ).rstrip("/")
        # SIMPLIFY-9/WATCH-11: starting IS opening. Watch attaches only through
        # the Switchboard relay; the retired host-local HTTP transport is not a
        # readiness or fallback path.
        pty_alive = bool(meta.get("pty") and control.get("runner_open") and meta.get("alive"))
        if not pty_alive:
            return {
                "error": "not_supported",
                "reason": "runner_open requires a live PTY-backed local session",
            }
        host_id = str(meta.get("host_id") or os.environ.get("PM_HOST_ID") or "")
        relay_url = ""
        ticket = None
        expires_at = 0.0

        def _open_fail_closed(error, reason):
            # The relay is the only browser transport; relay failures fail closed.
            return {
                "error": error,
                "reason": reason,
                "failure_class": "hidden_fallback",
                "opened": False,
                "runner_session_id": runner_session_id,
                "transport": None,
                "browser_safe": False,
                "relay_required": True,
                "capabilities": {"stream": "denied", "open": "denied"},
            }

        if public_base:
            try:
                from switchboard.application import runner_pty_relay as pty_relay
                from switchboard.domain import runner_pty as pty_domain
            except ModuleNotFoundError:
                _root = os.path.abspath(os.path.join(_HERE, ".."))
                if _root not in sys.path:
                    sys.path.insert(0, os.path.join(_root, "src"))
                from switchboard.application import runner_pty_relay as pty_relay
                from switchboard.domain import runner_pty as pty_domain
            if pty_relay.is_loopback_url(public_base):
                return {
                    "error": "not_supported",
                    "reason": "runner_open requires a non-loopback relay public base",
                }
            else:
                binding = {
                    "tenant_id": str(options.get("tenant_id") or meta.get("tenant_id") or "tenant/default"),
                    "user_id": str(options.get("user_id") or meta.get("user_id") or "operator"),
                    "project_id": str(options.get("project_id") or options.get("project")
                                      or os.environ.get("PM_PROJECT") or "switchboard"),
                    "task_id": str(options.get("task_id") or meta.get("task_id") or "unbound"),
                    "claim_id": str(options.get("claim_id") or meta.get("claim_id") or "unbound"),
                    "work_session_id": str(
                        options.get("work_session_id")
                        or (meta.get("metadata") or {}).get("work_session_id")
                        or meta.get("work_session_id")
                        or "unbound"),
                    "runner_session_id": runner_session_id,
                    "host_id": host_id or "host/unknown",
                    "wake_id": str(
                        options.get("wake_id")
                        or (meta.get("metadata") or {}).get("wake_id")
                        or meta.get("wake_id")
                        or "unbound"),
                    "execution_connection_id": str(
                        options.get("execution_connection_id")
                        or meta.get("execution_connection_id")
                        or "execconn/unspecified"),
                    "source_sha": str(options.get("source_sha") or meta.get("source_sha") or "unknown"),
                    "permission_profile": str(
                        options.get("permission_profile") or "operator_watch"),
                }
                server_relay = _fresh_server_relay(
                    options.get("server_relay"), runner_session_id, host_id)
                host_relay_url = str(server_relay.get("host_url") or "")
                browser_relay_url = str(server_relay.get("browser_url") or "")
                if isinstance(server_relay.get("binding"), dict):
                    binding = dict(server_relay["binding"])
                try:
                    if host_relay_url and browser_relay_url:
                        relay_url = browser_relay_url
                        expires_at = float(server_relay.get("expires_at") or expires_at)
                        ticket = None
                    else:
                        if server_relay.get("error"):
                            raise RuntimeError(str(server_relay.get("error")))
                        scopes = options.get("scopes") or [
                            "watch", "input", "resize", "signal"]
                        relay_ticket, relay_payload = pty_relay.mint_capability_ticket(
                            binding, scopes,
                            ttl_seconds=int(options.get("ttl_seconds") or 900))
                        relay_url = pty_relay.public_relay_url(
                            public_base, runner_session_id, relay_ticket)
                        expires_at = float(relay_payload.get("exp") or expires_at)
                        ticket = relay_ticket
                    transport = pty_domain.TRANSPORT_SWITCHBOARD_PTY_RELAY
                except Exception as mint_exc:
                    return _open_fail_closed(
                        "relay_mint_failed", str(mint_exc) or type(mint_exc).__name__)
                try:
                    # SIMPLIFY-9: dial host tunnel immediately (no localhost stream).
                    # Hub buffers until the executor owning master_fd attaches.
                    _ensure_host_bridge(
                        runner_session_id=runner_session_id,
                        host_id=host_id,
                        binding=binding,
                        public_base=public_base,
                        host_relay_url=host_relay_url,
                        log_path=str(meta.get("log_path") or ""),
                        child_pid=int(meta.get("pid") or 0),
                        expires_at=server_relay.get("expires_at") or expires_at,
                    )
                except Exception as bridge_exc:
                    return _open_fail_closed(
                        "host_bridge_failed", str(bridge_exc) or type(bridge_exc).__name__)
        else:
            return {
                "error": "not_supported",
                "reason": "runner_open requires a non-loopback relay public base",
            }
        metadata = {
            "pty": True,
            "stream_url": relay_url,
            "stream_ticket_exp": expires_at,
            "transport": transport,
            "browser_safe": True,
            "relay_required": False,
        }
        if relay_url:
            metadata["relay_url"] = relay_url
            try:
                from switchboard.application.runner_pty_relay import (
                    sanitize_browser_stream_metadata,
                )
            except ModuleNotFoundError:
                sanitize_browser_stream_metadata = None
            if sanitize_browser_stream_metadata is not None:
                metadata = sanitize_browser_stream_metadata(
                    metadata, relay_url=relay_url)
        return {
            "opened": True,
            "runner_session_id": runner_session_id,
            "transport": transport,
            "stream_url": relay_url,
            "relay_url": relay_url or None,
            "ticket": ticket,
            "expires_at": expires_at,
            "browser_safe": True,
            "relay_required": False,
            "capabilities": {"stream": "supported", "open": "supported"},
            "metadata": metadata,
        }
    elif action == "inject":
        return {
            "error": "not_supported",
            "reason": "runner input is delivered through the Switchboard PTY relay",
            "runner_session_id": runner_session_id,
            "capabilities": {"inject": "denied", "relay_input": "supported"},
        }
    else:
        return {"error": f"unsupported runner action {action}"}
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return {"error": "supervisor_failed", "stderr": out.stderr[-4000:]}
        data = json.loads(out.stdout or "{}")
        if action == "health":
            started = data.get("started_at")
            data["health"] = {
                "status": data.get("status") or "unknown",
                "alive": bool(data.get("alive")),
                "uptime_seconds": max(0.0, time.time() - float(started)) if started else None,
            }
        elif action == "logs":
            snap = data.get("last_snapshot") or {}
            data["logs"] = {"log_tail": snap.get("log_tail") or "", "log_path": data.get("log_path")}
        return data
    except Exception as e:
        return {"error": type(e).__name__, "message": str(e)}


def handle_runner_controls(inventory):
    """Consume pending snapshot/kill requests for runner sessions hosted here."""
    host_id = inventory["host_id"]
    listed = _try(
        "GET",
        f"{P_LIST_RUNNER_CONTROLS}?project={PROJECT}&status=pending&host_id={host_id}",
    ) or {}
    requests = listed.get("requests") or []
    handled = []
    for req in requests:
        req_id = req.get("request_id")
        claimed = _try("POST", P_CLAIM_RUNNER_CONTROL,
                       {"project": PROJECT, "host_id": host_id, "request_id": req_id})
        if not claimed or not claimed.get("claimed"):
            continue
        req = claimed.get("request") or req
        action = req.get("action")
        result = supervisor_action(action, req.get("runner_session_id"), req.get("options") or {})
        snapshot = result.get("last_snapshot") or result.get("snapshot") or {}
        if action == "health" and not snapshot:
            snapshot = {"captured_at": time.time(), "source": "supervisor_status",
                        "status": result.get("status"), "alive": result.get("alive"),
                        "health": result.get("health") or {}}
        if action == "snapshot" and not snapshot:
            snapshot = result
        if action == "logs" and not snapshot:
            snapshot = {"captured_at": time.time(), "source": "supervisor_logs",
                        "log_tail": (result.get("logs") or {}).get("log_tail") or "",
                        "log_path": (result.get("logs") or {}).get("log_path")}
        if action == "open" and result.get("opened"):
            open_meta = dict(result.get("metadata") or {})
            # Browser-facing registration must never publish loopback stream URLs.
            try:
                from switchboard.application.runner_pty_relay import (
                    sanitize_browser_stream_metadata,
                )
            except ModuleNotFoundError:
                sanitize_browser_stream_metadata = lambda meta, relay_url="": dict(meta or {})  # noqa: E731
            browser_meta = sanitize_browser_stream_metadata(
                {
                    "stream_url": result.get("stream_url"),
                    "relay_url": result.get("relay_url") or open_meta.get("relay_url"),
                    "stream_ticket_exp": result.get("expires_at"),
                    "transport": result.get("transport"),
                    "browser_safe": result.get("browser_safe"),
                    "relay_required": result.get("relay_required"),
                    "pty": True,
                },
                relay_url=str(result.get("relay_url") or open_meta.get("relay_url") or ""),
            )
            safe_stream_url = browser_meta.get("stream_url")
            snapshot = {
                "captured_at": time.time(),
                "source": "runner_open",
                "stream_url": safe_stream_url,
                "transport": result.get("transport"),
                "expires_at": result.get("expires_at"),
                "browser_safe": result.get("browser_safe"),
                "relay_required": result.get("relay_required"),
                "pty": True,
            }
            # Advertise stream coordinates on the central runner_session metadata.
            _try("POST", P_REGISTER_RUNNER, {
                "project": PROJECT,
                "runner_session_id": req.get("runner_session_id"),
                "host_id": host_id,
                "status": "running",
                "control": {"tier": "T3", "runner_kill": True, "managed_process": True,
                            "runner_open": True, "runner_inject": True, "runner_logs": True},
                "metadata": browser_meta,
                "heartbeat_ttl_s": 60,
            })
        if action == "inject" and result.get("injected"):
            snapshot = {
                "captured_at": time.time(),
                "source": "runner_inject",
                "runner_session_id": result.get("runner_session_id"),
                "task_id": result.get("task_id"),
                "kind": result.get("kind"),
                "bytes_written": result.get("bytes_written"),
            }
        status = "failed" if result.get("error") else "completed"
        if action == "kill" and status == "completed":
            # UI-24: deterministic cleanup — no orphan host tunnel outliving
            # the runner it was pumping bytes for.
            _drop_host_bridge(req.get("runner_session_id"))
            revoked = revoke_runner_workspace(
                req.get("runner_session_id"),
                str((req.get("options") or {}).get("reason") or "runner_killed"))
            if revoked:
                result = {**result, "workspace_revoked": bool(revoked.get("revoked"))}
        _try("POST", P_COMPLETE_RUNNER_CONTROL,
             {"project": PROJECT, "host_id": host_id, "request_id": req_id,
              "status": status, "result": result, "snapshot": snapshot})
        handled.append({"request_id": req_id, "action": action, "status": status,
                        "runner_session_id": req.get("runner_session_id")})
    return handled


def _drain_query(path, *, project=None, **query):
    return f"{path}?{urllib.parse.urlencode({
        'project': str(project or PROJECT), **query})}"


def _drain_runners(host_id, recover_stale_local=True, *, project=None,
                   include_local_only=True):
    """Join supervisor truth to only the central rows this tick can act on.

    A long-lived personal host can accumulate thousands of stale historical
    runner rows.  Downloading all of them before renewing a handful of live
    local PTYs makes the heartbeat itself miss its lease.  Recovery therefore
    asks for stale rows only for task ids that the local supervisor says are
    alive.  The graceful-drain caller opts out and fetches only centrally-live
    rows for the host.
    """
    local_inventory_available = False
    try:
        out = subprocess.run(
            [sys.executable, SUPERVISOR, "list"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            inventory = json.loads(out.stdout or "{}")
            local = (
                inventory if isinstance(inventory, list)
                else inventory.get("sessions") or []
            )
            local_inventory_available = isinstance(local, list)
        else:
            local = []
    except Exception:
        local = []
    project_id = str(project or PROJECT)
    sessions = []
    if recover_stale_local:
        live_task_ids = sorted({
            str(row.get("task_id") or "") for row in local
            if row.get("alive") is True and str(row.get("task_id") or "")
        })
        for task_id in live_task_ids:
            result = _try("GET", _drain_query(
                P_LIST_RUNNERS, host_id=host_id, task_id=task_id,
                include_stale="true", project=project_id)) or {}
            rows = result.get("sessions") or result.get("runner_sessions") or []
            if isinstance(rows, list):
                sessions.extend(rows)
        # A completed CLI can disappear from the supervisor inventory before
        # the next Host tick. Fetch only centrally nonterminal rows still owed
        # a host receipt: an unacknowledged completion handoff or a server-owned
        # lease surrender. This is deliberately not a scan of every stale
        # runner.
        pending = _try("GET", _drain_query(
            P_LIST_RUNNERS, host_id=host_id, include_stale="true",
            pending_completion="true", limit="1", project=project_id)) or {}
        rows = pending.get("sessions") or pending.get("runner_sessions") or []
        if isinstance(rows, list):
            sessions.extend({**dict(row), "_pending_completion": True}
                            for row in rows)
    else:
        result = _try("GET", _drain_query(
            P_LIST_RUNNERS, host_id=host_id, include_stale="false",
            project=project_id)) or {}
        rows = result.get("sessions") or result.get("runner_sessions") or []
        sessions = rows if isinstance(rows, list) else []
    local_by_id = {row.get("runner_session_id"): dict(row) for row in local
                   if row.get("runner_session_id")}
    merged = dict(local_by_id) if include_local_only else {}
    for row in sessions:
        runner_id = row.get("runner_session_id")
        if runner_id:
            local_row = local_by_id.get(runner_id, {})
            combined = {**local_row, **dict(row)}
            combined["_host_project"] = project_id
            if local_row.get("alive") is True:
                # Central identity/claim state is authoritative, but only the
                # local supervisor can report the live PTY transport.  Repair
                # an older preclaim placeholder on every daemon tick.
                for key in ("pty", "streamer_pid", "log_path", "pid", "alive"):
                    if local_row.get(key) not in (None, ""):
                        combined[key] = local_row.get(key)
                combined["metadata"] = {
                    **dict(row.get("metadata") or {}),
                    **{key: local_row.get(key) for key in
                       ("pty",)
                       if local_row.get(key) not in (None, "")},
                }
                combined["control"] = {
                    **dict(row.get("control") or {}),
                    **dict(local_row.get("control") or {}),
                }
            elif (
                local_inventory_available
                and runner_id not in local_by_id
                and (dict(row.get("metadata") or {}).get("completion_handoff") or {})
            ):
                # A successful supervisor inventory is Capacity truth. If an
                # exact pending-completion runner is absent, its managed process
                # has exited or was forgotten; report that terminal observation
                # through the normal durable receipt path. Never infer death
                # when the supervisor inventory itself was unavailable.
                combined["alive"] = False
            merged[runner_id] = combined
    for row in merged.values():
        row.setdefault("_host_project", project_id)
    return list(merged.values())


def _drain_runner_projects(inventory):
    """Join live runners everywhere while bounding stale receipt maintenance."""
    global _PENDING_COMPLETION_PROJECT_CURSOR

    host_id = str((inventory or {}).get("host_id") or "")
    projects = list(_host_projects(inventory))
    if not projects:
        return []
    start = _PENDING_COMPLETION_PROJECT_CURSOR % len(projects)
    projects = projects[start:] + projects[:start]
    _PENDING_COMPLETION_PROJECT_CURSOR = (
        start + _PENDING_COMPLETION_RETRIES_PER_TICK
    ) % len(projects)
    pending_remaining = _PENDING_COMPLETION_RETRIES_PER_TICK
    rows = []
    for project in projects:
        if project == PROJECT:
            project_rows = _drain_runners(host_id)
        else:
            # Local-only supervisor rows do not carry a project identity. Include
            # them once on the primary project; extra projects require a matching
            # central row before this host may renew anything there.
            project_rows = _drain_runners(
                host_id, project=project, include_local_only=False)
        for row in project_rows:
            if row.get("_pending_completion") is True:
                if pending_remaining <= 0:
                    continue
                pending_remaining -= 1
            rows.append({**row, "_host_project": project})
    return rows


# SIMPLIFY-18: the host shares the server's one terminal vocabulary. The
# release bundle ships src/, so there is no second spelling to drift.
from switchboard.domain.execution_liveness import (
    TERMINAL_EXECUTION_STATES as _TERMINAL_RUNNER_STATES)


def _positive_seconds(env_name, default):
    try:
        return max(0.0, float(os.environ.get(env_name, str(default)) or default))
    except (TypeError, ValueError):
        return float(default)


def _runner_last_output_at(session):
    """Return durable local PTY activity without reading or exposing its log."""
    metadata = dict(session.get("metadata") or {})
    log_path = str(session.get("log_path") or metadata.get("log_path") or "").strip()
    if log_path:
        try:
            return float(os.stat(log_path).st_mtime)
        except OSError:
            pass
    return _runner_timestamp(session.get("started_at"))


def _runner_log_path(session):
    metadata = dict(session.get("metadata") or {})
    return str(session.get("log_path") or metadata.get("log_path") or "").strip()


def _runner_output_bytes(session):
    log_path = _runner_log_path(session)
    if not log_path:
        return None
    try:
        return int(os.stat(log_path).st_size)
    except OSError:
        return None


def _runner_log_tail(session, limit=4000):
    """Return a small clean PTY tail for progress-fault evidence (WATCH-19)."""
    log_path = _runner_log_path(session)
    if not log_path:
        return ""
    try:
        data = Path(log_path).read_bytes()
        return data[-limit:].decode("utf-8", errors="replace")
    except OSError:
        return ""


def _runner_cpu_percent(session):
    """Best-effort CPU sample for progress-fault evidence; never raises."""
    try:
        pid = int(session.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        import psutil  # optional on host images
        return float(psutil.Process(pid).cpu_percent(interval=0.0))
    except Exception:
        return None


def _runner_progress_metadata(session, *, include_log_tail=False):
    """Progress signals beside liveness (WATCH-19).

    Routine renewals carry lightweight signals only. A 4KB PTY ``log_tail`` on
    every heartbeat turned Capacity renewal into a heavy Communication payload
    and caused control-plane timeouts to look like dead leases (ADR-0008 C2).
    Operators still get tails via Watch/attention paths that ask for them.
    """
    payload = {
        "last_output_at": _runner_last_output_at(session),
    }
    if include_log_tail:
        payload["log_tail"] = _runner_log_tail(session)
    output_bytes = _runner_output_bytes(session)
    if output_bytes is not None:
        payload["output_bytes"] = output_bytes
    cpu = _runner_cpu_percent(session)
    if cpu is not None:
        payload["cpu_percent"] = cpu
    return payload


def _runner_timestamp(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def expire_runner_leases(inventory, *, now=None):
    """Enforce the single process-stop clock: the renewable runner lease."""
    now = time.time() if now is None else float(now)
    host_id = str((inventory or {}).get("host_id") or "")
    outcomes = _drain_pending_stop_receipts(host_id)
    for session in _drain_runners(host_id):
        metadata = dict(session.get("metadata") or {})
        surrendered = bool(metadata.get("lease_surrender"))
        # ADR-0008 C2: only surrender or true lease expiry may stop a process.
        # A stale flag from a missed/timeout heartbeat must NOT SIGTERM a still-
        # alive Connect/native Codex — that is Capacity impersonating from a
        # Communication failure. Local process exit is terminalized by
        # renew_live_direct_runners; surrender remains the explicit stop clock
        # for complete_claim / terminal-task make_lease_due (BUG-175).
        native_or_connect = (
            metadata.get("native_host_execution") is True
            or metadata.get("connect_assignment") is True
            or str(session.get("wake_mode") or metadata.get("wake_mode") or "")
            == "connect"
        )
        if session.get("alive") is True and native_or_connect and not surrendered:
            continue
        if session.get("alive") is not True or not (
                session.get("stale") or surrendered):
            continue
        runner_id = str(session.get("runner_session_id") or "")
        task_id = str(session.get("task_id") or "")
        surrender_authority = str(
            (metadata.get("lease_surrender") or {}).get("authority") or "")
        terminal_surrender = surrender_authority in {
            "terminal_task", "completion_owner",
        }
        reason = (
            "terminal_lease_surrendered" if terminal_surrender
            else ("runner_lease_surrendered" if surrendered
                  else "runner_lease_expired"))
        outcome = {"runner_session_id": runner_id, "task_id": task_id,
                   "reason": reason}
        stop_reason = (
            "terminal lease surrendered" if reason == "terminal_lease_surrendered"
            else ("runner lease surrendered" if reason == "runner_lease_surrendered"
                  else "runner heartbeat lease expired"))
        stopped = supervisor_action("kill", runner_id, {
            "reason": stop_reason, "task_id": task_id})
        ok = bool(stopped and not stopped.get("error") and stopped.get("alive") is not True)
        if ok:
            _drop_host_bridge(runner_id)
            # Teardown is part of stopping: an isolated workspace must not
            # outlive the runner that leased it, or a surviving child could keep
            # writing into an execution the control plane already ended.
            revoked = revoke_runner_workspace(runner_id, reason)
            if revoked:
                outcome["workspace_revoked"] = bool(revoked.get("revoked"))
            receipt = {
                "project": PROJECT, "runner_session_id": runner_id,
                "host_id": host_id, "task_id": task_id,
                "claim_id": session.get("claim_id") or "",
                "agent_id": session.get("agent_id") or f"codex/{task_id}",
                "status": ("stopped" if reason == "terminal_lease_surrendered"
                           else "expired"),
                "metadata": {
                    **metadata,
                    "terminalized_by": (
                        "terminal_lease_surrendered"
                        if reason == "terminal_lease_surrendered"
                        else "runner_lease_expiry"),
                    **({"terminal_lease_surrendered_at": now}
                       if reason == "terminal_lease_surrendered"
                       else {"lease_expired_at": now,
                             "failure_reason": stop_reason}),
                },
            }
            # The process death and central acknowledgement are separate durable
            # steps.  Persist first so a network loss or daemon restart cannot
            # strand a successfully killed execution in Stopping forever.
            _persist_pending_stop_receipt(receipt)
            terminal = _try("POST", P_HEARTBEAT_RUNNER, receipt)
            terminal_ok = bool(
                terminal
                and not terminal.get("error")
                and not terminal.get("error_code"))
            if terminal_ok:
                _delete_pending_stop_receipt(runner_id)
            if reason == "terminal_lease_surrendered":
                outcome["terminalized"] = terminal_ok
            else:
                outcome["expired"] = terminal_ok
        else:
            if reason == "terminal_lease_surrendered":
                outcome["terminalized"] = False
            else:
                outcome["expired"] = False
            outcome["error"] = (stopped or {}).get("error")
        outcomes.append(outcome)
    return outcomes


def _pending_stop_receipt_dir():
    root = Path(os.environ.get("PM_RUNNER_DIR", ".switchboard/runner")).resolve()
    path = root / "_pending_stops"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pending_stop_receipt_path(runner_session_id):
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(runner_session_id or ""))
    return _pending_stop_receipt_dir() / f"{safe_id}.json"


def _persist_pending_stop_receipt(receipt):
    path = _pending_stop_receipt_path(receipt.get("runner_session_id"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _delete_pending_stop_receipt(runner_session_id):
    try:
        _pending_stop_receipt_path(runner_session_id).unlink()
    except FileNotFoundError:
        pass


def _drain_pending_stop_receipts(host_id):
    """Retry exact terminal acknowledgements even after local process removal."""
    outcomes = []
    paths = _pending_receipt_paths(_pending_stop_receipt_dir())
    limit = _pending_receipt_replay_limit()
    for path in paths:
        if len(outcomes) >= limit:
            break
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _archive_pending_receipt(
                path, kind="runner_stop",
                response={"error_code": "invalid_receipt_json",
                          "http_status": 400})
            continue
        if str(receipt.get("host_id") or "") != str(host_id or ""):
            continue
        terminal = _require("POST", P_HEARTBEAT_RUNNER, receipt)
        ok = bool(
            terminal
            and not terminal.get("error")
            and not terminal.get("error_code")
        )
        if ok:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        elif _receipt_refusal_is_irrecoverable(terminal):
            _archive_pending_receipt(
                path, kind="runner_stop", response=terminal)
        else:
            path.touch()
        outcomes.append({
            "runner_session_id": receipt.get("runner_session_id"),
            "task_id": receipt.get("task_id"),
            "reason": "pending_terminal_ack_retry",
            "expired": ok,
            "archived": (
                not ok and _receipt_refusal_is_irrecoverable(terminal)),
            "error": None if ok else _safe_receipt_error(terminal),
        })
    return outcomes


def converge_terminal_task_runners(inventory, heartbeat):
    """Refuse legacy terminal-task kill directives for a live process.

    Kept temporarily as a compatibility boundary while old servers drain.
    Only an already-exited runner may be acknowledged; lease expiry owns kills.
    """
    host_id = str((inventory or {}).get("host_id") or "")
    cleanup = (heartbeat or {}).get("terminal_runner_cleanup") or {}
    directives = cleanup.get("sessions") or []
    outcomes = []
    for directive in directives if isinstance(directives, list) else []:
        runner_session_id = str(directive.get("runner_session_id") or "")
        task_id = str(directive.get("task_id") or "")
        if not runner_session_id or not task_id:
            continue
        health = supervisor_action("health", runner_session_id)
        alive = bool(health and not health.get("error") and health.get("alive"))
        killed = ({"status": "observed_only", "alive": alive,
                   "error": "lease expiry is the only kill authority"}
                  if alive else {"status": "already_exited", "alive": False})
        kill_ok = not killed.get("error") and killed.get("alive") is not True
        if kill_ok:
            _drop_host_bridge(runner_session_id)
        terminal = None
        if kill_ok:
            terminal_status = (
                "completed" if directive.get("task_status") == "Done" else "cancelled"
            )
            terminal = _try("POST", P_HEARTBEAT_RUNNER, {
                "project": PROJECT,
                "runner_session_id": runner_session_id,
                "host_id": host_id,
                "task_id": task_id,
                "status": terminal_status,
                "metadata": {
                    "terminalized_by": "terminal_task",
                    "terminal_task_status": directive.get("task_status"),
                    "terminal_cleanup_reason": directive.get("reason"),
                },
            })
        outcomes.append({
            "runner_session_id": runner_session_id,
            "task_id": task_id,
            "task_status": directive.get("task_status"),
            "killed": kill_ok,
            "terminalized": bool(terminal and not terminal.get("error")),
            "error": killed.get("error") or (
                (terminal or {}).get("error") if isinstance(terminal, dict) else None),
        })
    return outcomes


def renew_live_direct_runners(inventory):
    """Keep browser Watch/Chat bound to every live Mac Codex PTY.

    Direct-task wakes are acknowledged immediately after launch, so they leave
    the pending-wake feed while the native CLI continues working.  The launch
    registration has a deliberately short lease; without this host heartbeat a
    close/reopen of Watch loses the centrally discoverable row even though the
    supervisor-owned process and PTY are still alive.

    Claim-bound Autopilot sessions need the same renewal.  The worker heartbeat
    owns claim/Work Session state but cannot see the outer supervisor's PTY, so
    this host heartbeat continuously joins both halves. ``_drain_runners`` also
    repairs sessions whose central preclaim placeholders hid a live local PTY.
    """
    host_id = str((inventory or {}).get("host_id") or "")
    renewed = []
    sessions = _drain_runner_projects(inventory)
    projects_needing_late_binding = {
        str(row.get("_host_project") or PROJECT)
        for row in sessions if _direct_work_session_join_needed(row)
    }
    work_sessions_by_project = {
        project: _drain_project_work_sessions(project)
        for project in projects_needing_late_binding
    }
    for session in sessions:
        session_project = str(session.get("_host_project") or PROJECT)
        work_sessions = work_sessions_by_project.get(session_project, [])
        metadata = dict(session.get("metadata") or {})
        native_transport = metadata.get("native_host_execution") is True
        admission_preclaim = _direct_work_session_join_needed(session)
        claim_id = str(session.get("claim_id") or "")
        work_session_id = str(metadata.get("work_session_id") or "")
        late_binding = _direct_work_session_binding(session, work_sessions)
        if (not late_binding and session_project in projects_needing_late_binding
                and admission_preclaim
                and not session.get("claim_id")
                and not metadata.get("work_session_id")):
            # A short task can create and complete its managed Work Session
            # between two host ticks.  Query only this task's completed rows so
            # the exact direct-session principal can still close the binding
            # race without scanning historical Work Sessions fleet-wide.
            completed = _drain_project_work_sessions(
                session_project,
                task_id=str(session.get("task_id") or ""),
                status="completed",
            )
            late_binding = _direct_work_session_binding(
                session, completed, allowed_statuses={"completed"})
        if late_binding:
            claim_id = str(late_binding.get("claim_id") or "")
            work_session_id = str(late_binding.get("work_session_id") or "")
            metadata.update({
                "work_session_id": work_session_id,
                "credential_admission_phase": "claim_bound",
                "late_bound_by": "agent_host_work_session_join",
            })
        claim_bound = bool(claim_id and work_session_id)
        wake_id = str(metadata.get("wake_id") or session.get("wake_id") or "")
        task_id = str(session.get("task_id") or "")
        # BUG-91: an exited process must go terminal NOW, not drift to stale and
        # then expired. A row that merely stops being renewed still looks like a
        # live session for a whole lease, and stays the newest thing the browser
        # can find for the task long after that. The supervisor's `alive` is the
        # only local truth about the process, so report it the moment it flips.
        if (session.get("alive") is False and task_id
                and str(session.get("status") or "").lower() not in _TERMINAL_RUNNER_STATES):
            surrender_authority = str(
                (metadata.get("lease_surrender") or {}).get("authority") or "")
            terminal_surrender = surrender_authority in {
                "terminal_task", "completion_owner",
            }
            reason = (
                "terminal lease surrendered" if terminal_surrender else str(
                    metadata.get("failure_reason")
                    or "supervisor reported the process exited"
                ).strip()
            )
            receipt_metadata = dict(metadata)
            if terminal_surrender:
                receipt_metadata.pop("failure_reason", None)
                receipt_metadata.update({
                    "terminalized_by": "terminal_lease_surrendered",
                    "terminal_lease_surrendered_at": time.time(),
                })
            else:
                receipt_metadata.update({
                    "failure_reason": reason,
                    "terminalized_by": "host_supervisor",
                })
            receipt = {
                "project": PROJECT,
                "runner_session_id": session.get("runner_session_id"),
                "host_id": host_id,
                "task_id": task_id,
                "status": "stopped" if terminal_surrender else "exited",
                "metadata": receipt_metadata,
            }
            _persist_pending_stop_receipt(receipt)
            terminal = _try("POST", P_HEARTBEAT_RUNNER, receipt)
            terminal_ok = bool(
                terminal
                and not terminal.get("error")
                and not terminal.get("error_code"))
            if terminal_ok:
                _delete_pending_stop_receipt(session.get("runner_session_id"))
            wake_repaired = False
            # SIMPLIFY-3 / BUG-102: same tick — if a wake is bound, force
            # complete_wake(started=false) so claimed limbo cannot outlive the
            # local death. Already-terminal rows stay skipped (BUG-91).
            if not terminal_surrender and wake_id and terminal_ok:
                completion = _try("POST", P_COMPLETE_WAKE, {
                    "project": PROJECT,
                    "wake_id": wake_id,
                    "runner_session_id": session.get("runner_session_id") or "",
                    "agent_id": session.get("agent_id") or f"codex/{task_id}",
                    "result": {
                        "started": False,
                        "reason": reason,
                        "error": reason,
                        "failure_class": "launch_failed",
                        "runner_session_id": session.get("runner_session_id"),
                        "host_id": host_id,
                        "task_id": task_id,
                    },
                })
                wake_repaired = bool(
                    completion and not completion.get("error")
                    and not completion.get("error_code")
                )
            renewed.append({
                "runner_session_id": session.get("runner_session_id"),
                "task_id": task_id,
                "wake_id": wake_id or None,
                "terminalized": terminal_ok,
                "wake_repaired": wake_repaired,
            })
            continue
        connect_transport = metadata.get("connect_assignment") is True
        if (not (native_transport or connect_transport)
                or session.get("alive") is not True
                or str(session.get("status") or "").lower() != "running"):
            continue
        # BUG-175 / ADR-0008 C2: an explicit lease surrender must not be
        # renewed. Terminal-task cleanup and complete_claim fence the
        # generation; renewing a surrendered lease was the zombie amplifier.
        if metadata.get("lease_surrender"):
            continue
        # Stale Connect/direct PTYs still get a renew attempt this tick.
        # expire_runner_leases will not kill a still-alive Connect/native row
        # on stale alone — only surrender or local process death does.
        if not wake_id or not task_id:
            continue
        body = {
            "project": session_project,
            "runner_session_id": session.get("runner_session_id"),
            "host_id": host_id,
            "agent_id": session.get("agent_id") or f"codex/{task_id}",
            "runtime": session.get("runtime") or "codex",
            "task_id": task_id,
            "claim_id": claim_id if claim_bound else "",
            "pid": session.get("pid"),
            "status": "running",
            "cwd": session.get("cwd") or inventory.get("repo_root"),
            "control": session.get("control") or {
                "tier": "T3", "runner_kill": True, "managed_process": True,
                "runner_open": True, "runner_inject": True, "runner_logs": True,
            },
            "metadata": {
                **metadata,
                "wake_id": wake_id,
                "wake_mode": (session.get("wake_mode") or
                              "claim_next"),
                "native_host_execution": True,
                **({
                    "direct_assignment": True,
                    "assignment_schema": "switchboard.direct_cli_assignment.v1",
                } if metadata.get("direct_assignment") is True else {}),
                # WATCH-19: lightweight progress beside liveness (no log_tail).
                **_runner_progress_metadata(session),
            },
            # Busy hosts may spend longer than one nominal tick finalizing other
            # work. A three-minute lease prevents a healthy direct PTY from
            # flickering out of Watch between successful renewals.
            "heartbeat_ttl_s": 180,
        }
        # Server fences renewals that omit lease_epoch when the row already has
        # one (execution_liveness.heartbeat_is_fenced). Re-assert identity
        # fields after progress metadata merge so a quiet PTY cannot 403 itself
        # to death.
        for key in (
            "lease_epoch", "execution_id", "execution_generation",
            "execution_role", "execution_head_sha", "assignment_id",
            "connect_assignment", "assignment_schema",
        ):
            if metadata.get(key) not in (None, ""):
                body["metadata"][key] = metadata.get(key)
        host_preflight = _host_repo_preflight(
            session, inventory, body["metadata"])
        if host_preflight:
            body["metadata"]["host_repo_preflight"] = host_preflight
        result = _try("POST", P_HEARTBEAT_RUNNER, body)
        first_error = (
            (result or {}).get("error") if isinstance(result, dict)
            else "heartbeat_runner_session_failed"
        )
        if not result or first_error:
            # ADAPTER-35: a single transport blip must not unfairly consume a
            # runner's lease. Retry once in this daemon tick; the next tick
            # remains the deferred renewal boundary if both attempts fail.
            result = _try("POST", P_HEARTBEAT_RUNNER, body)
        final_error = (
            (result or {}).get("error") if isinstance(result, dict)
            else "heartbeat_runner_session_failed"
        )
        preflight_refresh = None
        if (late_binding and host_preflight and not final_error
                and work_session_id):
            # BUG-97: the binding heartbeat makes the host attestation durable;
            # immediately ask Coordination to validate that exact evidence and
            # replace the provisional pending report. This is an evidence
            # projection only: Capacity still owns runner liveness and does not
            # mutate task lifecycle state.
            preflight_refresh = _try(
                "POST",
                P_PREFLIGHT_WORK_SESSION.format(
                    work_session_id=urllib.parse.quote(
                        work_session_id, safe="")),
                {
                    "project": PROJECT,
                    "agent_id": session.get("agent_id") or f"codex/{task_id}",
                    "expected_branch": host_preflight.get("branch") or "",
                    "agent_host_bootstrap_binding": {
                        "wake_id": wake_id,
                        "host_id": host_id,
                        "runner_session_id": session.get("runner_session_id") or "",
                        "task_id": task_id,
                        "agent_id": session.get("agent_id") or f"codex/{task_id}",
                    },
                },
            )
        _collect_companion_relay_auth_fault(session.get("runner_session_id"))
        requested_relay = _consume_host_relay_refresh_request(
            session.get("runner_session_id"), host_id)
        server_relay = requested_relay or _fresh_server_relay((
            (result or {}).get("server_relay")
            if isinstance(result, dict) else None
        ), session.get("runner_session_id"), host_id)
        if server_relay.get("host_url"):
            try:
                _ensure_host_bridge(
                    runner_session_id=str(session.get("runner_session_id") or ""),
                    host_id=host_id,
                    binding=dict(server_relay.get("binding") or {}),
                    public_base="",
                    host_relay_url=str(server_relay.get("host_url") or ""),
                    child_pid=int(session.get("pid") or 0),
                    log_path=str(session.get("log_path") or metadata.get("log_path") or ""),
                    expires_at=server_relay.get("expires_at"),
                    force_rotate=bool(requested_relay and requested_relay.get("host_url")),
                )
            except Exception as exc:
                if isinstance(result, dict):
                    result["host_relay_error"] = type(exc).__name__
        renewed.append({
            "runner_session_id": session.get("runner_session_id"),
            "task_id": task_id,
            "renewed": bool(result and not result.get("error")),
            "error": final_error,
            **({
                "work_session_preflight_refreshed": bool(
                    not preflight_refresh.get("error")),
            } if isinstance(preflight_refresh, dict) else {}),
            "renew_deferred": bool(not result or final_error),
            "relay_url_minted": bool(server_relay.get("host_url")),
            **({
                "server_relay_error": server_relay.get("error"),
                "server_relay_missing": list(server_relay.get("missing") or []),
            } if not server_relay.get("host_url") else {}),
        })
    return renewed


def _drain_work_sessions(*, project=None, task_id="", status="active"):
    result = _try("GET", _drain_query(
        P_LIST_WORK_SESSIONS, status=status, task_id=task_id,
        include_expired="true", project=project)) or {}
    sessions = result.get("work_sessions") or []
    return sessions if isinstance(sessions, list) else []


def _drain_project_work_sessions(project, **filters):
    """Keep primary-project call shape stable while routing granted projects."""
    if str(project or PROJECT) == PROJECT:
        return _drain_work_sessions(**filters)
    return _drain_work_sessions(project=project, **filters)


def _direct_work_session_join_needed(session):
    """Return whether one live direct runner still lacks its exact WS tuple."""
    session = dict(session or {})
    metadata = dict(session.get("metadata") or {})
    return bool(
        session.get("alive") is True
        and str(session.get("status") or "").lower() == "running"
        and (metadata.get("direct_assignment") is True
             or metadata.get("connect_assignment") is True)
        and (not str(session.get("claim_id") or "").strip()
             or not str(metadata.get("work_session_id") or "").strip())
    )


def _direct_work_session_binding(session, work_sessions, *, allowed_statuses=None):
    """Find the one Work Session created by this exact direct Codex process.

    Direct sessions intentionally launch before a claim exists.  The direct MCP
    token later creates the claim and managed Work Session under a principal
    derived from the runner id.  Join those two phases here; task/agent matching
    alone is insufficient because retries may exist for the same task.
    """
    session = dict(session or {})
    metadata = dict(session.get("metadata") or {})
    runner_session_id = str(session.get("runner_session_id") or "").strip()
    if (not runner_session_id
            or not (metadata.get("direct_assignment") is True
                    or metadata.get("connect_assignment") is True)):
        return None
    expected_principal = f"direct-session/{runner_session_id}"
    task_id = str(session.get("task_id") or "").upper()
    agent_id = str(session.get("agent_id") or "")
    execution_id = str(metadata.get("execution_id") or "").strip()
    generation = metadata.get("execution_generation")
    claim_id = str(session.get("claim_id") or "").strip()
    work_session_id = str(metadata.get("work_session_id") or "").strip()
    allowed = {str(value).lower() for value in (allowed_statuses or {"active"})}
    matches = []
    for candidate in work_sessions or []:
        env = dict(candidate.get("env") or {})
        if (str(candidate.get("status") or "").lower() not in allowed
                or str(candidate.get("principal_id") or "") != expected_principal
                or str(candidate.get("task_id") or "").upper() != task_id
                or str(candidate.get("agent_id") or "") != agent_id
                or not str(candidate.get("claim_id") or "").strip()
                or not str(candidate.get("work_session_id") or "").strip()):
            continue
        if (claim_id
                and str(candidate.get("claim_id") or "").strip() != claim_id):
            continue
        if (work_session_id
                and str(candidate.get("work_session_id") or "").strip()
                != work_session_id):
            continue
        if (execution_id
                and str(env.get("execution_id") or "").strip() != execution_id):
            continue
        if (generation not in (None, "")
                and str(env.get("execution_generation") or "") != str(generation)):
            continue
        matches.append(candidate)
    return dict(matches[0]) if len(matches) == 1 else None


def _release_provider_lease(lease_id, reason):
    return _try(
        "POST",
        f"/api/projects/{urllib.parse.quote(PROJECT, safe='')}/"
        f"provider-credential-leases/{urllib.parse.quote(lease_id, safe='')}/release",
        {"project": PROJECT, "reason": reason},
    ) or {"state": "release_failed"}


def _runner_session_id_for_wake(wake, host_id):
    try:
        from switchboard.domain.runner_pty import planned_runner_session_id
    except ModuleNotFoundError:
        _root = os.path.abspath(os.path.join(_HERE, ".."))
        if os.path.join(_root, "src") not in sys.path:
            sys.path.insert(0, os.path.join(_root, "src"))
        from switchboard.domain.runner_pty import planned_runner_session_id
    return planned_runner_session_id(wake.get("wake_id"), host_id)


def _bound_finalizer_key(wake, inventory, runner_session_id):
    return (f"{inventory.get('host_id')}:{wake.get('wake_id')}:"
            f"{runner_session_id}")


def _reuse_inflight_bound_runner(wake, inventory, runner_session_id,
                                  preclaim_registration=None):
    """Return a pending receipt when this exact local boot already exists.

    A claimed wake may be requeued if the central host heartbeat briefly expires
    while a slow local fetch/worktree is still running.  The deterministic runner
    id then leads the next host tick back to the same supervised process.  Reclaim
    the wake, but never call ``supervisor start`` a second time: doing so rejects
    the duplicate id and incorrectly terminalizes the wake that the first process
    still owns.

    The in-memory finalizer is authoritative within one daemon lifetime.  The
    supervisor record also lets a restarted daemon reattach to a surviving local
    process.
    """
    key = _bound_finalizer_key(wake, inventory, runner_session_id)
    with _BOUND_FINALIZERS_LOCK:
        finalizer_active = key in _BOUND_FINALIZERS
    health = supervisor_action("health", runner_session_id)
    local_alive = bool(
        health and not health.get("error") and health.get("alive"))
    if not finalizer_active and not local_alive:
        return None
    rec = dict(health or {}) if local_alive else {}
    if not finalizer_active:
        _submit_bound_finalizer(wake, inventory, runner_session_id, rec)
    reported_cwd = str(rec.get("cwd") or "")
    if not reported_cwd and wake_mode(wake, inventory) != "connect":
        reported_cwd = str(inventory.get("repo_root") or "")
    return {
        "wake_id": wake.get("wake_id"),
        "started": True,
        "runner_session_id": runner_session_id,
        "wake_mode": rec.get("wake_mode") or wake_mode(wake, inventory),
        "reason": "runner_binding_pending_reused",
        "pid": rec.get("pid"),
        "cwd": reported_cwd,
        "task_id": rec.get("task_id") or wake.get("task_id"),
        "claim_id": None,
        "work_session_id": None,
        "control": rec.get("control") or {},
        "session_url": rec.get("session_url"),
        "provider_session_id": rec.get("provider_session_id"),
        "failure_class": None,
        "provider_error": None,
        "runner_registered": bool(
            preclaim_registration
            and not preclaim_registration.get("error")
            and not preclaim_registration.get("error_code")),
        "usage_registered": False,
        "binding_pending": True,
        "reused_local_runner": True,
    }


def _register_preclaim_runner(wake, inventory, runner_session_id, *, renewal=False):
    binding = ((wake.get("policy") or {}).get("account_binding") or {})
    selector = wake.get("selector") or {}
    return register_runner_session({
        "runner_session_id": runner_session_id,
        "agent_id": selector.get("agent_id"),
        "runtime": selector.get("runtime"),
        "task_id": wake.get("task_id"),
        "claim_id": binding.get("claim_id"),
        "status": "starting",
        "cwd": "",
        "control": {"tier": "T3", "runner_kill": True, "managed_process": True},
        "metadata": {
            "credential_admission_phase": "preclaim",
            **({"preclaim_renewal": True} if renewal else {}),
        },
    }, wake, inventory)


def wait_for_runner_binding(wake, inventory, runner_session_id, timeout_s=None,
                            max_timeout_s=None, runner_alive=None,
                            sleep=time.sleep, monotonic=time.monotonic):
    """Wait until the child has published its exact claim + Work Session tuple.

    Process liveness is not execution readiness.  Autopilot may report Running only
    after the child owns the exact task and its Watch/Chat row is fully bound.
    """
    explicit_timeout = timeout_s is not None
    timeout_s = float(timeout_s if explicit_timeout else os.environ.get(
        "PM_AGENT_HOST_BIND_TIMEOUT_S", "90"))
    if max_timeout_s is None:
        max_timeout_s = (timeout_s if explicit_timeout else os.environ.get(
            "PM_AGENT_HOST_BIND_MAX_TIMEOUT_S", "600"))
    max_timeout_s = max(timeout_s, float(max_timeout_s))
    started_at = monotonic()
    deadline = started_at + max(0.0, timeout_s)
    hard_deadline = started_at + max(0.0, max_timeout_s)
    extended_for_live_boot = False
    renew_interval_s = max(1.0, float(os.environ.get(
        "PM_AGENT_HOST_PRECLAIM_RENEW_INTERVAL_S", "15")))
    next_renewal = monotonic() + renew_interval_s
    expected = {
        "runner_session_id": str(runner_session_id or ""),
        "task_id": str(wake.get("task_id") or ""),
        "host_id": str(inventory.get("host_id") or ""),
        "wake_id": str(wake.get("wake_id") or ""),
        "agent_id": str((wake.get("selector") or {}).get("agent_id") or ""),
        "runtime": str((wake.get("selector") or {}).get("runtime") or ""),
    }
    last = None
    last_exact_preclaim = False
    while monotonic() <= deadline:
        query = urllib.parse.urlencode({
            "project": _wake_project(wake),
            "task_id": expected["task_id"],
            "host_id": expected["host_id"],
            "include_stale": "false",
        })
        result = _try("GET", f"{P_LIST_RUNNERS}?{query}") or {}
        sessions = result.get("sessions") or result.get("runner_sessions") or []
        for row in sessions if isinstance(sessions, list) else []:
            if str(row.get("runner_session_id") or "") != expected["runner_session_id"]:
                continue
            last = row
            metadata = row.get("metadata") or {}
            status = str(row.get("status") or "").lower()
            phase = str(
                metadata.get("credential_admission_phase") or "").lower()
            if (str(row.get("task_id") or "") == expected["task_id"]
                    and str(row.get("host_id") or "") == expected["host_id"]
                    and str(row.get("agent_id") or "") == expected["agent_id"]
                    and str(row.get("runtime") or "") == expected["runtime"]
                    and str(metadata.get("wake_id") or "") == expected["wake_id"]
                    and row.get("claim_id")
                    and metadata.get("work_session_id")
                    and phase == "claim_bound"
                    and not row.get("stale")
                    and status in {"ready", "running"}):
                return {"bound": True, "session": row}
            exact_preclaim = (
                str(row.get("task_id") or "") == expected["task_id"]
                and str(row.get("host_id") or "") == expected["host_id"]
                and str(row.get("agent_id") or "") == expected["agent_id"]
                and str(row.get("runtime") or "") == expected["runtime"]
                and str(metadata.get("wake_id") or "") == expected["wake_id"]
                and not row.get("claim_id")
                and not metadata.get("work_session_id")
                and phase == "preclaim"
                and status == "starting"
            )
            last_exact_preclaim = exact_preclaim
            now_mono = monotonic()
            if exact_preclaim and now_mono >= next_renewal:
                # The server performs an atomic compare-and-refresh.  If the child
                # bound between this read and POST, it returns the stronger row
                # unchanged instead of letting this preclaim record downgrade it.
                _register_preclaim_runner(
                    wake, inventory, runner_session_id, renewal=True)
                next_renewal = now_mono + renew_interval_s
        if monotonic() >= deadline:
            # Worktree creation on user-owned storage can legitimately exceed the
            # normal readiness SLO. Keep waiting only when both halves of the
            # admission proof agree that this is still the exact boot we launched:
            # the server still has our renewable preclaim and the local supervised
            # process is alive. A dead/mismatched boot still fails closed at the
            # original deadline; even a live boot is capped by hard_deadline.
            if (not extended_for_live_boot and last_exact_preclaim
                    and hard_deadline > deadline):
                if runner_alive is None:
                    health = supervisor_action("health", runner_session_id)
                    alive = bool((health or {}).get("alive"))
                else:
                    alive = bool(runner_alive(runner_session_id))
                if alive:
                    extended_for_live_boot = True
                    deadline = hard_deadline
                    continue
            break
        sleep(min(1.0, max(0.0, deadline - monotonic())))
    return {"bound": False, "reason": "runner_bind_timeout", "session": last}


def _enrich_bound_runner_record(rec, session):
    """Combine worker authority with supervisor-local Watch/Chat transport.

    The worker owns the claim, Work Session, phase, status, and workspace.  The
    supervisor owns the PTY/log/stream process details.  Preserve the former
    while adding the latter so the central row becomes both authoritative and
    actually watchable from the web.
    """
    rec = dict(rec or {})
    session = dict(session or {})
    local_metadata = dict(rec.get("metadata") or {})
    bound_metadata = dict(session.get("metadata") or {})
    # The worker publishes claim/Work Session authority before the Agent Host
    # finalizer joins in the supervisor-owned PTY.  Its row inherits the
    # preclaim transport placeholders (pty=false, null stream coordinates).
    # Never let those placeholders overwrite the supervisor's live transport.
    for key in _RUNNER_TRANSPORT_METADATA_FIELDS:
        bound_metadata.pop(key, None)
    return {
        **rec,
        "agent_id": session.get("agent_id") or rec.get("agent_id"),
        "runtime": session.get("runtime") or rec.get("runtime"),
        "task_id": session.get("task_id") or rec.get("task_id"),
        "claim_id": session.get("claim_id") or rec.get("claim_id"),
        "status": session.get("status") or rec.get("status"),
        "cwd": session.get("cwd") or rec.get("cwd"),
        "control": {
            **dict(session.get("control") or {}),
            **dict(rec.get("control") or {}),
        },
        # Bound values win if the local launch record still contains preclaim
        # metadata. Top-level PTY fields are folded in by register_runner_session.
        "metadata": {**local_metadata, **bound_metadata},
    }


def _missing_local_runner_transport(rec):
    """Return the missing supervisor-owned fields for a watchable relay PTY."""
    rec = dict(rec or {})
    missing = []
    if rec.get("pty") is not True:
        missing.append("pty")
    return missing


def _finalize_bound_runner(wake, inventory, runner_session_id, rec):
    """Finish claim-bound admission without blocking host dispatch/heartbeats."""
    bound_result = wait_for_runner_binding(wake, inventory, runner_session_id)
    runner_registration = (bound_result or {}).get("session")
    started = bool((bound_result or {}).get("bound"))
    reason = (bound_result or {}).get("reason") or "runner_bind_timeout"
    transport_missing = _missing_local_runner_transport(rec) if started else []
    if transport_missing:
        started = False
        reason = "runner_stream_not_ready"
    if not started:
        supervisor_action("kill", runner_session_id, {
            "grace_seconds": 2.0, "reason": "spawn failed before runner binding"})
        failed_rec = {
            **(rec or {}),
            "runner_session_id": runner_session_id,
            "status": "failed",
            "metadata": {
                **((rec or {}).get("metadata") or {}),
                "credential_admission_phase": "preclaim_failed",
                "failure_reason": reason,
                **({"missing_transport": transport_missing}
                   if transport_missing else {}),
            },
        }
        runner_registration = register_runner_session(
            failed_rec, wake, inventory)
    else:
        # The child owns claim/Work Session authority and the supervisor owns
        # Watch/Chat transport. Publish their joined row before acknowledging
        # the wake so the web can observe the runner as soon as it is Running.
        runner_registration = register_runner_session(
            _enrich_bound_runner_record(rec, runner_registration), wake, inventory)
        if (not runner_registration
                or runner_registration.get("error")
                or runner_registration.get("error_code")):
            started = False
            reason = ((runner_registration or {}).get("error_code")
                      or (runner_registration or {}).get("error")
                      or "runner_bind_registration_failed")
            supervisor_action("kill", runner_session_id, {
                "grace_seconds": 2.0, "reason": reason})
            failed_rec = {
                **(rec or {}),
                "runner_session_id": runner_session_id,
                "status": "failed",
                "metadata": {
                    **((rec or {}).get("metadata") or {}),
                    "credential_admission_phase": "preclaim_failed",
                    "failure_reason": reason,
                },
            }
            runner_registration = register_runner_session(
                failed_rec, wake, inventory)
        else:
            reason = "runner_bound"

            # BUG-126: Connect does not pass through the direct-task launch
            # branch that opens Watch/Chat immediately.  Once the child has
            # supplied its exact claim + Work Session tuple, registration can
            # mint the host relay ticket.  Open that bridge before completing
            # the wake so the first visible Running receipt is already
            # watchable; the heartbeat path remains an idempotent repair loop.
            server_relay = _fresh_server_relay(
                (runner_registration or {}).get("server_relay"),
                runner_session_id, str(inventory.get("host_id") or ""))
            if server_relay.get("host_url"):
                try:
                    _ensure_host_bridge(
                        runner_session_id=runner_session_id,
                        host_id=str(inventory.get("host_id") or ""),
                        binding=dict(server_relay.get("binding") or {}),
                        public_base="",
                        host_relay_url=str(server_relay.get("host_url") or ""),
                        child_pid=int((rec or {}).get("pid") or 0),
                        log_path=str((rec or {}).get("log_path") or ""),
                        expires_at=server_relay.get("expires_at"),
                    )
                except Exception as exc:
                    # Keep the provider process alive: the heartbeat will retry
                    # the idempotent bridge.  Preserve the launch-time failure
                    # on the durable receipt instead of hiding it.
                    runner_registration["host_relay_error"] = type(exc).__name__
            else:
                # A bound Connect process without a host capability remains
                # alive and retryable, but it is not Watch-ready.  Name that
                # state on the wake receipt; never silently equate process
                # liveness with an attached terminal.
                runner_registration["host_relay_error"] = "missing_host_url"

    result = {
        "started": started,
        "runner_session_id": ((rec or {}).get("runner_session_id")
                              or runner_session_id),
        "wake_mode": (rec or {}).get("wake_mode") or wake_mode(wake, inventory),
        "reason": reason,
        "pid": (rec or {}).get("pid"),
        "cwd": (rec or {}).get("cwd"),
        "task_id": (rec or {}).get("task_id") or wake.get("task_id"),
        "claim_id": ((runner_registration or {}).get("claim_id")
                     if started else None),
        "work_session_id": (((runner_registration or {}).get("metadata") or {})
                            .get("work_session_id") if started else None),
        "control": (rec or {}).get("control") or {},
        "session_url": (rec or {}).get("session_url"),
        "provider_session_id": (rec or {}).get("provider_session_id"),
        "failure_class": ((rec or {}).get("failure_class")
                          or (None if started else "failed_gate")),
        "provider_error": (rec or {}).get("provider_error"),
        "runner_registered": bool(
            runner_registration and not runner_registration.get("error")
            and not runner_registration.get("error_code")),
        "host_relay_error": (
            (runner_registration or {}).get("host_relay_error") or None),
        "usage_registered": False,
        "binding_pending": False,
    }
    completion = _try("POST", P_COMPLETE_WAKE, {
        "project": _wake_project(wake),
        "wake_id": wake.get("wake_id"),
        "runner_session_id": result["runner_session_id"],
        "agent_id": (wake.get("selector") or {}).get("agent_id"),
        "result": result,
    })
    result["wake_completed"] = bool(completion and not completion.get("error"))
    return {"host_id": inventory.get("host_id"),
            "wake_id": wake.get("wake_id"), **result}


def _submit_bound_finalizer(wake, inventory, runner_session_id, rec):
    """Start one daemon finalizer per claimed wake and return immediately."""
    key = _bound_finalizer_key(wake, inventory, runner_session_id)

    def finish():
        try:
            receipt = _finalize_bound_runner(
                wake, inventory, runner_session_id, rec)
        except Exception as exc:
            # A background exception must still fail closed and release the
            # durable wake instead of silently stranding it as claimed.
            supervisor_action("kill", runner_session_id, {
                "grace_seconds": 2.0, "reason": "runner bind finalizer failed"})
            result = {
                "started": False,
                "runner_session_id": runner_session_id,
                "wake_mode": (rec or {}).get("wake_mode") or wake_mode(wake, inventory),
                "reason": "runner_bind_finalizer_error",
                "task_id": wake.get("task_id"),
                "failure_class": "failed_gate",
                "provider_error": str(exc)[:500],
                "binding_pending": False,
            }
            register_runner_session({
                **(rec or {}),
                "runner_session_id": runner_session_id,
                "status": "failed",
                "metadata": {
                    **((rec or {}).get("metadata") or {}),
                    "credential_admission_phase": "preclaim_failed",
                    "failure_reason": "runner_bind_finalizer_error",
                },
            }, wake, inventory)
            _try("POST", P_COMPLETE_WAKE, {
                "project": _wake_project(wake),
                "wake_id": wake.get("wake_id"),
                "runner_session_id": runner_session_id,
                "agent_id": (wake.get("selector") or {}).get("agent_id"),
                "result": result,
            })
            receipt = {"host_id": inventory.get("host_id"),
                       "wake_id": wake.get("wake_id"), **result}
        with _BOUND_FINALIZERS_LOCK:
            _BOUND_FINALIZERS.pop(key, None)
            _BOUND_FINALIZER_RESULTS.append(receipt)

    with _BOUND_FINALIZERS_LOCK:
        if key in _BOUND_FINALIZERS:
            return False
        thread = threading.Thread(
            target=finish,
            name=f"agent-host-bind-{str(wake.get('wake_id') or '')[-12:]}",
            daemon=True,
        )
        _BOUND_FINALIZERS[key] = thread
        thread.start()
    return True


def _reap_bound_finalizers(host_id):
    """Return completed async receipts for this host without blocking."""
    with _BOUND_FINALIZERS_LOCK:
        ours = [row for row in _BOUND_FINALIZER_RESULTS
                if row.get("host_id") == host_id]
        _BOUND_FINALIZER_RESULTS[:] = [
            row for row in _BOUND_FINALIZER_RESULTS
            if row.get("host_id") != host_id
        ]
    return [{k: v for k, v in row.items() if k != "host_id"} for row in ours]


def _publish_drain_host(inventory, status, capacity):
    return _try("POST", P_HEARTBEAT_HOST, {
        "project": PROJECT,
        "host_id": inventory["host_id"],
        "status": status,
        "active_sessions": capacity.get("active_sessions"),
        "capacity": capacity,
        "last_error": "" if status == "drained" else ",".join(
            (capacity.get("drain_receipt") or {}).get("failures") or []),
    })


def _update_drained_runner(runner):
    return _try("POST", P_REGISTER_RUNNER, {"project": PROJECT, **dict(runner)})


def _drain_runner_action(inventory, action, runner_session_id, options=None):
    """Route automatic CO drain stops through the one execution-lease clock."""
    if action != "lease_stop":
        return supervisor_action(action, runner_session_id, options)
    transition = _try("POST", P_RUNNER_LEASE_DUE, {
        "project": PROJECT,
        "host_id": inventory["host_id"],
        "runner_session_id": runner_session_id,
        "reason": str((options or {}).get("reason") or "host drain"),
        "authority": "co_drain",
    }) or {}
    if transition.get("error"):
        return {"error": transition.get("error"), "alive": True,
                "lease_transition": transition}
    outcomes = expire_runner_leases(inventory)
    outcome = next((
        item for item in outcomes
        if item.get("runner_session_id") == runner_session_id
    ), {})
    health = supervisor_action("health", runner_session_id)
    return {
        **outcome,
        "alive": bool(health and not health.get("error") and health.get("alive")),
        "lease_transition": transition,
    }


def handle_drain(request, inventory):
    """Stop claims first, then interrupt/checkpoint/release/purge and acknowledge."""
    current = co_drain.read_receipt()
    if current and current.get("request_id") == request.get("request_id"):
        published = _publish_drain_host(
            inventory, current.get("status") or "drain_failed", {
            "active_sessions": 0 if current.get("status") == "drained" else 1,
            "drain_receipt": current,
        })
        current["durable_acknowledged"] = bool(
            published and not published.get("error"))
        co_drain.write_receipt(current)
        return {"host_id": inventory["host_id"], "draining": True,
                "drain_receipt": current, "acted": [], "pending": 0,
                "runner_controls": []}
    receipt = co_drain.drain_host(
        request,
        co_drain.inventory_for_drain(inventory),
        runners=_drain_runners(inventory["host_id"], recover_stale_local=False),
        work_sessions=_drain_work_sessions(),
        supervisor=lambda action, runner_id, options=None: _drain_runner_action(
            inventory, action, runner_id, options),
        release_lease=_release_provider_lease,
        publish_host=lambda status, capacity: _publish_drain_host(
            inventory, status, capacity),
        update_runner=_update_drained_runner,
        workspace_root=os.environ.get("PM_AGENT_HOST_WORKSPACE_ROOT")
        or str(Path(os.environ.get(
            "PM_AGENT_HOST_STATE_DIR",
            str(Path.home() / ".local" / "share" / "switchboard-agent-host"),
        )).expanduser() / "workspaces"),
        runtime_root=os.environ.get("PM_PROVIDER_RUNTIME_ROOT"),
    )
    co_drain.write_receipt(receipt)
    return {"host_id": inventory["host_id"], "draining": True,
            "drain_receipt": receipt, "acted": [], "pending": 0,
            "runner_controls": []}


def run_once(inventory):
    """One daemon iteration. Returns a summary of what it did (for tests + logging)."""
    drain_request = co_drain.discover_request()
    if drain_request:
        return handle_drain(drain_request, inventory)
    host_id = inventory["host_id"]
    finalized = _reap_bound_finalizers(host_id)
    finalized.extend(_drain_pending_wake_receipts())
    capacity = heartbeat_capacity(inventory)
    heartbeat_body = {
        "project": PROJECT, "host_id": host_id,
        "active_sessions": capacity["active_sessions"], "capacity": capacity,
    }
    # HARDEN-79: carry any relay-auth fault raised since the last tick. It is
    # best-effort by nature — a bearer stale enough to fault is stale enough to
    # reject this heartbeat too, which is why the relay endpoint records the
    # same rejection server-side and does not depend on the host reporting it.
    relay_auth_faults = drain_relay_auth_faults()
    if relay_auth_faults:
        heartbeat_body["relay_auth_fault"] = relay_auth_faults[-1]
    heartbeat = _try("POST", P_HEARTBEAT_HOST, heartbeat_body)
    # DOGFOOD-25: presence must reach EVERY project this host serves. Wake
    # polling already spans _host_projects, but heartbeating only PM_PROJECT
    # left every other board with a stale host row, so their placements
    # refused this host with host_unavailable and wakes expired unclaimed.
    # Policy authority stays with the primary project's response alone.
    for extra_project in _host_projects(inventory):
        if extra_project != PROJECT:
            _try("POST", P_HEARTBEAT_HOST,
                 {**heartbeat_body, "project": extra_project})
    if apply_authoritative_execution_policy(inventory, heartbeat):
        advertised = _try("POST", P_REGISTER_HOST, registration_inventory(inventory))
        apply_authoritative_execution_policy(inventory, advertised)
        capacity = heartbeat_capacity(inventory)
    # Stay on the release the server says to run. While an update is in flight
    # this host stops asking for wakes: the server withholds work from a
    # draining host, and a host that kept claiming would never reach the quiet
    # state its own update is waiting for.
    update_plan = apply_required_host_release(inventory, heartbeat, capacity)
    if update_plan is not None:
        return {
            "host_id": host_id,
            "pending": 0,
            "acted": finalized,
            "refused": [],
            "runner_controls": [],
            "host_update": dict(update_plan),
        }
    # Renew before expiry kill. The previous order marked a just-due lease stale
    # and SIGTERM'd a live Codex before this tick could extend the heartbeat.
    runner_heartbeats = renew_live_direct_runners(inventory)
    expired_runner_leases = expire_runner_leases(inventory)
    if expired_runner_leases:
        capacity = heartbeat_capacity(inventory)
    local_auth = capacity.get("local_auth")
    if isinstance(local_auth, dict) and local_auth.get("available") is not True:
        return {
            "host_id": host_id,
            "pending": 0,
            "acted": finalized,
            "refused": [],
            "runner_controls": [],
            "runner_heartbeats": runner_heartbeats,
            "expired_runner_leases": expired_runner_leases,
            "auth_available": False,
        }
    recovery = None
    recovery_enabled = (
        _truthy(os.environ.get("PM_PERSONAL_AGENT_HOST_RECOVERY"))
        or _truthy(os.environ.get("PM_PERSONAL_AGENT_HOST_EXECUTION"))
    )
    if recovery_enabled:
        try:
            from codex_local_worker import resume_pending_postprocessing
            recovery = resume_pending_postprocessing()
        except Exception as exc:
            recovery = {
                "schema": "switchboard.personal_postprocessing_recovery_scan.v1",
                "recovered": [],
                "pending": [{"error": str(exc)}],
                "recovered_count": 0,
                "pending_count": 1,
            }
        # Never accept another wake while exact pushed work still needs its
        # checkpoint/claim completion. The daemon retries this durable receipt on
        # every poll; after the bounded deadline it is retained as an operator-visible
        # quarantine instead of permanently disabling unrelated host work.
    if recovery and recovery.get("pending_count"):
        return {
            "host_id": host_id,
            "pending": 0,
            "acted": finalized,
            "refused": [],
            "runner_controls": [],
            "expired_runner_leases": expired_runner_leases,
            "postprocessing_recovery": recovery,
        }
    controls = handle_runner_controls(inventory)
    wakes = []
    for project in _host_projects(inventory):
        listed = _try(
            "GET", f"{P_LIST_WAKES}?project={urllib.parse.quote(project, safe='')}"
                   "&status=pending") or {}
        project_wakes = listed.get("wake_intents") or listed.get("wakes") or []
        wakes.extend({
            **wake,
            # Wake ids are project-local. Keep the poll's project authoritative
            # even when an older server response omits project_id.
            "_host_project": project,
        } for wake in wakes_bound_to_host(project_wakes))
    wakes = _fair_wake_order(wakes, _host_projects(inventory))
    acted = list(finalized)
    refused = []
    cap = inventory["limits"]["max_sessions"]
    for w in wakes:
        wake_project = _wake_project(w)
        # The supervisor list already includes sessions launched earlier in this
        # tick. Adding len(acted) counts those children a second time (and also
        # counts failed launches), which silently cuts usable fanout roughly in
        # half. Treat the supervisor's live inventory as the capacity authority.
        if capacity_occupancy(inventory) >= cap:
            print("[agent_host] at capacity; leaving remaining wakes for other hosts", flush=True)
            break
        exact_binding = validate_personal_wake_binding(w, inventory)
        if not exact_binding.get("valid"):
            refused.append({"wake_id": w.get("wake_id"), **exact_binding})
            continue
        if not eligible_runtime(w, inventory):
            continue  # not ours — let an eligible host claim it (substrate records if none do)
        wake_id = w.get("wake_id")
        if wake_mode(w, inventory) == "direct_task":
            selected_host = str((w.get("selector") or {}).get("host_id") or "")
            if selected_host != str(host_id or ""):
                continue
            assignment = dict((w.get("policy") or {}).get("assignment") or {})
            if (assignment.get("schema") != "switchboard.direct_cli_assignment.v1"
                    or str(assignment.get("task_id") or "") != str(w.get("task_id") or "")
                    or str(assignment.get("host_id") or "") != str(host_id or "")):
                refused.append({
                    "wake_id": wake_id,
                    "error": "direct_assignment_invalid",
                    "reason": "direct assignment does not match task and selected host",
                })
                continue
            runner_session_id = _runner_session_id_for_wake(w, host_id)
            health = supervisor_action("health", runner_session_id)
            reused = bool(health and not health.get("error") and health.get("alive"))
            if reused:
                rec = dict(health)
                rec.update({
                    "runner_session_id": runner_session_id,
                    "wake_mode": "direct_task",
                    "host_id": host_id,
                    "runtime": "codex",
                    "task_id": w.get("task_id") or "",
                })
            else:
                try:
                    rec = launch(
                        w, inventory, runner_session_id=runner_session_id,
                        extra_env={
                            "PM_DIRECT_CODEX_ASSIGNMENT_JSON": json.dumps(
                                assignment, sort_keys=True),
                            "PM_CO_WAKE_ID": str(wake_id or ""),
                            "PM_CO_HOST_ID": str(host_id or ""),
                        },
                    )
                except Exception as exc:
                    rec = {
                        "runner_session_id": runner_session_id,
                        "started": False,
                        "wake_mode": "direct_task",
                        "reason": "direct_cli_launch_configuration_error",
                        "failure_class": "failed_gate",
                        "provider_error": str(exc)[:500],
                    }
            started = bool(reused or confirm_started(rec))
            assignment_path = os.path.join(
                str(os.environ.get("PM_AGENT_HOST_RUNNER_DIR")
                    or os.environ.get("PM_RUNNER_DIR") or ".switchboard/runner"),
                runner_session_id, "assignment.toml",
            )
            if started:
                rec["status"] = "running"
                rec["metadata"] = {
                    **((rec or {}).get("metadata") or {}),
                    "direct_assignment": True,
                    "assignment_schema": assignment.get("schema"),
                    "assignment_toml": assignment_path,
                    "auth_lane": "enrolled_agent_host_token",
                }
                runner_registration = register_runner_session(rec, w, inventory)
                server_relay = _fresh_server_relay((
                    (runner_registration or {}).get("server_relay")
                    if isinstance(runner_registration, dict) else None
                ), runner_session_id, host_id)
                if server_relay.get("host_url"):
                    try:
                        _ensure_host_bridge(
                            runner_session_id=runner_session_id,
                            host_id=host_id,
                            binding=dict(server_relay.get("binding") or {}),
                            public_base="",
                            host_relay_url=str(server_relay.get("host_url") or ""),
                            child_pid=int((rec or {}).get("pid") or 0),
                            log_path=str((rec or {}).get("log_path") or ""),
                            expires_at=server_relay.get("expires_at"),
                        )
                    except Exception as exc:
                        if isinstance(runner_registration, dict):
                            runner_registration["host_relay_error"] = type(exc).__name__
            else:
                runner_registration = None
            registered = bool(
                runner_registration
                and not runner_registration.get("error")
                and not runner_registration.get("error_code")
            )
            completion = None
            if started and registered:
                result = {
                    "started": True,
                    "reason": "direct_cli_started",
                    "runner_session_id": runner_session_id,
                    "task_id": w.get("task_id"),
                    "host_id": host_id,
                    "pid": (rec or {}).get("pid"),
                    "cwd": (rec or {}).get("cwd"),
                }
                # Acknowledge only after the PTY is live and centrally visible.
                # There is deliberately no ownership handshake before launch.
                completion = _try("POST", P_COMPLETE_WAKE, {
                    "project": wake_project,
                    "wake_id": wake_id,
                    "runner_session_id": runner_session_id,
                    "agent_id": (w.get("selector") or {}).get("agent_id") or "",
                    "result": result,
                })
            completion_recorded = bool(
                completion and not completion.get("error")
                and not completion.get("error_code")
            )
            if started and (not registered or not completion_recorded):
                # A native process without its durable runner/wake receipt cannot
                # be discovered, watched, or safely deduplicated.  Stop it instead
                # of leaving an invisible orphan, publish a terminal row when the
                # registry is reachable, and leave the wake retryable.
                failure_reason = (
                    "direct_runner_registration_failed" if not registered
                    else "direct_complete_wake_failed"
                )
                supervisor_action("kill", runner_session_id, {
                    "grace_seconds": 2.0, "reason": failure_reason})
                failed_rec = {
                    **(rec or {}),
                    "runner_session_id": runner_session_id,
                    "status": "failed",
                    "metadata": {
                        **((rec or {}).get("metadata") or {}),
                        "failure_reason": failure_reason,
                    },
                }
                register_runner_session(failed_rec, w, inventory)
                rec = {**(rec or {}), "reason": failure_reason,
                       "failure_class": "failed_gate"}
                started = False
            acted.append({
                "wake_id": wake_id,
                "started": started,
                "runner_session_id": runner_session_id,
                "wake_mode": "direct_task",
                "reason": (
                    "direct_cli_started" if started and registered
                    else "direct_runner_registration_failed" if started
                    else (rec or {}).get("reason") or "direct_cli_launch_failed"
                ),
                "pid": (rec or {}).get("pid"),
                "cwd": (rec or {}).get("cwd"),
                "task_id": w.get("task_id"),
                "host_id": host_id,
                "runner_registered": registered,
                "assignment_toml": assignment_path,
                "completion_recorded": completion_recorded,
                "provider_error": (rec or {}).get("provider_error"),
            })
            continue
        binding = ((w.get("policy") or {}).get("account_binding") or {})
        bind_required = bool(
            w.get("task_id")
            and (w.get("policy") or {}).get("require_runner_bind") is True
        )
        runner_session_id = ""
        if wake_mode(w, inventory) == "connect":
            # Connect leases one stable runner identity before launch. The same
            # id is carried by the Ack, supervisor, environment, and registry.
            runner_session_id = _runner_session_id_for_wake(w, host_id)
        preclaim_registration = None
        if binding or bind_required:
            runner_session_id = _runner_session_id_for_wake(w, host_id)
            preclaim_registration = _register_preclaim_runner(
                w, inventory, runner_session_id)
            if not preclaim_registration or preclaim_registration.get("error"):
                continue
        claimed = _try("POST", P_CLAIM_WAKE, {
            "project": wake_project,
            "host_id": host_id,
            "wake_id": wake_id,
            "runner_session_id": runner_session_id,
        })
        if not claimed or not (claimed.get("claimed", True)):
            continue  # another host won it (atomic claim)
        claimed_wake = {
            **(claimed.get("wake") or w),
            "_host_project": wake_project,
        }
        claimed_exact_binding = validate_personal_wake_binding(
            claimed_wake, inventory)
        if not claimed_exact_binding.get("valid"):
            refused.append({"wake_id": wake_id, "phase": "post_claim",
                            **claimed_exact_binding})
            _try("POST", P_COMPLETE_WAKE, {
                "project": wake_project,
                "wake_id": wake_id,
                "runner_session_id": runner_session_id,
                "agent_id": ((claimed_wake.get("selector") or {}).get("agent_id") or ""),
                "result": {"started": False, "reason": "exact_binding_denied"},
            })
            continue
        if bind_required and runner_session_id:
            reused = _reuse_inflight_bound_runner(
                claimed_wake, inventory, runner_session_id,
                preclaim_registration=preclaim_registration)
            if reused:
                acted.append(reused)
                continue
        execution_binding = ((claimed_wake.get("policy") or {}).get(
            "execution_binding") or {})
        if wake_mode(claimed_wake, inventory) == "connect":
            # Connect hands the CLI only its six immutable connection refs.
            # Legacy Work Session/claim/lifecycle bootstrap belongs above this
            # layer and must not leak into a provider-neutral launch.
            launch_env = {}
        else:
            launch_env = ({
                "PM_CO_WAKE_ID": str(claimed_wake.get("wake_id") or wake_id or ""),
                "PM_CO_HOST_ID": str(host_id or ""),
                "PM_REMOTE_WORK_SESSION_REGISTRATION": "1",
                "PM_AUTO_WORK_SESSION": "1",
                "PM_WORK_SESSION_POLICY_PROFILE": "code_strict",
                "PM_RUNTIME": str((claimed_wake.get("selector") or {}).get(
                    "runtime") or ""),
                "PM_WORK_SESSION_SOURCE_PATH": str(inventory.get("repo_root") or ""),
                "PM_AGENT_HOST_ISOLATE_TASK_WORKSPACE": "1",
                "PM_PERSONAL_AGENT_HOST_EXECUTION": "0",
            } if claimed_wake.get("task_id") else {})
        if binding:
            launch_env.update({
                "PM_CO_ACCOUNT_BINDING_JSON": json.dumps(
                    (claimed_wake.get("policy") or {}).get("account_binding") or {},
                    sort_keys=True,
                ),
                "PM_CO_WAKE_ID": str(claimed_wake.get("wake_id") or wake_id or ""),
                "PM_CO_HOST_ID": str(host_id or ""),
                "PM_REMOTE_WORK_SESSION_REGISTRATION": "1",
                "PM_AUTO_WORK_SESSION": "1",
                "PM_WORK_SESSION_POLICY_PROFILE": "code_strict",
                "PM_PERSONAL_AGENT_HOST_EXECUTION": (
                    "1" if claimed_exact_binding.get("required") else "0"),
                "PM_WORK_SESSION_ID": str(binding.get("work_session_id") or ""),
                "PM_CLAIM_ID": str(binding.get("claim_id") or ""),
                "PM_SOURCE_SHA": str(execution_binding.get("source_sha") or ""),
                "PM_EXECUTION_CONNECTION_ID": str(
                    execution_binding.get("execution_connection_id") or ""),
            })
        try:
            rec = (launch(claimed_wake, inventory, runner_session_id=runner_session_id,
                          extra_env=launch_env)
                   if runner_session_id else launch(claimed_wake, inventory))
        except Exception as exc:
            rec = {
                "runner_session_id": runner_session_id or None,
                "started": False,
                "wake_mode": wake_mode(claimed_wake, inventory),
                "reason": "runtime_launch_configuration_error",
                "failure_class": "failed_gate",
                "provider_error": str(exc)[:500],
            }
        rec_mode = (rec or {}).get("wake_mode") or wake_mode(w, inventory)
        started = confirm_started(rec)
        # BYOA runners rebind this preclaim row themselves after claim_next has
        # produced the active task claim and Work Session. A generic post-launch
        # upsert here would race that update and erase the exact binding.
        bound_result = None
        if binding and started:
            runner_registration = preclaim_registration
        elif binding:
            failed_rec = {
                **(rec or {}),
                "runner_session_id": (
                    (rec or {}).get("runner_session_id") or runner_session_id
                ),
                "status": "failed",
                "metadata": {
                    **((rec or {}).get("metadata") or {}),
                    "credential_admission_phase": "preclaim_failed",
                    "failure_reason": "launch_failed",
                },
            }
            runner_registration = register_runner_session(
                failed_rec, claimed_wake, inventory)
        elif bind_required and started:
            _submit_bound_finalizer(
                claimed_wake, inventory, runner_session_id, rec)
            # Launch acknowledgement is intentionally distinct from durable
            # wake completion. The finalizer will publish the exact claim-bound
            # Watch/Chat row and complete this wake independently.
            acted.append({
                "wake_id": wake_id,
                "started": True,
                "runner_session_id": ((rec or {}).get("runner_session_id")
                                      or runner_session_id),
                "wake_mode": (rec or {}).get("wake_mode") or wake_mode(w, inventory),
                "reason": "runner_binding_pending",
                "pid": (rec or {}).get("pid"),
                "cwd": (rec or {}).get("cwd"),
                "task_id": (rec or {}).get("task_id") or w.get("task_id"),
                "claim_id": None,
                "work_session_id": None,
                "control": (rec or {}).get("control") or {},
                "session_url": (rec or {}).get("session_url"),
                "provider_session_id": (rec or {}).get("provider_session_id"),
                "failure_class": None,
                "provider_error": None,
                "runner_registered": bool(
                    preclaim_registration
                    and not preclaim_registration.get("error")),
                "usage_registered": False,
                "binding_pending": True,
            })
            continue
        elif bind_required:
            result_reason = (rec or {}).get("reason") or "launch_failed"
            failed_rec = {
                **(rec or {}),
                "runner_session_id": runner_session_id,
                "status": "failed",
                "metadata": {
                    **((rec or {}).get("metadata") or {}),
                    "credential_admission_phase": "preclaim_failed",
                    "failure_reason": result_reason,
                },
            }
            runner_registration = register_runner_session(
                failed_rec, claimed_wake, inventory)
        else:
            runner_registration = (
                register_runner_session(rec, claimed_wake, inventory) if started else None
            )
            connect_mode = wake_mode(claimed_wake, inventory) == "connect"
            if started and connect_mode:
                # ADAPTER-32 / BUG-195: retry transport blips before failing closed.
                for attempt in range(1, CONNECT_REGISTER_ATTEMPTS):
                    if (runner_registration
                            and not runner_registration.get("error")
                            and not runner_registration.get("error_code")):
                        break
                    if (runner_registration
                            and not _is_transport_registration_error(runner_registration)):
                        break
                    print(
                        f"[agent_host] Connect register_runner_session transport "
                        f"blip; retry {attempt}/{CONNECT_REGISTER_ATTEMPTS - 1} "
                        f"wake_id={wake_id}",
                        flush=True,
                    )
                    time.sleep(CONNECT_REGISTER_RETRY_DELAY_S)
                    runner_registration = register_runner_session(
                        rec, claimed_wake, inventory)
                if (not runner_registration
                        or runner_registration.get("error")
                        or runner_registration.get("error_code")):
                    classified = _classify_connect_registration_failure(
                        runner_registration)
                    rec = {
                        **(rec or {}),
                        "failure_class": classified["failure_class"],
                        "provider_error": classified["provider_error"],
                    }
                    supervisor_action("kill", runner_session_id, {
                        "grace_seconds": 2.0,
                        "reason": classified["reason"]})
                    started = False
                    result_reason = classified["reason"]
                else:
                    result_reason = "started"
            # COORD-34: non-BYOA claimed-task boots must publish a successful bind
            # before Watch/Chat may open. Incomplete/failed register fails the wake.
            elif started and (rec or {}).get("claim_id"):
                if (not runner_registration
                        or runner_registration.get("error")
                        or runner_registration.get("error_code") == "runner_bind_incomplete"):
                    started = False
                    result_reason = (
                        (runner_registration or {}).get("error_code")
                        or (runner_registration or {}).get("error")
                        or "runner_bind_incomplete"
                    )
                    supervisor_action("kill", runner_session_id, {
                        "grace_seconds": 2.0, "reason": result_reason})
                else:
                    result_reason = "started"
            else:
                result_reason = ("started" if started else
                                 (rec or {}).get("reason") or "launch_failed")
        usage_registration = report_cloud_usage(
            rec, claimed_wake) if started and rec.get("cloud_session") else None
        if binding:
            result_reason = "started" if started else "launch_failed"
        result = {"started": started,
                  "runner_session_id": ((rec or {}).get("runner_session_id")
                                        or runner_session_id or None),
                  "wake_mode": (rec or {}).get("wake_mode") or wake_mode(w, inventory),
                  "reason": result_reason,
                  "pid": (rec or {}).get("pid"),
                  "cwd": (rec or {}).get("cwd"),
                  "task_id": (rec or {}).get("task_id") or w.get("task_id"),
                  "claim_id": ((runner_registration or {}).get("claim_id")
                               if bind_required else (rec or {}).get("claim_id")),
                  "work_session_id": (((runner_registration or {}).get("metadata") or {})
                                      .get("work_session_id")
                                      if bind_required else (rec or {}).get("work_session_id")),
                  "control": (rec or {}).get("control") or {},
                  "session_url": (rec or {}).get("session_url"),
                  "provider_session_id": (rec or {}).get("provider_session_id"),
                  "failure_class": (rec or {}).get("failure_class"),
                  "provider_error": (rec or {}).get("provider_error"),
                  "runner_registered": bool(runner_registration and not runner_registration.get("error")),
                  "usage_registered": bool(usage_registration and not usage_registration.get("error"))}
        # A BYOA wake is only reserved here. The child must establish its task claim,
        # Work Session, exact lease, encrypted materialization, and provider preflight
        # before it completes the wake. Completing it now would race the second-phase
        # claim_wake call and make the admission contract impossible.
        if binding:
            result["wake_completion_delegated"] = bool(started)
        if not binding or not started:
            # ADAPTER-32: retry complete_wake so a single blip cannot strand a
            # claimed Connect wake (COORD-48 "Starting" limbo). Exhaustion still
            # logs loudly; other one-shot _try complete_wake paths are unchanged.
            _complete_wake_with_retry({
                "project": wake_project,
                "wake_id": wake_id,
                "runner_session_id": result["runner_session_id"],
                "agent_id": (w.get("selector") or {}).get("agent_id"),
                "result": result,
            })
        acted.append({"wake_id": wake_id, **result})
    return {"host_id": host_id, "pending": len(wakes), "acted": acted,
            "refused": refused,
            "runner_controls": controls,
            "runner_heartbeats": runner_heartbeats,
            "expired_runner_leases": expired_runner_leases,
            "postprocessing_recovery": recovery}


def run(interval=10, once=False):
    inv = default_inventory()
    registered = False
    last_register_at = 0.0
    drain_advertised = False
    register_every = max(10, int(inv.get("heartbeat_ttl_s") or 60) // 2)
    while True:
        now = time.time()
        auth_changed = refresh_local_auth_inventory(inv, now=now)
        drain_request = co_drain.discover_request()
        advertised = registration_inventory(inv, drain_request=drain_request)
        should_register = (not registered or auth_changed
                           or now - last_register_at >= register_every
                           or bool(drain_request) != drain_advertised)
        if should_register:
            reg = _try("POST", P_REGISTER_HOST, advertised)
            if apply_authoritative_execution_policy(inv, reg):
                advertised = registration_inventory(inv, drain_request=drain_request)
                reg = _try("POST", P_REGISTER_HOST, advertised)
            # DOGFOOD-25: renew the host row on every board this host serves,
            # not just PM_PROJECT — see the multi-project heartbeat in run_once.
            for extra_project in _host_projects(inv):
                if extra_project != PROJECT:
                    _try("POST", P_REGISTER_HOST,
                         {**advertised, "project": extra_project})
            registered = bool(reg and not reg.get("error"))
            drain_advertised = bool(drain_request and reg)
            last_register_at = now
            print(f"[agent_host] registered {inv['host_id']} ({'ok' if reg else 'retrying'})",
                  flush=True)
        summary = run_once(inv)
        print(f"[agent_host] {json.dumps(summary)}", flush=True)
        if once:
            return summary
        time.sleep(max(1, interval))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Switchboard Agent Host daemon")
    ap.add_argument("--once", action="store_true", help="one iteration then exit (for tests/cron)")
    ap.add_argument("--interval", type=int, default=10)
    a = ap.parse_args()
    out = run(interval=a.interval, once=a.once)
    if a.once:
        print(json.dumps(out))
