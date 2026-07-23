"""BUG-155: every Start surface coalesces on one canonical execution lease."""
from __future__ import annotations

import os
import hashlib
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from path_setup import ROOT  # noqa: F401

tmp = Path(tempfile.mkdtemp(prefix="bug155-lease-"))
projects = tmp / "projects"
projects.mkdir()
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(tmp / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(tmp / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(projects)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from switchboard.application.commands import connect_dispatch  # noqa: E402

P = "switchboard"
store.init_db(P)
task = store.create_task({
    "task_id": "BUG-155-COALESCE",
    "workstream_id": "BUG",
    "title": "cross-surface execution coalescing",
    "status": "Not Started",
    "ui_impact": "no",
}, actor="bug155-test", project=P)


def start(surface_runtime: str):
    return connect_dispatch.enqueue_task(
        task, project=P, actor=f"surface/{surface_runtime}",
        runtime=surface_runtime, role="implementation")


# UI/desktop/scheduler/remediation providers race one another. Runtime is not
# part of the authority key: the first transaction wins placement; all others
# attach to its durable project+task+role execution.
surfaces = ["codex", "claude-code", "cursor", "codex", "claude-code"]
with ThreadPoolExecutor(max_workers=len(surfaces)) as pool:
    results = list(pool.map(start, surfaces))

wake_ids = {row.get("wake_id") for row in results}
assert len(wake_ids) == 1 and None not in wake_ids, results
wakes = store.list_wake_intents(task_id=task["task_id"], project=P)
assert len(wakes) == 1, wakes
lifecycle = (wakes[0].get("policy") or {}).get("lifecycle") or {}
assert lifecycle.get("execution_id", "").startswith("execlease-"), lifecycle
assert lifecycle.get("generation") == 1
assert lifecycle.get("fence_epoch") == 1
with store._conn(P) as c:
    leases = c.execute(
        "SELECT * FROM resource_leases WHERE task_id=? "
        "AND resource_type='execution' AND released_at IS NULL",
        (task["task_id"],)).fetchall()
assert len(leases) == 1
assert leases[0]["wake_id"] == wakes[0]["wake_id"]
assert leases[0]["execution_role"] == "implementation"
assert leases[0]["lease_state"] == "reserved"

# The sole wake produces one runner, and its authenticated direct-session tuple
# is the only identity allowed to bind one claim and one Work Session.
agent_id = (wakes[0].get("selector") or {})["agent_id"]
work_session = store.create_work_session({
    "agent_id": agent_id, "task_id": task["task_id"],
    "repo_role": "canonical", "storage_mode": "worktree",
    "worktree_path": str(tmp), "branch": "codex/BUG-155-coalesce",
    "upstream": "origin/codex/BUG-155-coalesce",
    "base_sha": "b" * 40, "head_sha": "a" * 40,
    "status": "active", "dirty_status": "clean",
    "policy_profile": "code_strict",
    "hygiene": {"repo_preflight": {
        "ok": True, "verdict": "pass", "dirty": False,
        "branch": "codex/BUG-155-coalesce",
        "expected_branch": "codex/BUG-155-coalesce",
        "base_sha": "b" * 40, "head_sha": "a" * 40,
        "upstream": "origin/codex/BUG-155-coalesce", "findings": [],
    }},
}, actor="bug155-test", project=P)["work_session"]
runner_id = "run-bug155-coalesced"
policy = wakes[0]["policy"]
lifecycle = policy["lifecycle"]
store.upsert_runner_session({
    "runner_session_id": runner_id, "host_id": "host/bug155",
    "agent_id": agent_id, "runtime": "codex", "task_id": task["task_id"],
    "claim_id": "", "status": "running", "metadata": {
        "wake_id": wakes[0]["wake_id"], "connect_assignment": True,
        "assignment_schema": "switchboard.connect.assignment.v1",
        "assignment_id": policy["assignment"]["assignment_id"],
        "execution_id": lifecycle["execution_id"],
        "execution_generation": lifecycle["generation"],
        "execution_role": lifecycle["role"],
        "execution_head_sha": lifecycle["head_sha"],
        "lease_epoch": lifecycle["fence_epoch"],
    },
}, actor="bug155-test", project=P)
now = time.time()
with store._conn(P) as c:
    c.execute(
        "INSERT INTO direct_session_tokens(token_hash,project_id,task_id,agent_id,"
        "host_id,wake_id,runner_session_id,issued_at,expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (hashlib.sha256(b"bug155-coalesce").hexdigest(), P, task["task_id"],
         agent_id, "host/bug155", wakes[0]["wake_id"], runner_id, now, now + 3600))
claim = store.claim_task(
    task["task_id"], agent_id, principal_id=f"direct-session/{runner_id}",
    work_session_id=work_session["work_session_id"], require_work_session=True,
    session_policy_profile="code_strict", actor="bug155-test", project=P)
assert claim["claimed"] is True, claim
with store._conn(P) as c:
    assert c.execute(
        "SELECT COUNT(*) FROM runner_sessions WHERE task_id=?",
        (task["task_id"],)).fetchone()[0] == 1
    assert c.execute(
        "SELECT COUNT(*) FROM task_claims WHERE task_id=? AND status='active'",
        (task["task_id"],)).fetchone()[0] == 1
    assert c.execute(
        "SELECT COUNT(*) FROM work_sessions WHERE task_id=? AND status='active'",
        (task["task_id"],)).fetchone()[0] == 1

print("BUG-155 canonical execution lease coalescing: passed")
