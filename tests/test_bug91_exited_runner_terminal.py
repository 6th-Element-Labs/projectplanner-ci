#!/usr/bin/env python3
"""BUG-91: a process the supervisor reports dead goes terminal on the next tick.

Previously a runner whose process exited was simply skipped by the renewal loop.
It then drifted: still `running` centrally for the rest of its lease, then
`stale`, then swept to `expired` — and for that whole window it remained the
newest row the browser could find for the task, so clicking the task opened a
runner window for a process that no longer existed.
"""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401
from adapters import agent_host

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


host_id = "host/bug91-mac"
inventory = {"host_id": host_id, "repo_root": str(ROOT)}
posted: list[tuple[str, str, dict]] = []


def fake_try(method, path, body=None):
    posted.append((method, path, dict(body or {})))
    return {"runner_session_id": (body or {}).get("runner_session_id"),
            "status": (body or {}).get("status")}


saved_try, saved_drain = agent_host._try, agent_host._drain_runners
agent_host._try = fake_try

# One live claim-bound session, one whose process the supervisor says is gone,
# and one already-terminal row that must not be re-reported every tick.
agent_host._drain_runners = lambda selected_host: [
    {"runner_session_id": "run_live", "task_id": "BUG-91-A", "host_id": selected_host,
     "runtime": "codex", "status": "running", "alive": True, "pid": 111,
     "claim_id": "claim-live", "cwd": str(ROOT),
     "metadata": {"wake_id": "wake-live", "work_session_id": "ws-live"}},
    {"runner_session_id": "run_dead", "task_id": "BUG-91-B", "host_id": selected_host,
     "runtime": "codex", "status": "running", "alive": False, "pid": 222,
     "claim_id": "claim-dead", "cwd": str(ROOT),
     "metadata": {"wake_id": "wake-dead", "work_session_id": "ws-dead"}},
    {"runner_session_id": "run_already_done", "task_id": "BUG-91-C", "host_id": selected_host,
     "runtime": "codex", "status": "exited", "alive": False, "pid": 333,
     "claim_id": "claim-done", "cwd": str(ROOT),
     "metadata": {"wake_id": "wake-done", "work_session_id": "ws-done"}},
]

try:
    results = agent_host.renew_live_direct_runners(inventory)
    # Index runner heartbeats only — SIMPLIFY-3 may also POST complete_wake
    # for the same runner_session_id in the death tick (BUG-102 wake repair).
    by_id = {str(body.get("runner_session_id")): body
             for _method, _path, body in posted
             if _path == agent_host.P_HEARTBEAT_RUNNER
             or "heartbeat_ttl_s" in body
             or body.get("status") in {"running", "exited", "failed"}}

    ok("run_live" in by_id and by_id["run_live"].get("status") == "running",
       "a supervisor-alive claim-bound session is still renewed as running")
    ok(by_id.get("run_live", {}).get("heartbeat_ttl_s") == 180,
       "the live renewal keeps the three-minute lease")

    dead = by_id.get("run_dead") or {}
    ok(dead.get("status") == "exited",
       "a session whose process the supervisor reports dead is reported terminal immediately")
    ok((dead.get("metadata") or {}).get("terminalized_by") == "host_supervisor",
       "the terminal report records that the host supervisor observed the exit")
    ok(dead.get("heartbeat_ttl_s") is None,
       "a terminal report does not renew a lease — it ends the session")

    ok("run_already_done" not in by_id,
       "an already-terminal row is not re-reported on every subsequent tick")

    outcomes = {str(row.get("runner_session_id")): row for row in results}
    ok(outcomes.get("run_dead", {}).get("terminalized") is True,
       "the tick summary reports the terminalization so it is auditable")
    ok(outcomes.get("run_live", {}).get("renewed") is True,
       "the tick summary still reports the live renewal")
finally:
    agent_host._try, agent_host._drain_runners = saved_try, saved_drain

print(f"\nBUG-91 exited runner goes terminal: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
