#!/usr/bin/env python3
"""COORD-99: one Mission Bot authority path reaches the exact runner."""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


tmp = Path(tempfile.mkdtemp(prefix="coord99-mission-authority-"))
workspace = Path(tempfile.mkdtemp(prefix="coord99-worktree-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(tmp / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(tmp / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(tmp / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"
(tmp / "projects").mkdir()

import store  # noqa: E402
from execution_readiness_fixture import configure_ready_project  # noqa: E402
from switchboard.application.mission_bot.driver import (  # noqa: E402
    production_mission_ports,
)


P = "switchboard"
HEAD = "c" * 40
AGENT = "agent/codex/coord-99"
RUNNER = "run-coord99-remediation"
HOST = "host/coord99"
MISSION_KEY = "mission:COORD-99:exact-red-head"
SECRET = "ghs_must_not_reach_runner"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


store.init_db(P)
configure_ready_project(P, actor="coord99-test")
dependency = store.create_task({
    "task_id": "COORD-99-DEP",
    "workstream_id": "COORD",
    "title": "Initially satisfied dependency",
    "status": "Done",
    "ui_impact": "no",
}, actor="coord99-test", project=P)
task = store.create_task({
    "task_id": "COORD-99-CANARY",
    "workstream_id": "COORD",
    "title": "Red CI remediation canary",
    "status": "Blocked",
    "depends_on": [dependency["task_id"]],
    "ui_impact": "no",
    "git_state": {
        "head_sha": HEAD,
        "pr_number": 99,
        "pr_url": "https://github.com/example/projectplanner/pull/99",
    },
}, actor="coord99-test", project=P)
with store._conn(P) as connection:
    connection.execute(
        "INSERT INTO task_git_state("
        "task_id,branch,head_sha,pr_number,pr_url,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            task["task_id"],
            f"codex/{task['task_id']}-canary",
            HEAD,
            99,
            "https://github.com/example/projectplanner/pull/99",
            1.0,
        ),
    )

finding = {
    "code": "required_exact_head_ci_failed",
    "blocking": True,
    "context": "Switchboard CI / VM gate",
    "url": "https://github.com/example/actions/runs/99",
    "summary": "test_open_prs_board.py failed",
}
dossier = {
    "schema": "switchboard.mission_dossier.v1",
    "mission": "remediate",
    "task_id": task["task_id"],
    "head_sha": HEAD,
    "pr_number": 99,
    "reason_code": "required_exact_head_ci_failed",
    "acceptance_findings": [finding],
    "failing_contexts": ["Switchboard CI / VM gate"],
    "failing_check_url": finding["url"],
    "failing_check_summary": finding["summary"],
    "environment": {"GITHUB_TOKEN": SECRET},
}
plan = {
    "task_id": task["task_id"],
    "role": "remediation",
    "head_sha": HEAD,
    "reason_code": "required_exact_head_ci_failed",
    "route": "remediation",
    "pr_number": 99,
    "acceptance_findings": [finding],
    "dossier": dossier,
    "idem_key": MISSION_KEY,
}
scope = store.start_autopilot_scope(
    project=P,
    scope_type="task",
    task_project=P,
    task_id=task["task_id"],
    runtime="codex",
    actor="coord99-test",
)
store.register_agent(
    "switchboard/scoped-owner/coord99-test",
    "scoped-completion-coordinator",
    project=P,
    ttl_s=600,
)
scope_authority = store.acquire_autopilot_scope_lease(
    scope["scope_id"],
    holder_agent_id="switchboard/scoped-owner/coord99-test",
    project=P,
)
ports = production_mission_ports(
    project=P,
    actor="coord99-test",
    agent_id="",
    store_mod=store,
    scope_authority=scope_authority,
    scope_project=P,
)
first = ports.start_task(plan)
second = ports.start_task(plan)
ok(first.get("started") is True, "red CI boots one remediation generation")
ok(second.get("wake_id") == first.get("wake_id"),
   "unchanged mission tick reuses the pending generation")
ok(len(store.list_wake_intents(task_id=task["task_id"], project=P)) == 1,
   "unchanged mission tick creates no duplicate wake")

wake = store.list_wake_intents(task_id=task["task_id"], project=P)[0]
policy = wake["policy"]
contract = policy["execution_assignment"]
safe_dossier = policy["lifecycle"].get("mission_dossier") or {}
ok(policy["lifecycle"].get("mission_key") == MISSION_KEY,
   "coordination lifecycle retains the one Mission Bot key")
ok(contract.get("launch_pointer", {}).get("evidence_url") == finding["url"],
   "immutable assignment carries only the starting CI URL")
ok("mission_dossier" not in contract and "mission_key" not in contract,
   "immutable runner assignment excludes interpreted Mission Bot state")
ok(SECRET not in str(safe_dossier) and "<redacted>" in str(safe_dossier),
   "coordination retains a prompt-safe dossier outside the runner assignment")

lifecycle = policy["lifecycle"]
branch = f"codex/{task['task_id']}-canary"
work_session = store.create_work_session({
    "agent_id": policy["assignment"]["principal_ref"],
    "task_id": task["task_id"],
    "repo_role": "canonical",
    "storage_mode": "worktree",
    "worktree_path": str(workspace),
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
}, actor="coord99-test", project=P)["work_session"]
store.upsert_runner_session({
    "runner_session_id": RUNNER,
    "host_id": HOST,
    "agent_id": policy["assignment"]["principal_ref"],
    "runtime": "codex",
    "task_id": task["task_id"],
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
}, actor="coord99-test", project=P)
store.complete_wake(
    wake["wake_id"],
    result={"started": True, "runner_session_id": RUNNER},
    runner_session_id=RUNNER,
    agent_id=policy["assignment"]["principal_ref"],
    actor="coord99-test",
    project=P,
)
now = time.time()
with store._conn(P) as c:
    c.execute(
        "UPDATE resource_leases SET lease_state='active' WHERE id=?",
        (lifecycle["execution_id"],),
    )
    c.execute(
        "INSERT INTO direct_session_tokens("
        "token_hash,project_id,task_id,agent_id,host_id,wake_id,"
        "runner_session_id,issued_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (hashlib.sha256(b"coord99").hexdigest(), P, task["task_id"],
         policy["assignment"]["principal_ref"], HOST, wake["wake_id"], RUNNER,
         now, now + 3600),
    )
    # The dependency moves after Coordination admitted the execution. Claim
    # attachment must not reinterpret that later board projection.
    c.execute(
        "UPDATE tasks SET status='Not Started' WHERE task_id=?",
        (dependency["task_id"],),
    )

claim = store._claim_task_impl(
    task["task_id"],
    policy["assignment"]["principal_ref"],
    principal_id=f"direct-session/{RUNNER}",
    work_session_id=work_session["work_session_id"],
    require_work_session=True,
    session_policy_profile="code_strict",
    actor="coord99-test",
    project=P,
)
ok(claim.get("claimed") is True,
   "completed wake and changed board facts cannot veto exact execution attach")
ok(claim.get("task", {}).get("status") == "Blocked",
   "claim attachment preserves the board projection")
ok(claim.get("dispatch_reason", {}).get("assigned_execution", {}).get(
    "execution_id") == lifecycle["execution_id"],
   "claim binds the exact server execution generation")

print(f"\nCOORD-99 mission authority path: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
