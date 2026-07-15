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
PM_MCP_TOKEN, PM_HOST_ID, PM_REPO_ROOT, PM_HOST_MAX_SESSIONS, PM_AGENT_WORK_MODULE (real work_fn;
absent -> --dry, which claims+abandons safely), PM_AGENT_HOST_ALLOW_WORK,
PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import switchboard_core as sb  # noqa: E402  (reuses _http + agent_id, same contract)
import co_drain  # noqa: E402
from codex.cloud_adapter import launch_wake as launch_codex_cloud_wake  # noqa: E402

PROJECT = os.environ.get("PM_PROJECT", "switchboard")
SUPERVISOR = os.path.join(_HERE, "codex", "supervisor.py")
RUN_AGENT = os.path.join(_HERE, "run_agent.py")
CLOSURE_VERIFIER = os.path.join(_HERE, "closure_verifier.py")

# Spec operation → REST path. Centralized so Codex's published paths get pinned in ONE place.
P_REGISTER_HOST = "/ixp/v1/register_host"
P_HEARTBEAT_HOST = "/ixp/v1/heartbeat_host"
P_LIST_WAKES = "/txp/v1/list_wake_intents"
P_CLAIM_WAKE = "/txp/v1/claim_wake"
P_COMPLETE_WAKE = "/txp/v1/complete_wake"
P_REGISTER_RUNNER = "/ixp/v1/register_runner_session"
P_LIST_RUNNER_CONTROLS = "/ixp/v1/runner_controls"
P_CLAIM_RUNNER_CONTROL = "/ixp/v1/claim_runner_control"
P_COMPLETE_RUNNER_CONTROL = "/ixp/v1/complete_runner_control"
P_LIST_RUNNERS = "/ixp/v1/runner_sessions"
P_LIST_WORK_SESSIONS = "/ixp/v1/work_sessions"
P_TALLY_SPEND = "/tally/v1/spend/ingest"
MESSAGE_ONLY_LANE = "__MESSAGE_ONLY__"
AGENT_HOST_VERSION = os.environ.get("PM_AGENT_HOST_VERSION", "0.2.0")


def _csv(value):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").replace("\n", ",").split(",") if x.strip()]


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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
        fingerprint = "acct-" + hashlib.sha256(
            f"switchboard-local-auth:{runtime}:{raw_proof}".encode()).hexdigest()[:16]
    return {
        "available": available,
        "runtime": runtime,
        "auth_mode": mode or ("local" if available else "unavailable"),
        "account_fingerprint": fingerprint or None,
        "credential_values_redacted": True,
        "provider_credential_exported": False,
    }


def _identity_inventory():
    generation = str(os.environ.get("PM_HOST_IDENTITY_GENERATION") or "").strip()
    return {
        "schema": "switchboard.agent_host_identity_proof.v1",
        "enrollment_id": os.environ.get("PM_HOST_ENROLLMENT_ID") or None,
        "identity_generation": int(generation) if generation.isdigit() else None,
        "public_key_fingerprint": os.environ.get("PM_HOST_PUBLIC_KEY_FINGERPRINT") or None,
        "credential_values_redacted": True,
    }


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
        "owner_user_ids": _csv(os.environ.get("PM_HOST_OWNER_USERS", "")),
        "projects": _csv(os.environ.get("PM_HOST_PROJECTS", PROJECT)),
        "providers": _csv(os.environ.get("PM_HOST_PROVIDERS", "")),
        "account_affinity_ids": _csv(os.environ.get("PM_HOST_ACCOUNT_AFFINITIES", "")),
        "supports_credential_leases": supports_leases,
        "repositories": _csv(os.environ.get(
            "PM_HOST_REPOSITORIES", "6th-Element-Labs/projectplanner")),
        "session_policies": _csv(os.environ.get("PM_HOST_SESSION_POLICIES", "code_strict")),
        "isolation_modes": _csv(os.environ.get("PM_HOST_ISOLATION", "task_worktree")),
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
        print(f"[agent_host] {method} {path} unavailable ({type(e).__name__}); skipping", flush=True)
        return None


