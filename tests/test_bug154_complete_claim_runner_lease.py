#!/usr/bin/env python3
"""BUG-154: complete_claim surrenders only its bound runner generation."""
from __future__ import annotations

import os
import hashlib
import json
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


tmp = Path(tempfile.mkdtemp(prefix="bug154-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(tmp / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(tmp / "registry.db")
projects_dir = tmp / "projects"
projects_dir.mkdir()
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(projects_dir)
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_RUNNER_DIR"] = str(tmp / "runner-state")

import store  # noqa: E402
from adapters import agent_host  # noqa: E402


P = "switchboard"
HEAD = "a" * 40
HOST = "host/bug154"
HOST_PRINCIPAL = "host-principal-bug154"


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
    return task, work_session


store.init_db(P)
task, work_session = make_claim("surrender exact runner generation")
other_task = store.create_task({
    "workstream_id": "BUG", "title": "preserve another generation",
    "status": "Not Started", "ui_impact": "no",
}, actor="bug154-test", project=P)


def register(runner_id: str, task_id: str, claim_id: str, *,
             role: str = "implementation", generation: int = 1):
    with store._conn(P) as c:
        c.execute("INSERT OR IGNORE INTO wake_intents(wake_id,source,reason,selector_json,"
                  "policy_json,status,requested_at,claimed_at,claimed_by_host,task_id,"
                  "placement_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (f"wake-{runner_id}", "connect", "test", json.dumps({
                      "task_id": task_id, "agent_id": f"agent/{task_id}",
                      "runtime": "codex"}), json.dumps({"mode": "connect", "assignment": {
                          "schema": "switchboard.connect.assignment.v1",
                          "assignment_id": f"assign-{task_id.lower()}",
                          "work_ref": f"task:{P}:{task_id}"}}), "claimed", time.time(),
                   time.time(), HOST, task_id, "{}"))
    return store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": HOST,
        "agent_id": f"agent/{task_id}", "runtime": "codex",
        "task_id": task_id, "claim_id": claim_id, "status": "running",
        "heartbeat_ttl_s": 60,
        "metadata": {"wake_id": f"wake-{runner_id}",
                     "connect_assignment": True,
                     "assignment_schema": "switchboard.connect.assignment.v1",
                     "assignment_id": f"assign-{task_id.lower()}",
                     "execution_id": f"exec-{runner_id}",
                     "execution_generation": generation,
                     "execution_role": role,
                     "execution_head_sha": HEAD,
                     "lease_epoch": 1},
    }, principal_id=HOST_PRINCIPAL, actor=HOST, project=P)


now = time.time()
with store._conn(P) as c:
    c.execute("INSERT INTO principals(id,kind,display_name,project,scopes,token_hash,created_at) "
              "VALUES (?,?,?,?,?,?,?)", (HOST_PRINCIPAL, "agent_host", HOST, P,
               json.dumps(["write:agent_host"]), "host-token-hash-bug154", now))
    c.execute("INSERT INTO agent_hosts(host_id,principal_id,registered_at,heartbeat_at,status) "
              "VALUES (?,?,?,?,?)", (HOST, HOST_PRINCIPAL, now, now, "online"))
    c.execute("INSERT INTO agent_host_enrollments(enrollment_id,project_id,host_id,owner_user_id,"
              "bootstrap_hash,bootstrap_expires_at,principal_id,status,created_at,updated_at) "
              "VALUES (?,?,?,?,?,?,?,?,?,?)",
              ("enroll-bug154", P, HOST, "user/bug154", "bootstrap-bug154", now + 3600,
               HOST_PRINCIPAL, "active", now, now))
    c.execute("INSERT INTO wake_intents(wake_id,source,reason,selector_json,policy_json,status,"
              "requested_at,claimed_at,claimed_by_host,task_id,placement_json) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              ("wake-run-bug154-bound", "connect", "test", json.dumps({
                  "task_id": task["task_id"], "agent_id": f"agent/{task['task_id']}",
                  "runtime": "codex"}), json.dumps({"mode": "connect", "assignment": {
                      "schema": "switchboard.connect.assignment.v1",
                      "assignment_id": f"assign-{task['task_id'].lower()}",
                      "work_ref": f"task:{P}:{task['task_id']}"}}), "claimed", now, now,
               HOST, task["task_id"], "{}"))

