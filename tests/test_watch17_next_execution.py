#!/usr/bin/env python3
"""WATCH-17: get_task_execution names the next hop after a C3 hand-off.

Observed 2026-08-18 on atlas/DEPLOY-1: the implementation runner completed its
claim, the host stopped it, and for ~4 minutes get_task_execution reported
``panel: Ready / No Connect wake is in flight`` while the completion owner had
already routed ``review_merge`` for the next generation. A launcher that
trusted that projection re-issued start_task and hit live_execution_conflict.
The projection must carry ``next_execution`` from the persisted completion
route, and the compact MCP summary must keep it.
"""
from __future__ import annotations

from path_setup import ROOT  # noqa: E402,F401

from switchboard.application.commands import task_execution  # noqa: E402
from switchboard.mcp.tools import read_summaries  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def completion(route, **extra):
    base = {
        "schema": "switchboard.completion_projection.v1",
        "task_id": "DEPLOY-1", "pr_number": 280, "head_sha": "0e64ccba",
        "state": "active", "route": route, "reason_code": "",
        "route_owner": "review/merge coordinator", "desired_role": "review_merge",
        "desired_head": "0e64ccba", "retry_deadline": None,
        "current_effect": "start review/merge", "board_status": "In Review",
        "attempt": 1, "state_version": 3, "merged_sha": None, "terminal": False,
    }
    base.update(extra)
    return base


# --- helper ------------------------------------------------------------------
nxt = task_execution._next_execution({"completion_projection": completion("review_merge")})
ok(nxt is not None, "review_merge route yields a next_execution")
ok(nxt["schema"] == task_execution.NEXT_EXECUTION_SCHEMA, "schema is switchboard.next_execution.v1")
ok(nxt["route"] == "review_merge" and nxt["effect"] == "start review/merge", "route + effect come from the completion owner")
ok(nxt["owner"] == "review/merge coordinator" and nxt["desired_head"] == "0e64ccba", "owner and desired head carried")
ok(nxt["trigger"] == "completion_owner", "trigger names the completion owner, not board status")

ok(task_execution._next_execution({}) is None, "no completion projection -> None")
ok(task_execution._next_execution({"completion_projection": completion("none")}) is None, "route none -> None")
ok(task_execution._next_execution({"completion_projection": completion("review_merge", terminal=True, merged_sha="abc")}) is None,
   "terminal (merged) projection -> None")
ok(task_execution._next_execution({"completion_projection": completion("wait")})["route"] == "wait", "wait route is surfaced")
ok(task_execution._next_execution({"completion_projection": completion("human")})["route"] == "human", "human route is surfaced")

# --- compact MCP summary keeps the field --------------------------------------
envelope = {
    "schema": task_execution.SCHEMA, "command": "get_task_execution", "task_id": "DEPLOY-1",
    "project": "atlas", "execution_id": None, "lifecycle_phase": "not_started",
    "running": False, "starting": False, "resumable_review": False,
    "has_ended_session": True, "available_commands": ["start_task"],
    "panel": {"state": "idle", "label": "Ready", "detail": "x", "next_execution": nxt},
    "next_execution": nxt,
    "execution": {"schema": "s", "task_id": "DEPLOY-1", "task": {"task_id": "DEPLOY-1", "status": "In Review"}},
}
compact = read_summaries.task_execution(envelope)
ok(compact.get("next_execution") == nxt, "compact summary carries next_execution")
ok(compact["panel"].get("next_execution") == nxt, "compact panel carries next_execution")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
