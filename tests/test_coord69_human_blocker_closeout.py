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
from switchboard.application.commands import task_execution  # noqa: E402
from switchboard.connect.execution_assignment import build_execution_assignment  # noqa: E402
from switchboard.domain.completion import routing  # noqa: E402
from switchboard.storage.repositories import runner as runner_repo  # noqa: E402
from switchboard.storage.repositories import tasks as tasks_repo  # noqa: E402

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
        # Server-stamped resolve_write_actor provenance (MCP edge normally sets).
        "binding": "registered_agent",
        "agent_id": AGENT,
        "source_tool": "agent_requires_human",
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

    # Dependency healing must not reinterpret an explicit human lifecycle block
    # as a legacy dependency-derived block.
    dep = _make_task("completed dependency", status="Done", agent="")
    with _conn(P) as c:
        c.execute(
            "UPDATE tasks SET depends_on=? WHERE task_id=?",
            (json.dumps([dep["task_id"]]), TASK),
        )
        healed = tasks_repo._heal_dependency_blocked_tasks_in(
            c, task_ids=[TASK], actor="test/dependency-heal",
        )
    ok(TASK not in healed, "dependency healing preserves task.human_blocker")
    task_after_heal = store.get_task(TASK, project=P)
    ok(task_after_heal.get("status") == "Blocked",
       f"dependency scan leaves board Blocked (got {task_after_heal.get('status')})")

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
        "binding": "registered_agent",
        "agent_id": AGENT,
        "source_tool": "agent_requires_human",
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

    stale_task = _make_task(
        "stale generation refuses human blocker",
        agent="agent/codex/stale",
    )
    stale_session = _make_session(
        stale_task["task_id"], "agent/codex/stale", "run_stale",
    )
    original_fence = human_blocker_cmd._fence_session_runner
    human_blocker_cmd._fence_session_runner = lambda *args, **kwargs: {
        "fenced": False,
        "error_code": "stale_execution_generation",
        "mismatched_fields": ["generation"],
    }
    try:
        refused = human_blocker_cmd.execute_mapping({
            "task_id": stale_task["task_id"],
            "work_session_id": stale_session["work_session_id"],
            "reason": "provider_acceptance_capacity_missing",
            "binding": "registered_agent",
            "agent_id": "agent/codex/stale",
            "source_tool": "agent_requires_human",
        }, actor="agent/codex/stale", project=P)
    finally:
        human_blocker_cmd._fence_session_runner = original_fence
    ok(refused.get("recorded") is False,
       "stale execution generation fails closed")
    ok(refused.get("error_code") == "human_blocker_fence_refused",
       f"stale fence refusal is typed ({refused.get('error_code')})")
    stale_task_row = store.get_task(stale_task["task_id"], project=P)
    ok(stale_task_row.get("status") == "In Progress",
       "stale fence refusal does not mutate board status")
    stale_ws_row = store.get_work_session(
        stale_session["work_session_id"], project=P,
    )
    ok(stale_ws_row.get("status") == "active",
       "stale fence refusal does not block Work Session")
    with _conn(P) as c:
        stale_attention_count = c.execute(
            "SELECT COUNT(*) AS n FROM attention_requests WHERE task_id=?",
            (stale_task["task_id"],),
        ).fetchone()["n"]
    ok(stale_attention_count == 0,
       "stale fence refusal creates no Needs-you side effect")

    # BUG-200: a Work Session has a truthful repository head even when the
    # immutable implementation execution was intentionally admitted headless.
    # Fencing must compare the execution head (empty) with the execution lease,
    # not substitute the Work Session commit and report a false stale-head race.
    headless_task = _make_task(
        "headless execution human blocker",
        agent="agent/codex/headless",
    )
    headless_session = _make_session(
        headless_task["task_id"], "agent/codex/headless", "run_headless",
    )
    with _conn(P) as c:
        c.execute(
            "UPDATE work_sessions SET env_json=? WHERE work_session_id=?",
            (json.dumps({
                **(headless_session.get("env") or {}),
                "assignment_exact_head_sha": "",
            }), headless_session["work_session_id"]),
        )
    headless_session = store.get_work_session(
        headless_session["work_session_id"], project=P,
    )
    captured_identity = {}
    original_get_runner = runner_repo.get_runner_session
    original_fence_generation = task_execution.fence_task_generation
    runner_repo.get_runner_session = lambda *args, **kwargs: {"live": True}

    def capture_headless_identity(task_id, identity, **kwargs):
        captured_identity.update(identity)
        return {"fenced": True}

    task_execution.fence_task_generation = capture_headless_identity
    try:
        headless_fence = human_blocker_cmd._fence_session_runner(
            headless_session,
            project=P,
            actor="agent/codex/headless",
            reason="human blocker: provider_acceptance_capacity_missing",
        )
    finally:
        runner_repo.get_runner_session = original_get_runner
        task_execution.fence_task_generation = original_fence_generation
    ok(headless_fence.get("fenced") is True,
       "headless implementation execution can be fenced")
    ok(captured_identity.get("head_sha") == "",
       "explicit empty execution head is preserved over Work Session head")

    original_make_due = runner_repo.make_runner_lease_due
    runner_repo.make_runner_lease_due = lambda *args, **kwargs: {
        "updated": True,
        "generation": 1,
        "lease_epoch": 2,
    }
    try:
        empty_head_fence = task_execution.fence_task_generation(
            headless_task["task_id"],
            captured_identity,
            project=P,
            actor="agent/codex/headless",
        )
    finally:
        runner_repo.make_runner_lease_due = original_make_due
    ok(empty_head_fence.get("fenced") is True,
       "exact-generation fence accepts an empty-but-present execution head")

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

    # Bare update_work_session(blocked) without server provenance must not
    # mint sticky human closeout — agents must call agent_requires_human MCP.
    t3 = _make_task("update_work_session promote", agent="agent/codex/promote")
    T3 = t3["task_id"]
    A3 = "agent/codex/promote"
    s3 = _make_session(T3, A3, "run_promote")
    forged = store.update_work_session(s3["work_session_id"], {
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
                "actor": A3,
                "source_tool": "agent_requires_human",
            }
        },
    }, actor=A3, project=P)
    ok(forged.get("updated") is True, f"update_work_session blocked lands ({forged.get('error')})")
    ok(not (forged.get("human_blocker") or {}).get("recorded"),
       "unstamped update_work_session does not promote human_blocker")
    forged_row = store.get_task(T3, project=P)
    ok(forged_row.get("status") != "Blocked",
       f"unstamped update does not sticky-Block board (got {forged_row.get('status')})")

    # Replaying an already server-stamped blocker still promotes (retry path).
    s4 = _make_session(T3, A3, "run_promote_stamped")
    upd = store.update_work_session(s4["work_session_id"], {
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
                "source_tool": "agent_requires_human",
                "binding": "registered_agent",
                "agent_id": A3,
                "actor": A3,
                "provenance_stamp": "switchboard.resolve_write_actor.v1",
            }
        },
    }, actor=A3, project=P)
    ok(upd.get("updated") is True, f"stamped update_work_session lands ({upd.get('error')})")
    ok(bool(upd.get("human_blocker") and upd["human_blocker"].get("recorded")),
       f"stamped update_work_session promotes via human_blocker ({upd.get('human_blocker_error')})")
    t3_row = store.get_task(T3, project=P)
    ok(t3_row.get("status") == "Blocked",
       f"stamped update_work_session promotes to Blocked (got {t3_row.get('status')})")
    with _conn(P) as c:
        n3 = c.execute(
            "SELECT COUNT(*) AS n FROM attention_requests WHERE task_id=?",
            (T3,),
        ).fetchone()["n"]
    ok(n3 >= 1, f"stamped update_work_session created Needs-you (n={n3})")

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