bound_work_session = work_session["work_session_id"]
# Production Connect shape: the supervised generation starts with null claim
# and Work Session metadata.  Its authenticated direct-session heartbeat then
# binds the exact claim; completion never infers identity from the task.
bound = register("run-bug154-bound", task["task_id"], "")
with store._conn(P) as c:
    c.execute("INSERT INTO direct_session_tokens(token_hash,project_id,task_id,agent_id,host_id,"
              "wake_id,runner_session_id,issued_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
              (hashlib.sha256(b"bug154-token").hexdigest(), P, task["task_id"],
               f"agent/{task['task_id']}", HOST, "wake-run-bug154-bound",
               "run-bug154-bound", now, now + 3600))
claim = store.claim_task(
    task["task_id"], f"agent/{task['task_id']}",
    principal_id="direct-session/run-bug154-bound",
    work_session_id=work_session["work_session_id"], require_work_session=True,
    session_policy_profile="code_strict", actor="bug154-test", project=P)
assert claim["claimed"] is True
bound = register("run-bug154-bound", task["task_id"], claim["claim_id"])
review = register("run-bug154-review", task["task_id"], "", role="review", generation=2)
other = register("run-bug154-other", other_task["task_id"], "")
other = register("run-bug154-other", other_task["task_id"], "claim-unrelated")
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
assert completed["completed"] is False, completed
assert completed.get("stopping") is True, completed
assert completed["pending_host_ack"] is True
assert completed["execution_id"] == "run-bug154-bound"
assert store.get_task(task["task_id"], project=P)["status"] == "In Progress"

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
assert late["error_code"] == "runner_generation_fenced"

# A terminal receipt for the wrong fenced epoch cannot release ownership or
# expose review before the exact supervised generation is dead.
wrong_epoch = store.upsert_runner_session({
    "runner_session_id": "run-bug154-bound", "host_id": HOST,
    "task_id": task["task_id"], "claim_id": claim["claim_id"],
    "status": "expired", "metadata": {
        "terminalized_by": "runner_lease_expiry",
        "execution_generation": 1, "execution_role": "implementation",
        "lease_epoch": 99,
    },
}, principal_id=HOST_PRINCIPAL, actor=HOST, project=P)
assert wrong_epoch["error_code"] == "terminal_ack_identity_mismatch"
assert store.get_task(task["task_id"], project=P)["status"] == "In Progress"
with store._conn(P) as c:
    still_owned = c.execute(
        "SELECT status FROM task_claims WHERE id=?", (claim["claim_id"],)).fetchone()
assert still_owned["status"] == "active"

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
                dict(body or {}), principal_id=HOST_PRINCIPAL, actor=HOST, project=P)
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
assert store.get_task(task["task_id"], project=P)["status"] == "In Review"
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
        "terminalized_by": "runner_lease_expiry",
    },
}, principal_id=HOST_PRINCIPAL, actor=HOST, project=P)
assert terminal["status"] == "expired"

# A successful physical kill followed by a failed POST remains durable across
# daemon restart and is retried even though the supervisor no longer lists the
# process locally.
pending = {
    "project": P, "runner_session_id": "run-restart-proof",
    "host_id": "host/bug154", "task_id": task["task_id"],
    "status": "expired", "metadata": {"terminalized_by": "runner_lease_expiry"},
}
agent_host._persist_pending_stop_receipt(pending)
old_try = agent_host._try
try:
    agent_host._try = lambda *_args, **_kwargs: {"error": "network_down"}
    failed_retry = agent_host._drain_pending_stop_receipts("host/bug154")
    assert failed_retry[0]["expired"] is False
    assert agent_host._pending_stop_receipt_path("run-restart-proof").exists()
    agent_host._try = lambda *_args, **_kwargs: {"ok": True}
    recovered = agent_host._drain_pending_stop_receipts("host/bug154")
    assert recovered[0]["expired"] is True
    assert not agent_host._pending_stop_receipt_path("run-restart-proof").exists()
finally:
    agent_host._try = old_try

print("BUG-154 runner lease surrender tests passed")
