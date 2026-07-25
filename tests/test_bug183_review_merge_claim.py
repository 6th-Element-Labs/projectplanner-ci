"""BUG-183: an admitted review/merge execution can claim an In Review task."""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


tmp = Path(tempfile.mkdtemp(prefix="bug183-review-claim-"))
workspace_tmp = Path(tempfile.mkdtemp(prefix="bug183-worktree-"))
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
AGENT = "agent/codex/bug-183-test"
RUNNER = "run-bug183-review"
HOST = "host/bug183"

store.init_db(P)
task = store.create_task({
    "task_id": "BUG-183-REPRO",
    "workstream_id": "BUG",
    "title": "review merge claim repro",
    "status": "In Review",
    "ui_impact": "no",
    "git_state": {
        "head_sha": HEAD,
        "pr_number": 183,
        "pr_url": "https://github.com/example/projectplanner/pull/183",
    },
}, actor="bug183-test", project=P)

dispatched = connect_dispatch.enqueue_task(
    task, project=P, actor="bug183-test", runtime="codex",
    caller_agent_id=AGENT, generation_ref=f"review:{HEAD}",
    role="review_merge", source_sha=HEAD,
    reason_code="review_required", route="review_merge",
)
wake = next(
    row for row in store.list_wake_intents(project=P)
    if row["wake_id"] == dispatched["wake_id"]
)
policy = wake["policy"]
lifecycle = policy["lifecycle"]
contract = policy["execution_assignment"]
assert contract["desired_role"] == "review_merge"
assert contract["exact_head_sha"] == HEAD
assert contract["claim_expectations"] == {
    "required": True,
    "work_session_required": True,
    "role": "review_merge",
}

work_session = store.create_work_session({
    "agent_id": AGENT,
    "task_id": task["task_id"],
    "repo_role": "canonical",
    "storage_mode": "worktree",
    "worktree_path": str(worktree),
    "branch": "codex/BUG-183-review-merge-claim",
    "upstream": "origin/codex/BUG-183-review-merge-claim",
    "base_sha": "b" * 40,
    "head_sha": HEAD,
    "status": "active",
    "dirty_status": "clean",
    "policy_profile": "code_strict",
    "hygiene": {"repo_preflight": {
        "ok": True,
        "verdict": "pass",
        "dirty": False,
        "branch": "codex/BUG-183-review-merge-claim",
        "expected_branch": "codex/BUG-183-review-merge-claim",
        "base_sha": "b" * 40,
        "head_sha": HEAD,
        "upstream": "origin/codex/BUG-183-review-merge-claim",
        "findings": [],
    }},
}, actor="bug183-test", project=P)["work_session"]

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
}, actor="bug183-test", project=P)

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
        (hashlib.sha256(b"bug183-token").hexdigest(), P, task["task_id"],
         AGENT, HOST, wake["wake_id"], RUNNER, now, now + 3600),
    )

claim = store._claim_task_impl(
    task["task_id"],
    AGENT,
    principal_id=f"direct-session/{RUNNER}",
    work_session_id=work_session["work_session_id"],
    require_work_session=True,
    session_policy_profile="code_strict",
    actor="bug183-test",
    project=P,
)
assert claim["claimed"] is True, claim
assert claim["task"]["status"] == "In Review", claim
authority = claim["dispatch_reason"]["assigned_execution"]
assert authority["execution_id"] == lifecycle["execution_id"]
assert authority["generation"] == lifecycle["generation"]
assert authority["role"] == "review_merge"
assert authority["head_sha"] == HEAD
assert claim["dispatch_reason"]["workflow_status_preserved"] == "In Review"
assert claim["work_session"]["status"] == "bound"

with store._conn(P) as c:
    durable_claim = c.execute(
        "SELECT runner_session_id,execution_generation,execution_role "
        "FROM task_claims WHERE id=?", (claim["claim_id"],),
    ).fetchone()
assert durable_claim["runner_session_id"] == RUNNER
assert durable_claim["execution_generation"] == lifecycle["generation"]
assert durable_claim["execution_role"] == "review_merge"

print("BUG-183 admitted review/merge claim: passed")
