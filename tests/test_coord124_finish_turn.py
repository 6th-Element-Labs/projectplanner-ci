#!/usr/bin/env python3
"""COORD-124 — one bounded, atomic finish_turn evidence submission."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


tmp = Path(tempfile.mkdtemp(prefix="coord124-finish-turn-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(tmp / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(tmp / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(tmp / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"
(tmp / "projects").mkdir()

import store  # noqa: E402
from switchboard.application.commands import executed_test_runs  # noqa: E402
from switchboard.application.commands import finish_turn as finish_command  # noqa: E402
from switchboard.contracts.claims import FinishTurnCommand  # noqa: E402
from switchboard.storage.repositories import finish_turns  # noqa: E402


P = "switchboard"
HEAD = "a" * 40
BRANCH = "agent/switchboard/COORD-124/g7"
PR = 1240
PR_URL = f"https://github.com/example/projectplanner/pull/{PR}"
EXECUTION = "execlease-coord124-g7"
GENERATION = 7
RUNNER = "run-coord124-g7"
HOST = "host/coord124"
HOST_PRINCIPAL = "host-principal-coord124"
AGENT = "agent/codex/coord-124"


def ready(evidence, *, project, actor):
    return {
        "schema": "switchboard.pr_ready.v1",
        "status": "already_ready",
        "is_draft": False,
        "pr_number": PR,
        "pr_url": PR_URL,
        "head_ref": BRANCH,
        "head_sha": HEAD,
        "repository": "example/projectplanner",
    }


def command_payload(**updates):
    payload = {
        "task_id": task["task_id"],
        "claim_id": claim["claim_id"],
        "execution_id": EXECUTION,
        "generation": GENERATION,
        "work_session_id": session["work_session_id"],
        "branch": BRANCH,
        "head_sha": HEAD,
        "pr_number": PR,
        "pr_url": PR_URL,
        "executed_test_run_id": test_run_id,
        "git_diff_check": "passed",
        "project": P,
    }
    payload.update(updates)
    return payload


def storage_call(*, provider_ready=True, **updates):
    payload = command_payload(**updates)
    cmd = FinishTurnCommand.from_mapping(payload)
    evidence = cmd.completion_evidence()
    if provider_ready:
        evidence["pr_ready"] = ready(evidence, project=P, actor=AGENT)
    return finish_turns.finish_turn(
        claim_id=cmd.claim_id,
        task_id=cmd.task_id,
        execution_id=cmd.execution_id,
        generation=cmd.generation,
        work_session_id=cmd.work_session_id,
        executed_test_run_id=cmd.executed_test_run_id,
        evidence=evidence,
        actor=AGENT,
        project=P,
    )


def durable_state():
    with store._conn(P) as c:
        claim_row = c.execute(
            "SELECT status,lease_epoch FROM task_claims WHERE id=?",
            (claim["claim_id"],),
        ).fetchone()
        runner_row = c.execute(
            "SELECT metadata_json FROM runner_sessions WHERE runner_session_id=?",
            (RUNNER,),
        ).fetchone()
        lease_row = c.execute(
            "SELECT lease_state,fence_epoch FROM resource_leases WHERE id=?",
            (EXECUTION,),
        ).fetchone()
    metadata = json.loads(runner_row["metadata_json"] or "{}")
    return {
        "claim_status": claim_row["status"],
        "claim_epoch": claim_row["lease_epoch"],
        "completion_handoff": metadata.get("completion_handoff"),
        "lease_surrender": metadata.get("lease_surrender"),
        "lease_state": lease_row["lease_state"],
        "fence_epoch": lease_row["fence_epoch"],
    }


store.init_db(P)
task = store.create_task({
    "task_id": "COORD-124-TEST",
    "workstream_id": "COORD",
    "title": "bounded finish turn",
    "description": "policy_profile:code_strict",
    "status": "Not Started",
    "ui_impact": "no",
}, actor="coord124-test", project=P)
session = store.create_work_session({
    "task_id": task["task_id"],
    "agent_id": AGENT,
    "runtime": "codex",
    "repo_role": "canonical",
    "repo": "example/projectplanner",
    "default_branch": "master",
    "branch": BRANCH,
    "upstream": f"origin/{BRANCH}",
    "base_sha": "b" * 40,
    "head_sha": HEAD,
    "storage_mode": "worktree",
    "worktree_path": str(tmp),
    "status": "active",
    "dirty_status": "clean",
    "conflict_marker_count": 0,
    "policy_profile": "code_strict",
    "hygiene": {"repo_preflight": {
        "ok": True,
        "verdict": "pass",
        "dirty": False,
        "branch": BRANCH,
        "expected_branch": BRANCH,
        "base_sha": "b" * 40,
        "head_sha": HEAD,
        "upstream": f"origin/{BRANCH}",
        "findings": [],
    }},
}, actor="coord124-test", project=P)["work_session"]

now = time.time()
with store._conn(P) as c:
    c.execute(
        "INSERT INTO principals(id,kind,display_name,project,scopes,token_hash,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (HOST_PRINCIPAL, "agent_host", HOST, P,
         json.dumps(["write:agent_host"]), "coord124-host-token", now),
    )
    c.execute(
        "INSERT INTO agent_hosts(host_id,principal_id,registered_at,heartbeat_at,status) "
        "VALUES (?,?,?,?,?)", (HOST, HOST_PRINCIPAL, now, now, "online"),
    )
    c.execute(
        "INSERT INTO agent_host_enrollments(enrollment_id,project_id,host_id,owner_user_id,"
        "bootstrap_hash,bootstrap_expires_at,principal_id,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("enroll-coord124", P, HOST, "user/coord124", "coord124-bootstrap",
         now + 3600, HOST_PRINCIPAL, "active", now, now),
    )
    c.execute(
        "INSERT INTO wake_intents(wake_id,source,reason,selector_json,policy_json,status,"
        "requested_at,claimed_at,claimed_by_host,task_id,placement_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("wake-coord124", "connect", "test", json.dumps({
            "task_id": task["task_id"], "agent_id": AGENT, "runtime": "codex",
        }), json.dumps({"mode": "connect", "assignment": {
            "schema": "switchboard.connect.assignment.v1",
            "assignment_id": "assignment-coord124",
            "work_ref": f"task:{P}:{task['task_id']}",
        }}), "claimed", now, now, HOST, task["task_id"], "{}"),
    )

runner_payload = {
    "runner_session_id": RUNNER,
    "host_id": HOST,
    "agent_id": AGENT,
    "runtime": "codex",
    "task_id": task["task_id"],
    "claim_id": "",
    "status": "running",
    "heartbeat_ttl_s": 60,
    "metadata": {
        "wake_id": "wake-coord124",
        "connect_assignment": True,
        "assignment_schema": "switchboard.connect.assignment.v1",
        "assignment_id": "assignment-coord124",
        "execution_id": EXECUTION,
        "execution_generation": GENERATION,
        "execution_role": "implementation",
        "execution_head_sha": "",
        "lease_epoch": 1,
    },
}
registered = store.upsert_runner_session(
    runner_payload, principal_id=HOST_PRINCIPAL, actor=HOST, project=P)
assert registered.get("runner_session_id") == RUNNER, registered
with store._conn(P) as c:
    c.execute(
        "INSERT INTO direct_session_tokens(token_hash,project_id,task_id,agent_id,host_id,"
        "wake_id,runner_session_id,issued_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (hashlib.sha256(b"coord124-token").hexdigest(), P, task["task_id"],
         AGENT, HOST, "wake-coord124", RUNNER, now, now + 3600),
    )
    c.execute(
        "INSERT INTO resource_leases(id,agent_id,principal_id,task_id,resource_type,names,"
        "claimed_at,ttl_seconds,execution_role,execution_generation,fence_epoch,lease_state,"
        "head_sha,wake_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (EXECUTION, AGENT, HOST_PRINCIPAL, task["task_id"], "execution",
         json.dumps([task["task_id"], "implementation"]), now, 7200,
         "implementation", GENERATION, 1, "active", "", "wake-coord124"),
    )

claim = store.claim_task(
    task["task_id"], AGENT,
    principal_id=f"direct-session/{RUNNER}",
    work_session_id=session["work_session_id"],
    require_work_session=True,
    session_policy_profile="code_strict",
    actor=AGENT,
    project=P,
)
assert claim.get("claimed") is True, claim
runner_payload["claim_id"] = claim["claim_id"]
registered = store.upsert_runner_session(
    runner_payload, principal_id=HOST_PRINCIPAL, actor=HOST, project=P)
assert registered.get("runner_session_id") == RUNNER, registered

recorded = executed_test_runs.execute_mapping({
    "task_id": task["task_id"],
    "work_session_id": session["work_session_id"],
    "commands": ["python3 tests/test_coord124_finish_turn.py"],
    "passed": True,
    "exit_code": 0,
    "output_sha256": "c" * 64,
    "branch": BRANCH,
    "head_sha": HEAD,
}, actor=AGENT, principal_id=f"direct-session/{RUNNER}", project=P)
assert recorded.get("recorded") is True, recorded
test_run_id = recorded["run"]["run_id"]

# The public contract is bounded: agents cannot smuggle lifecycle authority into it.
invalid = finish_command.execute_mapping_result(
    {**command_payload(), "done": True}, actor=AGENT, ensure_ready=ready)
assert invalid.get("error_code") == "invalid_finish_turn", invalid

# Provider exact-head proof is enforced before persistence is invoked.
called = []


def fake_finish(**kwargs):
    called.append(kwargs)
    return {"accepted": True, "stopping": True}


def stale_ready(evidence, *, project, actor):
    return {**ready(evidence, project=project, actor=actor), "head_sha": "d" * 40}


missing_diff_payload = command_payload()
missing_diff_payload.pop("git_diff_check")
missing_diff = finish_command.execute_mapping_result(
    missing_diff_payload, actor=AGENT, finish=fake_finish, ensure_ready=ready)
assert missing_diff.get("error_code") == "invalid_finish_turn", missing_diff
assert called == [], called

stale_provider = finish_command.execute_mapping_result(
    command_payload(), actor=AGENT, finish=fake_finish, ensure_ready=stale_ready)
assert stale_provider.get("error_code") == "completion_pr_head_mismatch", stale_provider
assert called == [], called
application_ok = finish_command.execute_mapping_result(
    command_payload(), actor=AGENT, finish=fake_finish, ensure_ready=ready)
assert application_ok.get("accepted") is True and len(called) == 1, application_ok
assert called[0]["generation"] == GENERATION
assert called[0]["executed_test_run_id"] == test_run_id
assert called[0]["evidence"]["pr_ready"]["head_sha"] == HEAD

# Every storage refusal is read-only: no partial handoff, fence, or claim mutation.
initial = durable_state()
stale_generation = storage_call(generation=GENERATION - 1)
assert stale_generation.get("error_code") == "finish_turn_generation_mismatch", stale_generation
assert durable_state() == initial

missing_test = storage_call(executed_test_run_id="testrun-missing")
assert missing_test.get("error_code") == "finish_turn_test_receipt_mismatch", missing_test
assert durable_state() == initial

missing_push = storage_call(provider_ready=False)
assert missing_push.get("error_code") == "finish_turn_push_unproven", missing_push
assert durable_state() == initial

stale_head = storage_call(head_sha="d" * 40)
assert stale_head.get("error_code") == "finish_turn_head_mismatch", stale_head
assert durable_state() == initial

unbound_session = storage_call(work_session_id="ws-missing")
assert unbound_session.get("error_code") == "finish_turn_work_session_unbound", unbound_session
assert durable_state() == initial

store.update_work_session(
    session["work_session_id"], {"dirty_status": "dirty"}, actor=AGENT, project=P)
dirty = storage_call()
assert dirty.get("error_code") == "finish_turn_dirty_diff", dirty
assert durable_state() == initial
store.update_work_session(
    session["work_session_id"], {"dirty_status": "clean"}, actor=AGENT, project=P)

# One valid submission delegates to complete_claim and starts the existing C3 stop.
accepted = storage_call()
assert accepted.get("accepted") is True, accepted
assert accepted.get("stopping") is True and accepted.get("pending_host_ack") is True
assert accepted["execution_id"] == EXECUTION
assert accepted["runner_session_id"] == RUNNER
state = durable_state()
assert state["claim_status"] == "active", state
assert state["lease_state"] == "stopping" and state["fence_epoch"] == 2, state
assert state["completion_handoff"]["evidence"]["finish_turn"]["generation"] == GENERATION
assert state["completion_handoff"]["evidence"]["executed_test_run"]["run_id"] == test_run_id

# Identical retries are idempotent; a different payload for the same turn conflicts.
again = storage_call()
assert again.get("accepted") is True and again.get("idempotent") is True, again
conflict = storage_call(executed_test_run_id="testrun-other")
assert conflict.get("error_code") == "finish_turn_idempotency_conflict", conflict
assert durable_state() == state

mcp_source = (ROOT / "src/switchboard/mcp/tools/claims.py").read_text(encoding="utf-8")
assert '"finish_turn"' in mcp_source
assert "final_status" not in mcp_source[mcp_source.index("def finish_turn("):
                                        mcp_source.index("def record_executed_test_run(")]

print("COORD-124 finish_turn tests passed")
