"""BUG-174: an admitted coordination repair can claim an In Review task."""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


tmp = Path(tempfile.mkdtemp(prefix="bug174-repair-claim-"))
workspace_tmp = Path(tempfile.mkdtemp(prefix="bug174-worktree-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(tmp / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(tmp / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(tmp / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"
(tmp / "projects").mkdir()
worktree = workspace_tmp / "worktree"
worktree.mkdir()

import store  # noqa: E402
from switchboard.application.commands import connect_dispatch  # noqa: E402


P = "switchboard"
HEAD = "a" * 40
AGENT = "agent/codex/bug-174-test"
RUNNER = "run-bug174-repair"
HOST = "host/bug174"

store.init_db(P)
task = store.create_task({
    "task_id": "BUG-174-REPRO",
    "workstream_id": "BUG",
    "title": "repair claim repro",
    "status": "In Review",
    "ui_impact": "no",
    "git_state": {
        "head_sha": HEAD,
        "pr_number": 174,
        "pr_url": "https://github.com/example/projectplanner/pull/174",
    },
}, actor="bug174-test", project=P)

# A normal implementation claimant remains unable to take an In Review task.
control = store.create_task({
    "task_id": "BUG-174-CONTROL",
    "workstream_id": "BUG",
    "title": "ordinary start control",
    "status": "In Review",
    "ui_impact": "no",
}, actor="bug174-test", project=P)
denied = store.claim_task(
    control["task_id"], "agent/control", actor="bug174-test", project=P)
assert denied == {
    "claimed": False,
    "reason": "status_not_ready",
    "task_id": control["task_id"],
    "status": "In Review",
}


def dispatch(route: str, generation: str):
    return connect_dispatch.enqueue_task(
        task, project=P, actor="bug174-test", runtime="codex",
        caller_agent_id=AGENT, generation_ref=generation,
        role="implementation", source_sha=HEAD,
        reason_code="missing_executed_test_run", route=route,
    )


repair = dispatch("coordination_retry", "repair-1")
wake = next(
    row for row in store.list_wake_intents(project=P)
    if row["wake_id"] == repair["wake_id"]
)
policy = wake["policy"]
lifecycle = policy["lifecycle"]
assert lifecycle["route"] == "coordination_retry"
assert policy["execution_assignment"]["route"] == "coordination_retry"

work_session = store.create_work_session({
    "agent_id": AGENT,
    "task_id": task["task_id"],
    "repo_role": "canonical",
    "storage_mode": "worktree",
    "worktree_path": str(worktree),
    "branch": "codex/BUG-174-repair-claim",
    "upstream": "origin/codex/BUG-174-repair-claim",
    "base_sha": "b" * 40,
    "head_sha": HEAD,
    "status": "active",
    "dirty_status": "clean",
    "policy_profile": "code_strict",
    "hygiene": {"repo_preflight": {
        "ok": True,
        "verdict": "pass",
        "dirty": False,
        "branch": "codex/BUG-174-repair-claim",
        "expected_branch": "codex/BUG-174-repair-claim",
        "base_sha": "b" * 40,
        "head_sha": HEAD,
        "upstream": "origin/codex/BUG-174-repair-claim",
        "findings": [],
    }},
}, actor="bug174-test", project=P)["work_session"]

store.upsert_runner_session({
    "runner_session_id": RUNNER,
    "host_id": HOST,
    "agent_id": AGENT,
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
}, actor="bug174-test", project=P)

now = time.time()
with store._conn(P) as c:
    c.execute(
        "UPDATE wake_intents SET status='claimed',claimed_at=?,claimed_by_host=? "
        "WHERE wake_id=?",
        (now, HOST, wake["wake_id"]),
    )
    c.execute(
        "UPDATE resource_leases SET lease_state='active' WHERE id=?",
        (lifecycle["execution_id"],),
    )
    c.execute(
        "INSERT INTO direct_session_tokens("
        "token_hash,project_id,task_id,agent_id,host_id,wake_id,"
        "runner_session_id,issued_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (hashlib.sha256(b"bug174-token").hexdigest(), P, task["task_id"],
         AGENT, HOST, wake["wake_id"], RUNNER, now, now + 3600),
    )

claim = store._claim_task_impl(
    task["task_id"],
    AGENT,
    principal_id=f"direct-session/{RUNNER}",
    work_session_id=work_session["work_session_id"],
    require_work_session=True,
    session_policy_profile="code_strict",
    actor="bug174-test",
    project=P,
)
assert claim["claimed"] is True, claim
assert claim["task"]["status"] == "In Review", claim
authority = claim["dispatch_reason"]["repair_execution"]
assert authority["execution_id"] == lifecycle["execution_id"]
assert authority["generation"] == lifecycle["generation"]
assert authority["role"] == "implementation"
assert claim["dispatch_reason"]["workflow_status_preserved"] == "In Review"

print("BUG-174 admitted repair claim: passed")
