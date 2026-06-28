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
absent → --dry, which claims+abandons safely).
"""
import json
import os
import socket
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import switchboard_core as sb  # noqa: E402  (reuses _http + agent_id, same contract)

PROJECT = os.environ.get("PM_PROJECT", "switchboard")
SUPERVISOR = os.path.join(_HERE, "codex", "supervisor.py")
RUN_AGENT = os.path.join(_HERE, "run_agent.py")

# Spec operation → REST path. Centralized so Codex's published paths get pinned in ONE place.
P_REGISTER_HOST = "/ixp/v1/register_host"
P_HEARTBEAT_HOST = "/ixp/v1/heartbeat_host"
P_LIST_WAKES = "/txp/v1/list_wake_intents"
P_CLAIM_WAKE = "/txp/v1/claim_wake"
P_COMPLETE_WAKE = "/txp/v1/complete_wake"


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
    return {
        "project": PROJECT, "host_id": host_id, "hostname": socket.gethostname(),
        "agent_host_version": "0.1.0", "repo_root": repo,
        "runtimes": [{
            "runtime": os.environ.get("PM_RUNTIME", "claude-code"),
            "launcher": "python3", "profiles": ["ixp.v1", "txp.dispatch.v0"],
            "control": {"mode": "hook_deny", "runner_kill": True},
            "lanes": [x for x in os.environ.get("PM_HOST_LANES", "").split(",") if x],
            "capabilities": ["docs", "python", "github", "tests"],
        }],
        "limits": {"max_sessions": int(os.environ.get("PM_HOST_MAX_SESSIONS", "2"))},
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
    want_caps = set(sel.get("capabilities") or [])
    for rt in inventory["runtimes"]:
        if want_rt and rt["runtime"] != want_rt:
            continue
        if want_lane and rt.get("lanes") and want_lane not in rt["lanes"]:
            continue
        if want_caps and not want_caps.issubset(set(rt.get("capabilities") or [])):
            continue
        return rt
    return None


def wake_mode(wake):
    """Choose the safe launch mode for a wake.

    Lane-scoped wakes may enter the claim_next loop. Lane-less wakes are message-only by
    construction: they can register and read inbox, but must never ask for global work.
    """
    policy = (wake or {}).get("policy") or {}
    selector = (wake or {}).get("selector") or {}
    explicit = (policy.get("mode") or "").strip()
    if explicit in ("inbox_only", "message_only"):
        return "inbox_only"
    if explicit == "claim_next" and selector.get("lane"):
        return "claim_next"
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


def launch_command(wake, inventory):
    """Build the supervisor command for a wake without executing it."""
    sel = wake.get("selector") or {}
    agent_id = sel.get("agent_id") or sel.get("runtime") or "claude-code"
    lane = sel.get("lane") or ""
    runtime = sel.get("runtime") or (eligible_runtime(wake, inventory) or {}).get("runtime") or "claude-code"
    work_mod = os.environ.get("PM_AGENT_WORK_MODULE", "")
    mode = wake_mode(wake)
    if mode == "inbox_only":
        idle = os.environ.get("PM_AGENT_HOST_INBOX_IDLE_SECONDS", "6")
        child = ["python3", RUN_AGENT, "--runtime", runtime,
                 "--inbox-only", "--idle-seconds", idle]
    else:
        child = ["python3", RUN_AGENT, "--runtime", runtime,
                 "--lanes", lane, "--max-tasks", "1"]
        child += (["--work-module", work_mod] if work_mod else ["--dry"])
    cmd = ["python3", SUPERVISOR, "start", "--agent-id", agent_id,
           "--cwd", inventory["repo_root"], "--"] + child
    return cmd, mode


def launch(wake, inventory):
    """Spawn a supervised run_agent for this wake via supervisor.py (the proven CLI). Returns the
    supervisor session record (with runner_session_id, pid) or None on failure."""
    cmd, mode = launch_command(wake, inventory)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        rec = json.loads(out.stdout)
        if isinstance(rec, dict):
            rec["wake_mode"] = mode
        return rec
    except Exception as e:
        print(f"[agent_host] launch failed: {e}", flush=True)
        return None


def confirm_started(rec, grace_s=4.0):
    """Confirm the launched process is alive after a short grace (proxy for 'runtime came up')."""
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


def run_once(inventory):
    """One daemon iteration. Returns a summary of what it did (for tests + logging)."""
    host_id = inventory["host_id"]
    _try("POST", P_HEARTBEAT_HOST, {"project": PROJECT, "host_id": host_id,
                                    "active_sessions": active_session_count(inventory)})
    listed = _try("GET", f"{P_LIST_WAKES}?project={PROJECT}&status=pending") or {}
    wakes = listed.get("wake_intents") or listed.get("wakes") or []
    acted = []
    cap = inventory["limits"]["max_sessions"]
    for w in wakes:
        if active_session_count(inventory) + len(acted) >= cap:
            print("[agent_host] at capacity; leaving remaining wakes for other hosts", flush=True)
            break
        if not eligible_runtime(w, inventory):
            continue  # not ours — let an eligible host claim it (substrate records if none do)
        wake_id = w.get("wake_id")
        claimed = _try("POST", P_CLAIM_WAKE, {"project": PROJECT, "host_id": host_id, "wake_id": wake_id})
        if not claimed or not (claimed.get("claimed", True)):
            continue  # another host won it (atomic claim)
        rec = launch(w, inventory)
        started = confirm_started(rec)
        result = {"started": started, "runner_session_id": (rec or {}).get("runner_session_id"),
                  "wake_mode": (rec or {}).get("wake_mode") or wake_mode(w),
                  "reason": "started" if started else "launch_failed"}
        _try("POST", P_COMPLETE_WAKE, {"project": PROJECT, "wake_id": wake_id,
                                       "runner_session_id": result["runner_session_id"],
                                       "agent_id": (w.get("selector") or {}).get("agent_id"),
                                       "result": result})
        acted.append({"wake_id": wake_id, **result})
    return {"host_id": host_id, "pending": len(wakes), "acted": acted}


def run(interval=10, once=False):
    inv = default_inventory()
    registered = False
    last_register_at = 0.0
    register_every = max(10, int(inv.get("heartbeat_ttl_s") or 60) // 2)
    while True:
        now = time.time()
        if not registered or now - last_register_at >= register_every:
            reg = _try("POST", P_REGISTER_HOST, inv)
            registered = bool(reg)
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
