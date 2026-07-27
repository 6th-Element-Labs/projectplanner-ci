#!/usr/bin/env python3
"""COORD-67/68 wedge: a finished runner must not own the task that outlived it.

Observed live 2026-07-27 (AUTOPILOT-BREAKDOWNS 57/58). Both tasks completed
hands-off, opened PRs, and went red on a stale base. The completion classifier
correctly routed them to remediation, and every dispatch was refused:

    start_remediation -> "Another agent owns this task."  (retry_count 6, then
                          next_retry_at=null -- permanently wedged)

because the implementation runner's task claim was still ``status='active'`` on
its one-hour TTL, held by ``agent/codex/coord-67`` via principal
``direct-session/run_bf392438d0cc6d02`` -- a runner that had already surrendered
its lease and been reaped.

The rule pinned here:

A claim whose bound execution is recorded terminal is not live ownership. Per
   ADR-0008 C2 a fenced/reaped generation holds no authority, so it must not
   block the completion owner's next role (W4). A claim with no bound execution,
   or one whose execution is still alive, still blocks -- desktop/MCP claims and
   genuinely concurrent agents are unaffected.
The companion defect (BREAKDOWN 58 — the dependency healer resetting the
completion owner's ``Blocked(route=remediation)`` to ``Not Started``) is tracked
separately: the healer already honours an explicit ``block_cause``, so the fix
belongs with whatever writes the route-block, not here.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="coord67-wedge-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from switchboard.storage.repositories import coordination as coordination_repo  # noqa: E402
from switchboard.storage.repositories import tasks as tasks_repo  # noqa: E402
from db.connection import _conn  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if condition:
        passed += 1
    else:
        failed += 1


def _claim(c, task_id, *, claim_id, agent_id, runner_session_id, now):
    c.execute(
        "INSERT INTO task_claims(id,task_id,agent_id,principal_id,status,"
        "claimed_at,expires_at,runner_session_id,execution_generation,"
        "execution_role) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (claim_id, task_id, agent_id,
         f"direct-session/{runner_session_id}" if runner_session_id else None,
         "active", now, now + 3600, runner_session_id, 1, "implementation"),
    )


def _runner(c, runner_session_id, task_id, status, now):
    c.execute(
        "INSERT INTO runner_sessions(runner_session_id,host_id,task_id,agent_id,"
        "runtime,status,started_at,heartbeat_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (runner_session_id, "host/test", task_id, "agent/codex/test", "codex",
         status, now, now, now),
    )


try:
    store.init_db(P)
    now = time.time()

    # --- rule 1: a terminal execution's claim is not live ownership ----------
    dead = store.create_task(
        {"workstream_id": "COORD", "title": "claim held by a reaped runner"},
        actor="coord67-test", project=P)["task_id"]
    live = store.create_task(
        {"workstream_id": "COORD", "title": "claim held by a live runner"},
        actor="coord67-test", project=P)["task_id"]
    desktop = store.create_task(
        {"workstream_id": "COORD", "title": "claim with no bound execution"},
        actor="coord67-test", project=P)["task_id"]

    with _conn(P) as c:
        _runner(c, "run_dead", dead, "expired", now)
        _claim(c, dead, claim_id="taskclaim-dead", agent_id="agent/codex/dead",
               runner_session_id="run_dead", now=now)
        _runner(c, "run_live", live, "running", now)
        _claim(c, live, claim_id="taskclaim-live", agent_id="agent/codex/live",
               runner_session_id="run_live", now=now)
        _claim(c, desktop, claim_id="taskclaim-desktop",
               agent_id="claude/desktop", runner_session_id=None, now=now)

    # The live regression: this returned the reaped runner's claim, so every
    # start_remediation was refused "Another agent owns this task."
    ok(coordination_repo.task_start_ownership(dead, project=P) == {},
       "a claim bound to a REAPED execution does not block the next role")
    ok(str(coordination_repo.task_start_ownership(live, project=P)
           .get("claim_id") or "") == "taskclaim-live",
       "a claim bound to a LIVE execution still blocks (no double-boot)")
    ok(str(coordination_repo.task_start_ownership(desktop, project=P)
           .get("claim_id") or "") == "taskclaim-desktop",
       "a claim with no bound execution still blocks (desktop/MCP ownership)")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nCOORD-67/68 post-completion wedge: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
