#!/usr/bin/env python3
"""COORD-69: human-route WS blockers must sticky-Block via DHCP closeout.

Live DOGFOOD-17 wrote WS blocked route=human then abandon_claim, which reset
the board to Not Started and left the runner green — no Needs-you, no
completion stickiness. This pins the promote path.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401

_TMP = Path(tempfile.mkdtemp(prefix="coord69-human-blocker-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(_TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(_TMP / "registry.db")
(_TMP / "projects").mkdir()
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(_TMP / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.application.commands import human_blocker as human_blocker_cmd  # noqa: E402
from switchboard.connect.execution_assignment import build_execution_assignment  # noqa: E402
from switchboard.domain.completion import routing  # noqa: E402

P = "switchboard"
HEAD = "a" * 40
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def _make_task(title: str, *, status="In Progress", agent="agent/codex/x"):
    task = store.create_task({
        "workstream_id": "DOGFOOD",
        "title": title,
        "description": "session_profile:code_strict\ncapacity proof",
        "status": "Not Started",
        "ui_impact": "no",
    }, actor="test", project=P)
    task_id = task["task_id"]
    with _conn(P) as c:
        c.execute(
            "UPDATE tasks SET status=?, assignee=? WHERE task_id=?",
            (status, agent, task_id))
    return store.get_task(task_id, project=P)


def _make_session(task_id: str, agent: str, runner_id: str):
    ws = store.create_work_session({
        "agent_id": agent,
        "task_id": task_id,
        "repo_role": "canonical",
        "storage_mode": "worktree",
        "worktree_path": str(_TMP / f"wt-{task_id}"),
        "branch": f"codex/{task_id}-alerts",
        "upstream": f"origin/codex/{task_id}-alerts",
        "base_sha": "b" * 40,
        "head_sha": HEAD,
        "status": "active",
        "dirty_status": "clean",
        "policy_profile": "code_strict",
        "hygiene": {"repo_preflight": {
            "ok": True, "verdict": "pass", "dirty": False,
            "branch": f"codex/{task_id}-alerts",
            "expected_branch": f"codex/{task_id}-alerts",
            "base_sha": "b" * 40, "head_sha": HEAD,
            "upstream": f"origin/codex/{task_id}-alerts",
            "findings": [],
        }},
        "env": {
            "execution_id": f"exec-{task_id}",
            "assignment_id": f"asg-{task_id}",
            "execution_generation": 1,
            "execution_role": "implementation",
            "lease_epoch": 1,
            "runner_session_id": runner_id,
        },
    }, actor=agent, project=P)
    ok(bool(ws.get("created") and ws.get("work_session")),
       f"work session created for {task_id}: {ws.get('error') or ws.get('errors')}")
    session = ws["work_session"]
    with _conn(P) as c:
        # Best-effort bind runner identity columns when present.
        try:
            c.execute(
                "UPDATE work_sessions SET runner_session_id=?, execution_generation=1, "
                "execution_role='implementation', lease_epoch=1 WHERE work_session_id=?",
                (runner_id, session["work_session_id"]))
        except Exception:
            pass
    return store.get_work_session(session["work_session_id"], project=P)


def _make_claim(task_id: str, agent: str, work_session_id: str) -> str:
    claim_id = f"taskclaim-{task_id.lower()}"
    now = time.time()
    with _conn(P) as c:
        c.execute(
            "INSERT INTO task_claims(id, task_id, agent_id, status, "
            "claimed_at, expires_at, principal_id) VALUES (?,?,?,?,?,?,?)",
            (claim_id, task_id, agent, "active", now, now + 1800, "test"))
        c.execute(
            "UPDATE work_sessions SET claim_id=? WHERE work_session_id=?",
            (claim_id, work_session_id))
        c.execute(
            "INSERT OR IGNORE INTO resource_leases("
            "id, agent_id, principal_id, task_id, resource_type, names, "
            "claimed_at, ttl_seconds, released_at) "
            "VALUES (?,?,?,?,?,?,?,?,NULL)",
            (f"lease-{claim_id}", agent, "test", task_id, "task",
             json.dumps([task_id]), now, 1800))
    return claim_id


try:
    store.init_db(P)

    task = _make_task("human blocker sticky closeout", agent="agent/codex/dogfood-17")
    TASK = task["task_id"]
    AGENT = "agent/codex/dogfood-17"
    session = _make_session(TASK, AGENT, "run_dogfood17")
    ws_id = session["work_session_id"]
    claim_id = _make_claim(TASK, AGENT, ws_id)

    first = human_blocker_cmd.execute_mapping({
        "task_id": TASK,
        "work_session_id": ws_id,
        "reason": "provider_acceptance_capacity_missing",
        "completed_work": "Audited provider inventory; PROTO-7 acceptance passed.",
        "minimum_human_action": "Merge #921; enroll Cursor and Claude hosts.",
        "resume_condition": "Claude and Cursor runtimes launchable.",
        "next_automatic_step": "Re-read SHA and run 3-provider matrix.",
        "evidence": {
            "coord_63_pr": "https://github.com/6th-Element-Labs/projectplanner/pull/921",
            "cursor_connection_present": False,
        },
    }, actor=AGENT, project=P)

    ok(first.get("recorded") is True,
       f"record_human_blocker records ({first.get('error') or first.get('message')})")
    ok(first.get("route") == "human", "receipt route is human")
    ok(bool(first.get("attention_request_id")),
       f"Needs-you attention_request_id present ({first.get('attention_request_id')})")

    task_row = store.get_task(TASK, project=P)
    ok(task_row.get("status") == "Blocked",
       f"board is sticky Blocked (got {task_row.get('status')})")

    ws = store.get_work_session(ws_id, project=P)
    ok(ws.get("status") == "blocked", f"work session is blocked (got {ws.get('status')})")
    blocker = (ws.get("hygiene") or {}).get("blocker") or {}
    ok(blocker.get("route") == "human", "hygiene.blocker.route=human")
    ok(blocker.get("reason") == "provider_acceptance_capacity_missing",
       "blocker reason preserved")

    with _conn(P) as c:
        n = c.execute(
            "SELECT COUNT(*) AS n FROM attention_requests WHERE task_id=?",
            (TASK,),
        ).fetchone()["n"]
    ok(n == 1, f"exactly one Needs-you request (n={n})")

    second = human_blocker_cmd.execute_mapping({
        "task_id": TASK,
        "work_session_id": ws_id,
        "reason": "provider_acceptance_capacity_missing",
        "completed_work": "Audited provider inventory; PROTO-7 acceptance passed.",
        "minimum_human_action": "Merge #921; enroll Cursor and Claude hosts.",
        "resume_condition": "Claude and Cursor runtimes launchable.",
        "next_automatic_step": "Re-read SHA and run 3-provider matrix.",
        "evidence": {
            "coord_63_pr": "https://github.com/6th-Element-Labs/projectplanner/pull/921",
            "cursor_connection_present": False,
        },
    }, actor=AGENT, project=P)
    ok(second.get("idempotent_replay") is True
       or second.get("attention_request_id") == first.get("attention_request_id"),
       "second call is idempotent (same attention)")
    with _conn(P) as c:
        n2 = c.execute(
            "SELECT COUNT(*) AS n FROM attention_requests WHERE task_id=?",
            (TASK,),
        ).fetchone()["n"]
    ok(n2 == 1, f"idempotent: still one attention request (n={n2})")

    abandoned = store.abandon_claim(
        claim_id, reason="provider_acceptance_capacity_missing",
        actor=AGENT, project=P)
    ok(abandoned.get("abandoned") is True, f"claim abandoned ({abandoned})")
    task2 = store.get_task(TASK, project=P)
    ok(task2.get("status") == "Blocked",
       f"abandon leaves Blocked sticky (got {task2.get('status')})")
    ok(abandoned.get("human_closeout_preserved") is True,
       "abandon reports human_closeout_preserved")

    ok(not routing.task_ready_for_dispatch({
        "task_id": TASK,
        "status": "Blocked",
        "active_claims": [],
        "dependency_state": {"ready": True},
        "completion_run": {"route": "human"},
    }), "Blocked+human is not dispatchable")
    ok(not routing.task_ready_for_dispatch({
        "task_id": TASK,
        "status": "Not Started",
        "active_claims": [],
        "dependency_state": {"ready": True},
        "work_session": {
            "status": "blocked",
            "hygiene": {"blocker": {"route": "human"}},
        },
    }), "Not Started + blocked human WS is not dispatchable")

    # Ordinary abandon still resets when no human blocker
    other = _make_task("ordinary abandon reset", agent="agent/codex/other")
    OTHER = other["task_id"]
    OTHER_AGENT = "agent/codex/other"
    other_ws = _make_session(OTHER, OTHER_AGENT, "run_other")
    claim2 = _make_claim(OTHER, OTHER_AGENT, other_ws["work_session_id"])
    ordinary = store.abandon_claim(
        claim2, reason="gave up", actor=OTHER_AGENT, project=P)
    other_task = store.get_task(OTHER, project=P)
    ok(ordinary.get("abandoned") is True, "ordinary abandon works")
    ok(other_task.get("status") == "Not Started",
       f"ordinary abandon resets to Not Started (got {other_task.get('status')})")
    ok(ordinary.get("human_closeout_preserved") is not True,
       "ordinary abandon does not mark human closeout preserved")

    # update_work_session(blocked) promotes the same way
    t3 = _make_task("update_work_session promote", agent="agent/codex/promote")
    T3 = t3["task_id"]
    A3 = "agent/codex/promote"
    s3 = _make_session(T3, A3, "run_promote")
    upd = store.update_work_session(s3["work_session_id"], {
        "status": "blocked",
        "hygiene": {
            "blocker": {
                "route": "human",
                "reason": "provider_acceptance_capacity_missing",
                "completed_work": "checked capacity",
                "minimum_human_action": "enroll host",
                "resume_condition": "host online",
                "next_automatic_step": "retry proof",
                "evidence": {},
            }
        },
    }, actor=A3, project=P)
    ok(upd.get("updated") is True, f"update_work_session blocked lands ({upd.get('error')})")
    ok(bool(upd.get("human_blocker") and upd["human_blocker"].get("recorded")),
       f"update_work_session promotes via human_blocker ({upd.get('human_blocker_error')})")
    t3_row = store.get_task(T3, project=P)
    ok(t3_row.get("status") == "Blocked",
       f"update_work_session promotes to Blocked (got {t3_row.get('status')})")
    with _conn(P) as c:
        n3 = c.execute(
            "SELECT COUNT(*) AS n FROM attention_requests WHERE task_id=?",
            (T3,),
        ).fetchone()["n"]
    ok(n3 >= 1, f"update_work_session created Needs-you (n={n3})")

    contract = build_execution_assignment(
        task_id="COORD-69",
        assignment={"assignment_id": "assignment-x"},
        lifecycle={
            "role": "implementation",
            "execution_id": "exec-1",
            "generation": 1,
            "head_sha": "",
            "pr_number": 0,
            "pr_url": "",
        },
    )
    tools = contract.get("typed_tools") or {}
    ok(tools.get("human_blocker") == "record_human_blocker",
       "execution_assignment names record_human_blocker")
    ok(tools.get("executed_test_run") == "record_executed_test_run",
       "execution_assignment names record_executed_test_run")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nCOORD-69 human blocker closeout: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