def _require(method, path, body=None):
    """Fail-closed REST used for COORD-34 claim-bound runner registration."""
    try:
        return sb._http(method, path, body)
    except Exception as e:
        print(f"[agent_host] {method} {path} failed ({type(e).__name__}): {e}", flush=True)
        return {
            "error": "runner_bind_incomplete",
            "error_code": "runner_bind_incomplete",
            "failure_class": "unbound_identity",
            "refused": True,
            "message": f"{method} {path} failed: {type(e).__name__}",
        }


def default_inventory():
    repo = os.environ.get("PM_REPO_ROOT") or _git_root()
    host_id = os.environ.get("PM_HOST_ID") or f"host/{socket.gethostname().split('.')[0]}"
    env_lanes = _csv(os.environ.get("PM_HOST_LANES", ""))
    policy = host_policy_from_env(env_lanes)
    runtime_lanes = env_lanes or ([MESSAGE_ONLY_LANE] if not policy["allow_work"] else [])
    runtime = os.environ.get("PM_RUNTIME", "claude-code")
    cloud_enabled = runtime == "codex" and bool(os.environ.get("PM_CODEX_CLOUD_ENVIRONMENT_ID"))
    profiles = ["ixp.v1", "txp.dispatch.v0"]
    capabilities = ["docs", "python", "github", "tests"]
    # Fleet workers advertise a host-owned capability profile.  The wake payload may
    # select from this inventory, but it cannot add capabilities to the host.  Keeping
    # this in configuration lets co-general/co-build use the same immutable AMI while
    # still failing closed when a heavy-build wake lands on a general worker.
    capabilities.extend(_csv(os.environ.get("PM_HOST_CAPABILITIES", "")))
    capabilities = list(dict.fromkeys(capabilities))
    if cloud_enabled:
        profiles.append("cloud_execution")
        capabilities.append("cloud_execution")
    placement = placement_inventory(repo, runtime, policy)
    local_auth = _redacted_local_auth(runtime)
    owner = {
        "user_id": os.environ.get("PM_HOST_OWNER_USER_ID") or None,
        "tenant_allowlist": placement.get("tenant_ids") or [],
        "project_allowlist": placement.get("projects") or [],
        "provider_allowlist": placement.get("providers") or [],
    }
    return {
        "project": PROJECT, "host_id": host_id, "hostname": socket.gethostname(),
        "agent_host_version": AGENT_HOST_VERSION, "repo_root": repo,
        "policy": policy,
        "runtimes": [{
            "runtime": runtime,
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
        },
        "heartbeat_ttl_s": 60,
    }


def heartbeat_capacity(inventory):
    """Return the full non-secret admission record for each heartbeat."""
    active = active_session_count(inventory)
    maximum = int((inventory.get("limits") or {}).get("max_sessions") or 0)
    capacity = dict(inventory.get("capacity") or {})
    capacity.update({
        "active_sessions": active,
        "headroom": max(0, maximum - active),
        "allow_work": bool((inventory.get("policy") or {}).get("allow_work")),
        "drain_state": ((capacity.get("placement") or {}).get("drain_state")
                        or capacity.get("drain_state") or "accepting"),
    })
    return capacity


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
    want_caps = set(_csv(sel.get("capabilities") or []))
    requested_mode = str(((wake or {}).get("policy") or {}).get("mode") or "").strip()
    wants_claim = requested_mode == "claim_next" or bool(want_lane and requested_mode != "message_only")
    for rt in inventory["runtimes"]:
        if want_rt and rt["runtime"] != want_rt:
            continue
        rt_policy = {**(inventory.get("policy") or {}), **(rt.get("policy") or {})}
        rt_lanes = set(rt.get("lanes") or [])
        if wants_claim:
            if not rt_policy.get("allow_work"):
                continue
            if want_lane:
                if want_lane not in rt_lanes:
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
    A closure_verification wake is a special case of message-only: still lane-less (never
    a claim_next grab), but instead of the inbox-only ack stub it runs the deterministic
    closure engine (DELIVERABLES-23) — bounded gate checks, not an open-ended agent.
    """
    policy = (wake or {}).get("policy") or {}
    selector = (wake or {}).get("selector") or {}
    explicit = (policy.get("mode") or "").strip()
    if explicit == "cloud_execution" or policy.get("kind") == "cloud_execution":
        return "cloud_execution"
    if policy.get("kind") == "closure_verification" and policy.get("deliverable_id"):
        return "closure_verify"
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
        if str(session.get("status") or "").lower() not in {
            "completed", "failed", "cancelled", "expired", "lost", "killed", "exited"
        }:
            active += 1
    return active


def launch_command(wake, inventory, runner_session_id=""):
    """Build the supervisor command for a wake without executing it."""
    sel = wake.get("selector") or {}
    eligible = eligible_runtime(wake, inventory)
    if not eligible:
        raise ValueError("wake is not eligible for this host policy/runtime inventory")
    agent_id = sel.get("agent_id") or sel.get("runtime") or "claude-code"
    lane = sel.get("lane") or ""
    runtime = sel.get("runtime") or eligible.get("runtime") or "claude-code"
    work_mod = os.environ.get("PM_AGENT_WORK_MODULE", "")
    mode = wake_mode(wake, inventory)
    if mode == "refused":
        raise ValueError("wake asks for global claim_next but host policy forbids global work")
    if mode == "closure_verify":
        policy = wake.get("policy") or {}
        child = [sys.executable, CLOSURE_VERIFIER, "--project", PROJECT,
                 "--deliverable-id", policy.get("deliverable_id"),
                 "--host-id", inventory.get("host_id", "")]
        if wake.get("wake_id"):
            child += ["--wake-id", wake.get("wake_id")]
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
        child += (["--work-module", work_mod] if work_mod else ["--dry"])
    cmd = [sys.executable, SUPERVISOR, "start", "--agent-id", agent_id,
           "--cwd", inventory["repo_root"]]
    if runner_session_id:
        cmd += ["--runner-session-id", runner_session_id]
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
    cmd, mode = launch_command(wake, inventory, runner_session_id=runner_session_id)
    try:
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (extra_env or {}).items()})
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, env=env)
        rec = json.loads(out.stdout)
        if isinstance(rec, dict):
            rec["wake_mode"] = mode
            rec["host_id"] = inventory.get("host_id")
            rec["runtime"] = (wake.get("selector") or {}).get("runtime") or ""
            rec["task_id"] = rec.get("task_id") or wake.get("task_id") or ""
        return rec
    except Exception as e:
        print(f"[agent_host] launch failed: {e}", flush=True)
        return None


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


def confirm_closure_verified(rec, grace_s=4.0):
    """Like confirm_started, but for the closure_verify job: it is deterministic and
    often finishes within confirm_started's own liveness window on success (a
    scope-only gate resolves in well under a second) — 'no longer alive' must not be
    conflated with 'crashed', or the daemon logs launch_failed for jobs that actually
    ran fine and persisted a report. Still-alive at the deadline is success (matches
    confirm_started). Once it has exited, trust its own last-line JSON verdict
    (adapters/closure_verifier.py always prints one) rather than raw process liveness.
    """
    pid = (rec or {}).get("pid")
    if not pid:
        return False
    deadline = time.time() + grace_s
    while time.time() < deadline:
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            result = _tail_json_result((rec or {}).get("log_path"))
            return bool(result) and not result.get("error")
        time.sleep(0.5)
    return True


def register_runner_session(rec, wake, inventory):
    """Publish the supervisor session to Switchboard's central runner registry.

    COORD-34: claimed/watchable registrations must carry task/claim/host/wake/
    work_session bind fields. Incomplete bind returns a typed error payload.
    """
    if not rec or not rec.get("runner_session_id"):
        return None
    binding = ((wake.get("policy") or {}).get("account_binding") or {})
    metadata = {
        "wake_id": wake.get("wake_id"),
        "wake_mode": rec.get("wake_mode"),
        "log_path": rec.get("log_path"),
        "command": rec.get("command"),
        "pty": bool(rec.get("pty")),
        "stream_bind": rec.get("stream_bind"),
        "stream_port": rec.get("stream_port"),
        "work_session_id": (
            (rec.get("metadata") or {}).get("work_session_id")
            or binding.get("work_session_id")
            or rec.get("work_session_id")
        ),
        "credential_lease_id": binding.get("credential_lease_id"),
        "provider": binding.get("provider"),
        "account_affinity_id": binding.get("account_affinity_id"),
        **(rec.get("metadata") or {}),
    }
    # Prefer explicit host/<instance-id> from inventory; never invent task-row EC2 ids.
    host_id = inventory.get("host_id") or ""
    body = {
        "project": PROJECT,
        "runner_session_id": rec.get("runner_session_id"),
        "host_id": host_id,
        "agent_id": rec.get("agent_id") or (wake.get("selector") or {}).get("agent_id"),
        "runtime": rec.get("runtime") or (wake.get("selector") or {}).get("runtime"),
        "task_id": rec.get("task_id") or wake.get("task_id") or "",
        "claim_id": rec.get("claim_id") or binding.get("claim_id") or "",
        "pid": rec.get("pid"),
        "status": rec.get("status") or "running",
        "cwd": rec.get("cwd") or inventory.get("repo_root"),
        "control": rec.get("control") or {"tier": "T3", "runner_kill": True,
                                           "managed_process": True},
        "metadata": metadata,
        "heartbeat_ttl_s": 3600 if rec.get("cloud_session") else 60,
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
    return _try("POST", P_REGISTER_RUNNER, body)


def report_cloud_usage(rec, wake):
    receipt = (rec or {}).get("usage_receipt") or {}
    if not receipt:
        return None
    return _try("POST", P_TALLY_SPEND, {
        "project": PROJECT,
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
        try:
            from codex.pty_stream import build_stream_url, mint_ticket
        except ModuleNotFoundError:
            sys.path.insert(0, _HERE)
            from codex.pty_stream import build_stream_url, mint_ticket
        status_cmd = [sys.executable, SUPERVISOR, "status", runner_session_id]
        try:
            out = subprocess.run(status_cmd, capture_output=True, text=True, timeout=15)
            if out.returncode != 0:
                return {"error": "supervisor_failed", "stderr": (out.stderr or "")[-4000:]}
            meta = json.loads(out.stdout or "{}")
        except Exception as e:
            return {"error": type(e).__name__, "message": str(e)}
        control = meta.get("control") or {}
        streamer_pid = int(meta.get("streamer_pid") or 0)
        stream_port = int(meta.get("stream_port") or 0)
        stream_bind = str(meta.get("stream_bind") or "127.0.0.1")
        streamer_alive = bool(streamer_pid and _pid_alive(streamer_pid))
        port_listening = _tcp_port_open(stream_bind, stream_port) if stream_port else False
        if not (meta.get("pty") and control.get("runner_open") and stream_port
                and meta.get("alive") and streamer_alive and port_listening):
            return {
                "error": "not_supported",
                "reason": "runner_open requires a live PTY-backed local session with an active streamer",
            }
        host_id = str(meta.get("host_id") or os.environ.get("PM_HOST_ID") or "")
        ticket, expires_at = mint_ticket(
            runner_session_id=runner_session_id,
            host_id=host_id,
            ttl_seconds=int(options.get("ttl_seconds") or 900),
        )
        local_stream_url = build_stream_url(
            bind_host=stream_bind,
            port=stream_port,
            runner_session_id=runner_session_id,
            ticket=ticket,
            public_base="",
        )
        public_base = str(
            os.environ.get("PM_RUNNER_PTY_RELAY_PUBLIC_BASE")
            or os.environ.get("PM_SWITCHBOARD_PUBLIC_BASE")
            or ""
        ).rstrip("/")
        # Prefer Switchboard relay when a non-loopback public base is configured
        # so browsers never receive a host-local 127.0.0.1 URL (ADAPTER-22).
        use_relay = False
        relay_url = ""
        transport = "http_chunked"
        browser_safe = False
        relay_required = True
        stream_url = local_stream_url
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
            if not pty_relay.is_loopback_url(public_base):
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
                scopes = options.get("scopes") or [
                    "watch", "input", "resize", "signal"]
                try:
                    relay_ticket, relay_payload = pty_relay.mint_capability_ticket(
                        binding, scopes,
                        ttl_seconds=int(options.get("ttl_seconds") or 900))
                    relay_url = pty_relay.public_relay_url(
                        public_base, runner_session_id, relay_ticket)
                    use_relay = True
                    transport = pty_domain.TRANSPORT_SWITCHBOARD_PTY_RELAY
                    browser_safe = True
                    relay_required = False
                    stream_url = relay_url
                    expires_at = float(relay_payload.get("exp") or expires_at)
                    ticket = relay_ticket
                except Exception as mint_exc:
                    # BUG-76: non-loopback public base requires relay. Never fall
                    # back to local http_chunked / 127.0.0.1 stream_url.
                    return {
                        "error": "relay_mint_failed",
                        "reason": str(mint_exc) or type(mint_exc).__name__,
                        "failure_class": "hidden_fallback",
                        "opened": False,
                        "runner_session_id": runner_session_id,
                        "transport": None,
                        "browser_safe": False,
                        "relay_required": True,
                        "capabilities": {"stream": "denied", "open": "denied"},
                    }
        metadata = {
            "pty": True,
            "stream_url": stream_url,
            "stream_ticket_exp": expires_at,
            "transport": transport,
            "browser_safe": browser_safe,
            "relay_required": relay_required,
            "local_stream_url": local_stream_url,
        }
        if use_relay and relay_url:
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
                # Keep host-private loopback coordinate after sanitize.
                metadata["local_stream_url"] = local_stream_url
        return {
            "opened": True,
            "runner_session_id": runner_session_id,
            "transport": transport,
            "stream_url": stream_url,
            "relay_url": relay_url or None,
            "ticket": ticket,
            "expires_at": expires_at,
            "browser_safe": browser_safe,
            "relay_required": relay_required,
            "capabilities": {"stream": "supported", "open": "supported"},
            "metadata": metadata,
        }
    elif action == "inject":
        try:
            from codex.pty_stream import (
                build_inject_url,
                mint_inject_ticket,
            )
        except ModuleNotFoundError:
            sys.path.insert(0, _HERE)
            from codex.pty_stream import (
                build_inject_url,
                mint_inject_ticket,
            )
        status_cmd = [sys.executable, SUPERVISOR, "status", runner_session_id]
        try:
            out = subprocess.run(status_cmd, capture_output=True, text=True, timeout=15)
            if out.returncode != 0:
                return {"error": "supervisor_failed", "stderr": (out.stderr or "")[-4000:]}
            meta = json.loads(out.stdout or "{}")
        except Exception as e:
            return {"error": type(e).__name__, "message": str(e)}
        caller_task = str(options.get("task_id") or "").strip()
        session_task = str(meta.get("task_id") or "").strip()
        if not caller_task or not session_task or caller_task != session_task:
            return {
                "error": "wrong_session",
                "reason": "task_mismatch",
                "runner_session_id": runner_session_id,
                "expected_task_id": session_task or None,
                "provided_task_id": caller_task or None,
            }
        text = options.get("text")
        if text is None:
            text = options.get("message")
        if not isinstance(text, str) or not text:
            return {"error": "invalid_input", "reason": "text_required"}
        kind = str(options.get("kind") or "freeform").strip().lower() or "freeform"
        control = meta.get("control") or {}
        streamer_pid = int(meta.get("streamer_pid") or 0)
        stream_port = int(meta.get("stream_port") or 0)
        stream_bind = str(meta.get("stream_bind") or "127.0.0.1")
        streamer_alive = bool(streamer_pid and _pid_alive(streamer_pid))
        port_listening = _tcp_port_open(stream_bind, stream_port) if stream_port else False
        if not (meta.get("pty") and control.get("runner_inject") and stream_port
                and meta.get("alive") and streamer_alive and port_listening):
            return {
                "error": "not_supported",
                "reason": "runner_inject requires a live PTY-backed local session with an active streamer",
            }
        host_id = str(meta.get("host_id") or os.environ.get("PM_HOST_ID") or "")
        ticket, expires_at = mint_inject_ticket(
            runner_session_id=runner_session_id,
            task_id=caller_task,
            host_id=host_id,
            ttl_seconds=int(options.get("ttl_seconds") or 120),
        )
        inject_url = build_inject_url(
            bind_host=stream_bind,
            port=stream_port,
            runner_session_id=runner_session_id,
            public_base=str(os.environ.get("PM_RUNNER_STREAM_PUBLIC_BASE") or ""),
        )
        payload = {
            "ticket": ticket,
            "task_id": caller_task,
            "text": text,
            "kind": kind,
            "nl": bool(options.get("nl", options.get("newline", True))),
        }
        try:
            req = urllib.request.Request(
                inject_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
        except Exception as e:
            return {"error": type(e).__name__, "message": str(e), "inject_url": inject_url}
        if not body.get("injected"):
            return {
                "error": body.get("error") or "inject_failed",
                "reason": body.get("reason") or "companion_refused",
                "result": body,
            }
        return {
            "injected": True,
            "runner_session_id": runner_session_id,
            "task_id": caller_task,
            "kind": kind,
            "bytes_written": body.get("bytes_written"),
            "expires_at": expires_at,
            "capabilities": {"inject": "supported"},
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
            # Never register host-private loopback URLs on the control plane.
            browser_meta.pop("local_stream_url", None)
            # BUG-76: do not reintroduce loopback via result.stream_url fallback
            # after sanitize strips it from browser_meta.
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
        _try("POST", P_COMPLETE_RUNNER_CONTROL,
             {"project": PROJECT, "host_id": host_id, "request_id": req_id,
              "status": status, "result": result, "snapshot": snapshot})
        handled.append({"request_id": req_id, "action": action, "status": status,
                        "runner_session_id": req.get("runner_session_id")})
    return handled


def _drain_query(path, **query):
    return f"{path}?{urllib.parse.urlencode({'project': PROJECT, **query})}"


def _drain_runners(host_id):
    result = _try("GET", _drain_query(
        P_LIST_RUNNERS, host_id=host_id, include_stale="true")) or {}
    sessions = result.get("sessions") or result.get("runner_sessions") or []
    sessions = sessions if isinstance(sessions, list) else []
    try:
        out = subprocess.run(
            [sys.executable, SUPERVISOR, "list"],
            capture_output=True, text=True, timeout=10)
        local = (json.loads(out.stdout or "{}").get("sessions") or []) \
            if out.returncode == 0 else []
    except Exception:
        local = []
    merged = {row.get("runner_session_id"): dict(row) for row in local
              if row.get("runner_session_id")}
    for row in sessions:
        runner_id = row.get("runner_session_id")
        if runner_id:
            merged[runner_id] = {**merged.get(runner_id, {}), **dict(row)}
    return list(merged.values())


def _drain_work_sessions():
    result = _try("GET", _drain_query(
        P_LIST_WORK_SESSIONS, status="active", include_expired="true")) or {}
    sessions = result.get("work_sessions") or []
    return sessions if isinstance(sessions, list) else []


def _release_provider_lease(lease_id, reason):
    return _try(
        "POST",
        f"/api/projects/{urllib.parse.quote(PROJECT, safe='')}/"
        f"provider-credential-leases/{urllib.parse.quote(lease_id, safe='')}/release",
        {"project": PROJECT, "reason": reason},
    ) or {"state": "release_failed"}


def _runner_session_id_for_wake(wake, host_id):
    source = f"{wake.get('wake_id') or ''}:{host_id or ''}"
    return "run_" + hashlib.sha256(source.encode()).hexdigest()[:16]


def _register_preclaim_runner(wake, inventory, runner_session_id):
    binding = ((wake.get("policy") or {}).get("account_binding") or {})
    selector = wake.get("selector") or {}
    return register_runner_session({
        "runner_session_id": runner_session_id,
        "agent_id": selector.get("agent_id"),
        "runtime": selector.get("runtime"),
        "task_id": wake.get("task_id"),
        "claim_id": binding.get("claim_id"),
        "status": "starting",
        "cwd": inventory.get("repo_root"),
        "control": {"tier": "T3", "runner_kill": True, "managed_process": True},
        "metadata": {"credential_admission_phase": "preclaim"},
    }, wake, inventory)


def _acquire_provider_lease(wake, inventory, runner_session_id):
    binding = ((wake.get("policy") or {}).get("account_binding") or {})
    reference = str(binding.get("credential_reference") or "")
    if not reference:
        return {"error": "credential_reference_missing"}
    return _try(
        "POST",
        f"/api/projects/{urllib.parse.quote(PROJECT, safe='')}/"
        f"provider-connections/{urllib.parse.quote(reference, safe='')}/leases",
        {
            "project": PROJECT,
            "user_id": binding.get("user_id"),
            "provider": binding.get("provider"),
            "provider_account_id": binding.get("provider_account_id"),
            "task_id": wake.get("task_id"),
            "host_id": inventory.get("host_id"),
            "runner_session_id": runner_session_id,
            "work_session_id": binding.get("work_session_id"),
            "account_affinity_id": binding.get("account_affinity_id"),
            "ttl_seconds": int((wake.get("policy") or {}).get(
                "credential_lease_ttl_seconds") or 900),
        },
    ) or {"error": "credential_lease_acquisition_failed"}


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
        runners=_drain_runners(inventory["host_id"]),
        work_sessions=_drain_work_sessions(),
        supervisor=supervisor_action,
        release_lease=_release_provider_lease,
        publish_host=lambda status, capacity: _publish_drain_host(
            inventory, status, capacity),
        update_runner=_update_drained_runner,
        workspace_root=os.environ.get("PM_WORKSPACE_ROOT")
        or os.path.dirname(os.environ.get("PM_REPO_ROOT")
                           or inventory.get("repo_root") or os.getcwd()),
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
    capacity = heartbeat_capacity(inventory)
    _try("POST", P_HEARTBEAT_HOST, {"project": PROJECT, "host_id": host_id,
                                    "active_sessions": capacity["active_sessions"],
                                    "capacity": capacity})
    controls = handle_runner_controls(inventory)
    listed = _try("GET", f"{P_LIST_WAKES}?project={PROJECT}&status=pending") or {}
    wakes = wakes_bound_to_host(listed.get("wake_intents") or listed.get("wakes") or [])
    acted = []
    refused = []
    cap = inventory["limits"]["max_sessions"]
    for w in wakes:
        if active_session_count(inventory) + len(acted) >= cap:
            print("[agent_host] at capacity; leaving remaining wakes for other hosts", flush=True)
            break
        exact_binding = validate_personal_wake_binding(w, inventory)
        if not exact_binding.get("valid"):
            refused.append({"wake_id": w.get("wake_id"), **exact_binding})
            continue
        if not eligible_runtime(w, inventory):
            continue  # not ours — let an eligible host claim it (substrate records if none do)
        wake_id = w.get("wake_id")
        binding = ((w.get("policy") or {}).get("account_binding") or {})
        runner_session_id = ""
        preclaim_registration = None
        if binding:
            runner_session_id = _runner_session_id_for_wake(w, host_id)
            preclaim_registration = _register_preclaim_runner(
                w, inventory, runner_session_id)
            if not preclaim_registration or preclaim_registration.get("error"):
                continue
        claimed = _try("POST", P_CLAIM_WAKE, {
            "project": PROJECT,
            "host_id": host_id,
            "wake_id": wake_id,
            "runner_session_id": runner_session_id,
        })
        if not claimed or not (claimed.get("claimed", True)):
            continue  # another host won it (atomic claim)
        claimed_wake = claimed.get("wake") or w
        claimed_exact_binding = validate_personal_wake_binding(
            claimed_wake, inventory)
        if not claimed_exact_binding.get("valid"):
            refused.append({"wake_id": wake_id, "phase": "post_claim",
                            **claimed_exact_binding})
            _try("POST", P_COMPLETE_WAKE, {
                "project": PROJECT,
                "wake_id": wake_id,
                "runner_session_id": runner_session_id,
                "agent_id": ((claimed_wake.get("selector") or {}).get("agent_id") or ""),
                "result": {"started": False, "reason": "exact_binding_denied"},
            })
            continue
        execution_binding = ((claimed_wake.get("policy") or {}).get(
            "execution_binding") or {})
        launch_env = ({
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
        } if binding else {})
        rec = (launch(claimed_wake, inventory, runner_session_id=runner_session_id,
                      extra_env=launch_env)
               if runner_session_id else launch(claimed_wake, inventory))
        rec_mode = (rec or {}).get("wake_mode") or wake_mode(w, inventory)
        started = (confirm_closure_verified(rec) if rec_mode == "closure_verify"
                  else confirm_started(rec))
        # BYOA runners rebind this preclaim row themselves after claim_next has
        # produced the active task claim and Work Session. A generic post-launch
        # upsert here would race that update and erase the exact binding.
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
        else:
            runner_registration = (
                register_runner_session(rec, claimed_wake, inventory) if started else None
            )
            # COORD-34: non-BYOA claimed-task boots must publish a successful bind
            # before Watch/Chat may open. Incomplete/failed register fails the wake.
            if started and (rec or {}).get("claim_id"):
                if (not runner_registration
                        or runner_registration.get("error")
                        or runner_registration.get("error_code") == "runner_bind_incomplete"):
                    started = False
                    result_reason = (
                        (runner_registration or {}).get("error_code")
                        or (runner_registration or {}).get("error")
                        or "runner_bind_incomplete"
                    )
                else:
                    result_reason = "started"
            else:
                result_reason = "started" if started else "launch_failed"
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
            _try("POST", P_COMPLETE_WAKE, {"project": PROJECT, "wake_id": wake_id,
                                           "runner_session_id": result["runner_session_id"],
                                           "agent_id": (w.get("selector") or {}).get("agent_id"),
                                           "result": result})
        acted.append({"wake_id": wake_id, **result})
    return {"host_id": host_id, "pending": len(wakes), "acted": acted,
            "refused": refused,
            "runner_controls": controls}


def run(interval=10, once=False):
    inv = default_inventory()
    registered = False
    last_register_at = 0.0
    drain_advertised = False
    register_every = max(10, int(inv.get("heartbeat_ttl_s") or 60) // 2)
    while True:
        now = time.time()
        drain_request = co_drain.discover_request()
        advertised = co_drain.inventory_for_drain(inv) if drain_request else inv
        if drain_request:
            placement = ((advertised.get("capacity") or {}).get("placement") or {})
            placement["drain_state"] = "draining"
        should_register = (not registered or now - last_register_at >= register_every
                           or bool(drain_request) != drain_advertised)
        if should_register:
            reg = _try("POST", P_REGISTER_HOST, advertised)
            registered = bool(reg)
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
