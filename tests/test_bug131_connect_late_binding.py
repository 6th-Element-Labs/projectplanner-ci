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
            "credential_admission_phase": "preclaim",
        },
    }


def work_session(status="active"):
    return {
        "work_session_id": "worksession-bug131", "claim_id": "taskclaim-bug131",
        "principal_id": f"direct-session/{RUN_ID}", "task_id": TASK_ID,
        "agent_id": AGENT_ID, "status": status,
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
    agent_host._host_repo_preflight = lambda *_a, **_k: None
    agent_host._try = lambda method, path, body=None: (
        posted.append(dict(body or {})) or {"ok": True})
    renewed = agent_host.renew_live_direct_runners({"host_id": "host/bug131"})
    ok(renewed[0]["renewed"] is True
       and posted[-1]["claim_id"] == "taskclaim-bug131"
       and posted[-1]["metadata"]["work_session_id"] == "worksession-bug131",
       "Connect runner late-binds an active claim and Work Session")

    calls = []
    posted.clear()

    def drain_sessions(**filters):
        calls.append(filters)
        return [] if filters.get("status") == "active" else [work_session("completed")]

    agent_host._drain_work_sessions = drain_sessions
    agent_host.renew_live_direct_runners({"host_id": "host/bug131"})
    ok({"task_id": TASK_ID, "status": "completed"} in calls
       and posted[-1]["claim_id"] == "taskclaim-bug131"
       and posted[-1]["metadata"]["work_session_id"] == "worksession-bug131",
       "Connect runner closes the just-completed Work Session race")
finally:
    agent_host._drain_runners = saved["runners"]
    agent_host._drain_work_sessions = saved["sessions"]
    agent_host._host_repo_preflight = saved["preflight"]
    agent_host._try = saved["try"]


print(f"\nBUG-131 Connect late binding: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
