"""BUG-202: exact assigned remediation can claim In Review or Blocked work."""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


tmp = Path(tempfile.mkdtemp(prefix="bug202-remediation-claim-"))
workspace_tmp = Path(tempfile.mkdtemp(prefix="bug202-worktree-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(tmp / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(tmp / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(tmp / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"
(tmp / "projects").mkdir()

import store  # noqa: E402
from switchboard.application.commands import connect_dispatch  # noqa: E402
from execution_policy_fixture import (  # noqa: E402
    install_ready_execution_policy,
    ready_execution_context,
)


P = "switchboard"
HEAD = "a" * 40

store.init_db(P)
install_ready_execution_policy(P)
connect_dispatch.execution_context.resolve = lambda **kwargs: ready_execution_context(
    kwargs["task_id"], runtime=kwargs["runtime"])


def assigned_claim(status: str, role: str, suffix: str):
    task = store.create_task({
        "task_id": f"BUG-202-{suffix}",
        "workstream_id": "BUG",
        "title": f"{status} {role} claim",
        "status": status,
        "ui_impact": "no",
        "git_state": {
            "head_sha": HEAD,
            "pr_number": 202,
            "pr_url": "https://github.com/example/projectplanner/pull/202",
        },
    }, actor="bug202-test", project=P)
    agent = f"agent/codex/{task['task_id'].lower()}"
    runner = f"run-bug202-{suffix.lower()}"
    host = f"host/bug202-{suffix.lower()}"
    dispatched = connect_dispatch.enqueue_task(
        task, project=P, actor="bug202-test", runtime="codex",
        caller_agent_id=agent, generation_ref=f"{role}:{suffix}",
        role=role, source_sha=HEAD,
        reason_code="required_exact_head_ci_failed", route=role,
    )
    wake = next(
        row for row in store.list_wake_intents(project=P)
        if row["wake_id"] == dispatched["wake_id"]
    )
    policy = wake["policy"]
    lifecycle = policy["lifecycle"]
    worktree = workspace_tmp / suffix.lower()
    worktree.mkdir()
    branch = f"codex/{task['task_id']}-{suffix.lower()}"
    work_session = store.create_work_session({
        "agent_id": agent,
        "task_id": task["task_id"],
        "repo_role": "canonical",
        "storage_mode": "worktree",
        "worktree_path": str(worktree),
        "branch": branch,
        "upstream": f"origin/{branch}",
        "base_sha": "b" * 40,
        "head_sha": HEAD,
        "status": "active",
        "dirty_status": "clean",
        "policy_profile": "code_strict",
        "hygiene": {"repo_preflight": {
            "ok": True,
            "verdict": "pass",
            "dirty": False,
            "branch": branch,
            "expected_branch": branch,
            "base_sha": "b" * 40,
            "head_sha": HEAD,
            "upstream": f"origin/{branch}",
            "findings": [],
        }},
    }, actor="bug202-test", project=P)["work_session"]
    store.upsert_runner_session({
        "runner_session_id": runner,
        "host_id": host,
        "agent_id": agent,
        "runtime": "codex",
        "task_id": task["task_id"],
        "claim_id": "",
        "status": "running",
        "metadata": {
            "wake_id": wake["wake_id"],
            "connect_assignment": True,
            "assignment_schema": "switchboard.connect.assignment.v1",
            "assignment_id": policy["assignment"]["assignment_id"],
            "execution_id": lifecycle["execution_id"],
            "execution_generation": lifecycle["generation"],
            "execution_role": lifecycle["role"],
            "execution_head_sha": lifecycle["head_sha"],
            "lease_epoch": lifecycle["fence_epoch"],
        },
    }, actor="bug202-test", project=P)
    now = time.time()
    with store._conn(P) as c:
        c.execute(
            "UPDATE wake_intents SET status='completed',claimed_at=?,"
            "claimed_by_host=?,completed_at=? "
            "WHERE wake_id=?",
            (now, host, now, wake["wake_id"]),
        )
        c.execute(
            "UPDATE resource_leases SET lease_state='active' WHERE id=?",
            (lifecycle["execution_id"],),
        )
        c.execute(
            "INSERT INTO direct_session_tokens("
            "token_hash,project_id,task_id,agent_id,host_id,wake_id,"
            "runner_session_id,issued_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (hashlib.sha256(f"bug202-{suffix}".encode()).hexdigest(), P,
             task["task_id"], agent, host, wake["wake_id"], runner, now,
             now + 3600),
        )
        durable_wake = c.execute(
            "SELECT status FROM wake_intents WHERE wake_id=?",
            (wake["wake_id"],),
        ).fetchone()
    assert durable_wake["status"] == "completed"
    claim = store._claim_task_impl(
        task["task_id"],
        agent,
        principal_id=f"direct-session/{runner}",
        work_session_id=work_session["work_session_id"],
        require_work_session=True,
        session_policy_profile="code_strict",
        actor="bug202-test",
        project=P,
    )
    return task, lifecycle, claim


in_review_task, in_review_lifecycle, in_review_claim = assigned_claim(
    "In Review", "remediation", "IN-REVIEW")
assert in_review_claim["claimed"] is True, in_review_claim
assert in_review_claim["task"]["status"] == "In Review", in_review_claim
assert in_review_claim["dispatch_reason"]["workflow_status_preserved"] == "In Review"
assert (
    in_review_claim["dispatch_reason"]["assigned_execution"]["execution_id"]
    == in_review_lifecycle["execution_id"]
)

blocked_task, blocked_lifecycle, blocked_claim = assigned_claim(
    "Blocked", "remediation", "BLOCKED")
assert blocked_claim["claimed"] is True, blocked_claim
assert blocked_claim["task"]["status"] == "Blocked", blocked_claim
assert blocked_claim["dispatch_reason"]["workflow_status_preserved"] == "Blocked"
assert blocked_claim["work_session"]["status"] == "bound"
assert (
    blocked_claim["dispatch_reason"]["assigned_execution"]["execution_id"]
    == blocked_lifecycle["execution_id"]
)
with store._conn(P) as c:
    durable_claim = c.execute(
        "SELECT status,execution_generation,execution_role "
        "FROM task_claims WHERE id=?",
        (blocked_claim["claim_id"],),
    ).fetchone()
assert durable_claim["status"] == "active"
assert durable_claim["execution_generation"] == blocked_lifecycle["generation"]
assert durable_claim["execution_role"] == "remediation"

wrong_role_task, _, wrong_role_claim = assigned_claim(
    "Blocked", "review_merge", "WRONG-ROLE")
assert wrong_role_claim["claimed"] is True, wrong_role_claim
assert wrong_role_claim["task"]["status"] == "Blocked"
assert wrong_role_claim["dispatch_reason"]["workflow_status_preserved"] == "Blocked"
assert wrong_role_claim["dispatch_reason"]["assigned_execution"]["role"] == "review_merge"

unassigned = store.create_task({
    "task_id": "BUG-202-UNASSIGNED",
    "workstream_id": "BUG",
    "title": "ordinary blocked claim",
    "status": "Blocked",
    "ui_impact": "no",
}, actor="bug202-test", project=P)
unassigned_claim = store.claim_task(
    unassigned["task_id"], "agent/control", actor="bug202-test", project=P)
assert unassigned_claim == {
    "claimed": False,
    "reason": "status_not_ready",
    "task_id": unassigned["task_id"],
    "status": "Blocked",
}

print("BUG-202 assigned remediation claim admission: passed")
