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
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse

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
    ephemeral = bool(str(os.environ.get("PM_WAKE_ID") or "").strip())
    return {
        "schema": "switchboard.agent_host_placement.v1",
        "host_class": os.environ.get(
            "PM_HOST_CLASS", "ephemeral" if ephemeral else "persistent"),
        "cost_class": os.environ.get(
            "PM_HOST_COST_CLASS", "ephemeral_variable" if ephemeral else "already_paid"),
        "wakeable": True,
        "drain_state": "accepting" if policy.get("allow_work") else "message_only",
        "tenant_ids": _csv(os.environ.get("PM_HOST_TENANTS", "")),
        "projects": _csv(os.environ.get("PM_HOST_PROJECTS", PROJECT)),
        "providers": _csv(os.environ.get("PM_HOST_PROVIDERS", "")),
        "account_affinity_ids": _csv(os.environ.get("PM_HOST_ACCOUNT_AFFINITIES", "")),
        "supports_credential_leases": _truthy(
            os.environ.get("PM_HOST_SUPPORTS_CREDENTIAL_LEASES")),
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
    return {
        "project": PROJECT, "host_id": host_id, "hostname": socket.gethostname(),
        "agent_host_version": "0.1.0", "repo_root": repo,
        "policy": policy,
        "runtimes": [{
            "runtime": runtime,
            "launcher": "codex cloud exec" if cloud_enabled else "python3",
            "profiles": profiles,
            "control": {"mode": "hook_deny", "runner_kill": True, "host_policy": policy["mode"]},
            "policy": policy,
            "lanes": runtime_lanes,
            "capabilities": capabilities,
        }],
        "limits": {"max_sessions": int(os.environ.get("PM_HOST_MAX_SESSIONS", "2"))},
        "capacity": {"active_sessions": 0, "placement": placement},
        "heartbeat_ttl_s": 60,
    }


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
        out = subprocess.run(["python3", SUPERVISOR, "list"], capture_output=True, text=True, timeout=10)
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
        child = ["python3", CLOSURE_VERIFIER, "--project", PROJECT,
                 "--deliverable-id", policy.get("deliverable_id"),
                 "--host-id", inventory.get("host_id", "")]
        if wake.get("wake_id"):
            child += ["--wake-id", wake.get("wake_id")]
    elif mode == "inbox_only":
        idle = os.environ.get("PM_AGENT_HOST_INBOX_IDLE_SECONDS", "6")
        child = ["python3", RUN_AGENT, "--runtime", runtime,
                 "--inbox-only", "--idle-seconds", idle]
        if _truthy(os.environ.get("PM_AGENT_HOST_ACK_INBOX_ONLY", "1")):
            child.append("--ack-inbox")
    else:
        child = ["python3", RUN_AGENT, "--runtime", runtime, "--max-tasks", "1"]
        if lane:
            child += ["--lanes", lane]
        elif not (inventory.get("policy") or {}).get("allow_global_claim"):
            raise ValueError("global claim_next requires PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM=1")
        idle = os.environ.get("PM_AGENT_HOST_CLAIM_IDLE_SECONDS", "6")
        child += ["--idle-seconds", idle]
        child += (["--work-module", work_mod] if work_mod else ["--dry"])
    cmd = ["python3", SUPERVISOR, "start", "--agent-id", agent_id,
           "--cwd", inventory["repo_root"]]
    if runner_session_id:
        cmd += ["--runner-session-id", runner_session_id]
    if wake.get("task_id"):
        cmd += ["--task-id", wake.get("task_id")]
    cmd += ["--"] + child
    return cmd, mode


def launch(wake, inventory, runner_session_id=""):
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
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
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
    """Publish the supervisor session to Switchboard's central runner registry."""
    if not rec or not rec.get("runner_session_id"):
        return None
    binding = ((wake.get("policy") or {}).get("account_binding") or {})
    body = {
        "project": PROJECT,
        "runner_session_id": rec.get("runner_session_id"),
        "host_id": inventory.get("host_id"),
        "agent_id": rec.get("agent_id") or (wake.get("selector") or {}).get("agent_id"),
        "runtime": rec.get("runtime") or (wake.get("selector") or {}).get("runtime"),
        "task_id": rec.get("task_id") or wake.get("task_id") or "",
        "claim_id": rec.get("claim_id") or binding.get("claim_id") or "",
        "pid": rec.get("pid"),
        "status": rec.get("status") or "running",
        "cwd": rec.get("cwd") or inventory.get("repo_root"),
        "control": rec.get("control") or {"tier": "T3", "runner_kill": True,
                                           "managed_process": True},
        "metadata": {
            "wake_id": wake.get("wake_id"),
            "wake_mode": rec.get("wake_mode"),
            "log_path": rec.get("log_path"),
            "command": rec.get("command"),
            "work_session_id": binding.get("work_session_id"),
            "credential_lease_id": binding.get("credential_lease_id"),
            "provider": binding.get("provider"),
            "account_affinity_id": binding.get("account_affinity_id"),
            **(rec.get("metadata") or {}),
        },
        "heartbeat_ttl_s": 3600 if rec.get("cloud_session") else 60,
    }
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


def supervisor_action(action, runner_session_id, options=None):
    options = options or {}
    if action == "snapshot":
        cmd = ["python3", SUPERVISOR, "snapshot", runner_session_id]
    elif action == "health":
        cmd = ["python3", SUPERVISOR, "status", runner_session_id]
    elif action == "logs":
        cmd = ["python3", SUPERVISOR, "snapshot", runner_session_id]
    elif action == "kill":
        cmd = ["python3", SUPERVISOR, "kill", runner_session_id,
               "--grace-seconds", str(options.get("grace_seconds") or 5.0),
               "--signal", options.get("signal") or "TERM"]
    elif action == "open":
        return {"error": "not_supported", "reason": "runner_open is not implemented by this host"}
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
            ["python3", SUPERVISOR, "list"], capture_output=True, text=True, timeout=10)
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
    _try("POST", P_HEARTBEAT_HOST, {"project": PROJECT, "host_id": host_id,
                                    "active_sessions": active_session_count(inventory)})
    controls = handle_runner_controls(inventory)
    listed = _try("GET", f"{P_LIST_WAKES}?project={PROJECT}&status=pending") or {}
    wakes = wakes_bound_to_host(listed.get("wake_intents") or listed.get("wakes") or [])
    acted = []
    cap = inventory["limits"]["max_sessions"]
    for w in wakes:
        if active_session_count(inventory) + len(acted) >= cap:
            print("[agent_host] at capacity; leaving remaining wakes for other hosts", flush=True)
            break
        if not eligible_runtime(w, inventory):
            continue  # not ours — let an eligible host claim it (substrate records if none do)
        wake_id = w.get("wake_id")
        binding = ((w.get("policy") or {}).get("account_binding") or {})
        runner_session_id = ""
        credential_lease_id = ""
        if binding:
            runner_session_id = _runner_session_id_for_wake(w, host_id)
            registered = _register_preclaim_runner(w, inventory, runner_session_id)
            if not registered or registered.get("error"):
                continue
            lease = _acquire_provider_lease(w, inventory, runner_session_id)
            credential_lease_id = str(lease.get("lease_id") or "")
            if not credential_lease_id:
                continue
        claimed = _try("POST", P_CLAIM_WAKE, {
            "project": PROJECT,
            "host_id": host_id,
            "wake_id": wake_id,
            "runner_session_id": runner_session_id,
            "credential_lease_id": credential_lease_id,
        })
        if not claimed or not (claimed.get("claimed", True)):
            if credential_lease_id:
                _release_provider_lease(credential_lease_id, "wake_claim_not_acquired")
            continue  # another host won it (atomic claim)
        claimed_wake = claimed.get("wake") or w
        rec = (launch(claimed_wake, inventory, runner_session_id=runner_session_id)
               if runner_session_id else launch(claimed_wake, inventory))
        rec_mode = (rec or {}).get("wake_mode") or wake_mode(w, inventory)
        started = (confirm_closure_verified(rec) if rec_mode == "closure_verify"
                  else confirm_started(rec))
        runner_registration = register_runner_session(
            rec, claimed_wake, inventory) if started else None
        usage_registration = report_cloud_usage(
            rec, claimed_wake) if started and rec.get("cloud_session") else None
        if credential_lease_id and not started:
            _release_provider_lease(credential_lease_id, "runtime_launch_failed")
        result = {"started": started, "runner_session_id": (rec or {}).get("runner_session_id"),
                  "wake_mode": (rec or {}).get("wake_mode") or wake_mode(w, inventory),
                  "reason": "started" if started else "launch_failed",
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
        _try("POST", P_COMPLETE_WAKE, {"project": PROJECT, "wake_id": wake_id,
                                       "runner_session_id": result["runner_session_id"],
                                       "agent_id": (w.get("selector") or {}).get("agent_id"),
                                       "result": result})
        acted.append({"wake_id": wake_id, **result})
    return {"host_id": host_id, "pending": len(wakes), "acted": acted,
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
