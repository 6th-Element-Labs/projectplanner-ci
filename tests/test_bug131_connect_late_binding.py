#!/usr/bin/env python3
"""BUG-131: Connect runners late-bind their claim and Work Session."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from adapters import agent_host


RUN_ID = "run_bug131_connect"
TASK_ID = "BUG-131"
AGENT_ID = "agent/codex/bug-131"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def runner():
    return {
        "runner_session_id": RUN_ID, "agent_id": AGENT_ID, "runtime": "codex",
        "task_id": TASK_ID, "host_id": "host/bug131", "status": "running",
        "alive": True, "pid": 131, "cwd": str(ROOT), "wake_mode": "connect",
        "control": {"runner_open": True},
        "metadata": {
            "wake_id": "wake-bug131", "connect_assignment": True,
            "assignment_schema": "switchboard.connect.assignment.v1",
            "native_host_execution": True,
            "execution_id": "exec-bug131", "execution_generation": 2,
        },
    }


def work_session(status="active"):
    return {
        "work_session_id": "worksession-bug131", "claim_id": "taskclaim-bug131",
        "principal_id": f"direct-session/{RUN_ID}", "task_id": TASK_ID,
        "agent_id": AGENT_ID, "status": status,
        "env": {"execution_id": "exec-bug131", "execution_generation": 2},
    }


saved = {
    "runners": agent_host._drain_runners,
    "sessions": agent_host._drain_work_sessions,
    "preflight": agent_host._host_repo_preflight,
    "try": agent_host._try,
}
try:
    posted = []
    agent_host._drain_runners = lambda _host: [runner()]
    agent_host._drain_work_sessions = lambda **_filters: [work_session()]
    agent_host._host_repo_preflight = lambda _session, _inventory, metadata: {
        "work_session_id": metadata.get("work_session_id"),
        "branch": "codex/bug131", "ok": True,
    }
    agent_host._try = lambda method, path, body=None: (
        posted.append(dict(body or {})) or {"ok": True})
    renewed = agent_host.renew_live_direct_runners({"host_id": "host/bug131"})
    heartbeat = next(body for body in posted if body.get("status") == "running")
    ok(renewed[0]["renewed"] is True
       and heartbeat["claim_id"] == "taskclaim-bug131"
       and heartbeat["metadata"]["work_session_id"] == "worksession-bug131"
       and heartbeat["metadata"]["host_repo_preflight"]["ok"] is True,
       "Connect runner late-binds and attests without a volatile phase marker")
    paths = []
    posted.clear()

    def record_try(method, path, body=None):
        paths.append(path)
        posted.append(dict(body or {}))
        return {"ok": True}

    agent_host._try = record_try
    agent_host.renew_live_direct_runners({"host_id": "host/bug131"})
    ok(any(path.endswith("/worksession-bug131/preflight") for path in paths),
       "the binding heartbeat immediately requests server-side preflight validation")
    preflight_body = next(
        body for path, body in zip(paths, posted)
        if path.endswith("/worksession-bug131/preflight"))
    ok(preflight_body.get("agent_host_bootstrap_binding") == {
        "wake_id": "wake-bug131", "host_id": "host/bug131",
        "runner_session_id": RUN_ID, "task_id": TASK_ID,
        "agent_id": AGENT_ID,
    }, "preflight validation carries the exact host execution tuple")

    mismatched = work_session()
    mismatched["env"]["execution_generation"] = 1
    ok(agent_host._direct_work_session_binding(runner(), [mismatched]) is None,
       "a Work Session from another execution generation fails closed")
    claimed_runner = runner()
    claimed_runner["claim_id"] = "taskclaim-bug131"
    ok(agent_host._direct_work_session_binding(
        claimed_runner, [work_session()]) is not None,
       "an already-bound claim can still acquire its missing Work Session ID")
    ok(agent_host._direct_work_session_binding(
        claimed_runner,
        [{**work_session(), "claim_id": "taskclaim-other"}]) is None,
       "an already-bound claim cannot join another claim's Work Session")

    calls = []
    posted.clear()
    paths.clear()

    def drain_sessions(**filters):
        calls.append(filters)
        return [] if filters.get("status") == "active" else [work_session("completed")]

    agent_host._drain_work_sessions = drain_sessions
    agent_host.renew_live_direct_runners({"host_id": "host/bug131"})
    heartbeat = next(body for body in posted if body.get("status") == "running")
    ok({"task_id": TASK_ID, "status": "completed"} in calls
       and heartbeat["claim_id"] == "taskclaim-bug131"
       and heartbeat["metadata"]["work_session_id"] == "worksession-bug131",
       "Connect runner closes the just-completed Work Session race")
finally:
    agent_host._drain_runners = saved["runners"]
    agent_host._drain_work_sessions = saved["sessions"]
    agent_host._host_repo_preflight = saved["preflight"]
    agent_host._try = saved["try"]


print(f"\nBUG-131 Connect late binding: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
