#!/usr/bin/env python3
"""BUG-154: complete_claim surrenders only its bound runner generation."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


tmp = Path(tempfile.mkdtemp(prefix="bug154-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(tmp / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(tmp / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(tmp)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from adapters import agent_host  # noqa: E402


P = "switchboard"
HEAD = "a" * 40


def make_claim(title: str):
    task = store.create_task({
        "workstream_id": "BUG", "title": title, "status": "Not Started",
        "ui_impact": "no",
    }, actor="bug154-test", project=P)
    work_session = store.create_work_session({
        "agent_id": f"agent/{task['task_id']}", "task_id": task["task_id"],
        "repo_role": "canonical", "storage_mode": "worktree",
        "worktree_path": str(tmp),
        "branch": f"codex/{task['task_id']}-bug154",
        "upstream": f"origin/codex/{task['task_id']}-bug154",
        "base_sha": "b" * 40, "head_sha": HEAD,
        "status": "active", "dirty_status": "clean",
        "policy_profile": "code_strict",
        "hygiene": {"repo_preflight": {
            "ok": True, "verdict": "pass", "dirty": False,
            "branch": f"codex/{task['task_id']}-bug154",
            "expected_branch": f"codex/{task['task_id']}-bug154",
            "base_sha": "b" * 40, "head_sha": HEAD,
            "upstream": f"origin/codex/{task['task_id']}-bug154",
            "findings": [],
        }},
    }, actor="bug154-test", project=P)["work_session"]
    claim = store.claim_task(
        task["task_id"], f"agent/{task['task_id']}",
        work_session_id=work_session["work_session_id"],
        require_work_session=True, session_policy_profile="code_strict",
        actor="bug154-test", project=P)
    assert claim["claimed"] is True
    return task, claim, work_session


store.init_db(P)
task, claim, work_session = make_claim("surrender exact runner generation")
other_task, other_claim, _other_work_session = make_claim("preserve another generation")


def register(runner_id: str, task_id: str, claim_id: str, *,
             work_session_id: str | None = None):
    return store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": "host/bug154",
        "agent_id": f"agent/{task_id}", "runtime": "codex",
        "task_id": task_id, "claim_id": claim_id, "status": "running",
        "heartbeat_ttl_s": 60,
        "metadata": {"wake_id": f"wake-{runner_id}",
                     "work_session_id": work_session_id or f"ws-{claim_id}"},
    }, actor="host/bug154", project=P)


bound_work_session = work_session["work_session_id"]
# Production Connect shape: the supervised generation exists before its late
# claim-binding heartbeat, but its Work Session identity is already durable.
bound = register("run-bug154-bound", task["task_id"], "",
                 work_session_id=bound_work_session)
review = register("run-bug154-review", task["task_id"], "",
                  work_session_id="ws-review-generation")
other = register("run-bug154-other", other_task["task_id"], other_claim["claim_id"])
assert not bound.get("stale") and not review.get("stale") and not other.get("stale")

completed = store.complete_claim(
    claim["claim_id"], evidence={
        "branch": work_session["branch"], "head_sha": HEAD,
        "pr_url": "https://github.com/example/projectplanner/pull/154",
        "executed_test_run": {
            "schema": "switchboard.executed_test_run.v1",
            "run_id": "bug154-regression", "work_session_id": work_session["work_session_id"],
            "branch": work_session["branch"], "head_sha": HEAD,
            "commands": ["python3 tests/test_bug154_complete_claim_runner_lease.py"],
            "exit_code": 0, "status": "success", "completed_at": time.time(),
            "output_hash": "sha256:" + "c" * 64,
        },
        "git_diff_check": "clean",
    },
    actor="bug154-test", project=P)
assert completed["completed"] is True

surrendered = store.get_runner_session("run-bug154-bound", project=P)
unrelated = store.get_runner_session("run-bug154-other", project=P)
assert surrendered["stale"] is True
assert surrendered["metadata"]["lease_surrender"]["claim_id"] == claim["claim_id"]
assert store.get_runner_session("run-bug154-review", project=P)["stale"] is False
assert unrelated["stale"] is False
assert "lease_surrender" not in unrelated["metadata"]

# A late host heartbeat must neither renew nor resurrect the fenced generation.
prior_heartbeat = surrendered["heartbeat_at"]
late = register("run-bug154-bound", task["task_id"], claim["claim_id"])
assert late["stale"] is True
assert late["heartbeat_at"] == prior_heartbeat

# The existing Agent Host expiry clock stops the supervised process, publishes
# its terminal heartbeat, and leaves the later review generation alive.
old_drain = agent_host._drain_runners
old_action = agent_host.supervisor_action
old_try = agent_host._try
old_drop = agent_host._drop_host_bridge
old_enforcement = os.environ.get("PM_RUNNER_LEASE_ENFORCEMENT")
host_calls = []
try:
    os.environ["PM_RUNNER_LEASE_ENFORCEMENT"] = "1"
    def drain(_host_id):
        rows = store.list_runner_sessions(task_id=task["task_id"],
                                          include_stale=True, project=P)
        return [{**row, "alive": row["runner_session_id"] in {
            "run-bug154-bound", "run-bug154-review"}} for row in rows]

    def action(name, runner_id, options=None):
        host_calls.append((name, runner_id, dict(options or {})))
        return {"alive": False, "status": "killed"}

    def post(method, path, body=None):
        host_calls.append((method, path, dict(body or {})))
        if method == "POST" and path == agent_host.P_HEARTBEAT_RUNNER:
            return store.upsert_runner_session(
                dict(body or {}), actor="host/bug154", project=P)
        return {"ok": True}

    agent_host._drain_runners = drain
    agent_host.supervisor_action = action
    agent_host._try = post
    agent_host._drop_host_bridge = lambda runner_id: host_calls.append(
        ("drop", runner_id))
    enforced = agent_host.expire_runner_leases(
        {"host_id": "host/bug154"}, now=time.time())
finally:
    agent_host._drain_runners = old_drain
    agent_host.supervisor_action = old_action
    agent_host._try = old_try
    agent_host._drop_host_bridge = old_drop
    if old_enforcement is None:
        os.environ.pop("PM_RUNNER_LEASE_ENFORCEMENT", None)
    else:
        os.environ["PM_RUNNER_LEASE_ENFORCEMENT"] = old_enforcement

assert enforced == [{
    "runner_session_id": "run-bug154-bound", "task_id": task["task_id"],
    "reason": "runner_lease_expired", "would_expire": False, "expired": True,
}]
assert ("kill", "run-bug154-bound") == host_calls[0][:2]
assert not any(call[:2] == ("kill", "run-bug154-review") for call in host_calls)
assert store.get_runner_session("run-bug154-bound", project=P)["status"] == "expired"
assert store.get_runner_session("run-bug154-other", project=P)["status"] == "running"
fleet_live_ids = {
    row["runner_session_id"] for row in store.list_runner_sessions(
        task_id=task["task_id"], include_stale=False, project=P)
    if row["status"] in {"ready", "running"}
}
assert "run-bug154-bound" not in fleet_live_ids
assert "run-bug154-review" in fleet_live_ids

# A lease-expiry terminal heartbeat is allowed through the fence and is idempotent.
terminal = store.upsert_runner_session({
    "runner_session_id": "run-bug154-bound", "host_id": "host/bug154",
    "agent_id": f"agent/{task['task_id']}", "runtime": "codex",
    "task_id": task["task_id"], "claim_id": claim["claim_id"],
    "status": "expired", "metadata": {
        "wake_id": "wake-run-bug154-bound",
        "work_session_id": f"ws-{claim['claim_id']}",
        "terminalized_by": "runner_lease_expiry",
    },
}, actor="host/bug154", project=P)
assert terminal["status"] == "expired"

print("BUG-154 runner lease surrender tests passed")
