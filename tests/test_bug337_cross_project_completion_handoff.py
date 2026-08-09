#!/usr/bin/env python3
"""BUG-337: shared Agent Hosts preserve exact completion handoffs."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT


TMP = Path(tempfile.mkdtemp(prefix="bug337-cross-project-completion-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP / "projects")
os.environ["PM_AUTH_MODE"] = "required"

import auth  # noqa: E402
import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.storage.repositories.agent_host_enrollments import (  # noqa: E402
    PERSONAL_EXECUTION_POLICY,
)


SOURCE = "atlas"
TARGET = "switchboard"
HOST = "host/bug337-mac"
HOST_PRINCIPAL = "host-bug337"
HOST_TOKEN = "bug337-host-token"
HEAD = "3" * 40
BASE = "2" * 40


def inventory() -> dict:
    local_auth = {
        "available": True,
        "runtime": "codex",
        "provider_credential_exported": False,
    }
    return {
        "host_id": HOST,
        "hostname": "bug337-mac",
        "agent_host_version": "0.4.35",
        "repo_root": str(ROOT),
        "runtimes": [{
            "runtime": "codex",
            "provider": "openai-codex",
            "lanes": [],
            "capabilities": ["docs", "github", "python", "tests"],
            "policy": {"allow_work": True, "allow_global_claim": False},
            "local_auth": local_auth,
        }],
        "limits": {"max_sessions": 8},
        "capacity": {
            "active_sessions": 0,
            "owner": {
                "user_id": "user/bug337",
                "tenant_allowlist": ["org-6th-element-labs"],
                "project_allowlist": [SOURCE, TARGET],
                "provider_allowlist": ["openai-codex"],
            },
            "local_auth": local_auth,
        },
        "heartbeat_ttl_s": 60,
    }


def create_completion(suffix: str, pr_number: int) -> dict:
    task = store.create_task({
        "workstream_id": "BUG",
        "title": f"BUG-337 cross-project completion {suffix}",
        "status": "Not Started",
        "ui_impact": "no",
    }, actor="bug337-test", project=TARGET)
    task_id = task["task_id"]
    agent_id = f"agent/{task_id.lower()}"
    branch = f"codex/{task_id}-bug337"
    work_session = store.create_work_session({
        "agent_id": agent_id,
        "task_id": task_id,
        "repo_role": "canonical",
        "storage_mode": "worktree",
        "worktree_path": str(TMP),
        "branch": branch,
        "upstream": f"origin/{branch}",
        "base_sha": BASE,
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
            "base_sha": BASE,
            "head_sha": HEAD,
            "upstream": f"origin/{branch}",
            "findings": [],
        }},
    }, actor="bug337-test", project=TARGET)["work_session"]

    wake_id = f"wake-{suffix}"
    runner_id = "run_" + hashlib.sha256(
        f"{wake_id}:{HOST}".encode()).hexdigest()[:16]
    execution_id = f"execlease-{suffix}"
    assignment_id = f"assignment-{suffix}"
    now = time.time()
    with _conn(TARGET) as connection:
        connection.execute(
            "INSERT INTO wake_intents("
            "wake_id,source,reason,selector_json,policy_json,status,requested_at,"
            "claimed_at,claimed_by_host,task_id,placement_json) "
            "VALUES (?,?,?,?,?,'claimed',?,?,?,?,?)",
            (
                wake_id,
                "bug337-test",
                "cross-project completion proof",
                json.dumps({
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "runtime": "codex",
                }),
                json.dumps({
                    "mode": "connect",
                    "assignment": {
                        "schema": "switchboard.connect.assignment.v1",
                        "assignment_id": assignment_id,
                        "work_ref": f"task:{TARGET}:{task_id}",
                    },
                }),
                now,
                now,
                HOST,
                task_id,
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO resource_leases("
            "id,agent_id,principal_id,task_id,resource_type,names,claimed_at,"
            "ttl_seconds,execution_role,execution_generation,fence_epoch,"
            "lease_state,head_sha,wake_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                execution_id,
                agent_id,
                HOST_PRINCIPAL,
                task_id,
                "execution",
                json.dumps([task_id, "implementation"]),
                now,
                7200,
                "implementation",
                1,
                1,
                "active",
                HEAD,
                wake_id,
            ),
        )
        connection.execute(
            "INSERT INTO direct_session_tokens("
            "token_hash,project_id,task_id,agent_id,host_id,wake_id,"
            "runner_session_id,issued_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                hashlib.sha256(f"token-{suffix}".encode()).hexdigest(),
                TARGET,
                task_id,
                agent_id,
                HOST,
                wake_id,
                runner_id,
                now,
                now + 3600,
            ),
        )

    def register(claim_id: str, status: str = "running", **metadata: object) -> dict:
        return store.upsert_runner_session({
            "runner_session_id": runner_id,
            "host_id": HOST,
            "agent_id": agent_id,
            "runtime": "codex",
            "task_id": task_id,
            "claim_id": claim_id,
            "status": status,
            "control": {
                "tier": "T3",
                "managed_process": True,
                "runner_kill": True,
            },
            "heartbeat_ttl_s": 60,
            "metadata": {
                "wake_id": wake_id,
                "connect_assignment": True,
                "assignment_schema": "switchboard.connect.assignment.v1",
                "assignment_id": assignment_id,
                "execution_id": execution_id,
                "execution_generation": 1,
                "execution_role": "implementation",
                "execution_head_sha": HEAD,
                "lease_epoch": 1,
                **metadata,
            },
        }, principal_id=HOST_PRINCIPAL, actor=HOST, project=TARGET)

    initial = register("")
    assert not initial.get("error"), initial
    claim = store.claim_task(
        task_id,
        agent_id,
        principal_id=f"direct-session/{runner_id}",
        work_session_id=work_session["work_session_id"],
        require_work_session=True,
        session_policy_profile="code_strict",
        actor="bug337-test",
        project=TARGET,
    )
    assert claim.get("claimed") is True, claim
    bound = register(claim["claim_id"])
    assert not bound.get("error"), bound
    kill_request = None
    if suffix == "kill":
        kill_request = store.request_runner_control(
            runner_id, "kill", reason="verified completion-race kill",
            actor="bug337-test", project=TARGET,
        )
        assert kill_request.get("requested") is True, kill_request

    completion = store.complete_claim(
        claim["claim_id"],
        evidence={
            "branch": branch,
            "head_sha": HEAD,
            "pr_number": pr_number,
            "pr_url": f"https://github.com/example/projectplanner/pull/{pr_number}",
            "executed_test_run": {
                "schema": "switchboard.executed_test_run.v1",
                "run_id": f"bug337-{suffix}",
                "work_session_id": work_session["work_session_id"],
                "branch": branch,
                "head_sha": HEAD,
                "commands": [
                    "python3 tests/test_bug337_cross_project_completion_handoff.py",
                ],
                "exit_code": 0,
                "status": "success",
                "completed_at": time.time(),
                "output_hash": "sha256:" + "4" * 64,
            },
            "git_diff_check": "clean",
        },
        final_status="In Review",
        actor="bug337-test",
        project=TARGET,
    )
    assert completion.get("stopping") is True, completion
    return {
        "task": task,
        "task_id": task_id,
        "claim_id": claim["claim_id"],
        "work_session_id": work_session["work_session_id"],
        "runner_id": runner_id,
        "execution_id": execution_id,
        "pr_number": pr_number,
        "register": register,
        "kill_request": kill_request,
    }


def claim_status(claim_id: str) -> str:
    with _conn(TARGET) as connection:
        return connection.execute(
            "SELECT status FROM task_claims WHERE id=?", (claim_id,),
        ).fetchone()["status"]


def work_session_status(work_session_id: str) -> str:
    with _conn(TARGET) as connection:
        return connection.execute(
            "SELECT status FROM work_sessions WHERE work_session_id=?",
            (work_session_id,),
        ).fetchone()["status"]


def execution_lease_state(execution_id: str) -> tuple[str, object]:
    with _conn(TARGET) as connection:
        row = connection.execute(
            "SELECT lease_state,released_at FROM resource_leases WHERE id=?",
            (execution_id,),
        ).fetchone()
    return row["lease_state"], row["released_at"]


def orphan_activity_count(task_id: str) -> int:
    with _conn(TARGET) as connection:
        return connection.execute(
            "SELECT COUNT(*) AS n FROM activity WHERE task_id=? AND kind IN ("
            "'task.claim.released_by_terminal_runner',"
            "'task.claim.released_by_runner_lease_expiry',"
            "'work_session.archived_by_runner_lease_expiry')",
            (task_id,),
        ).fetchone()["n"]


def pending_activity_count(task_id: str) -> int:
    with _conn(TARGET) as connection:
        return connection.execute(
            "SELECT COUNT(*) AS n FROM activity WHERE task_id=? "
            "AND kind='task.completion_handoff_ack_pending'",
            (task_id,),
        ).fetchone()["n"]


try:
    (TMP / "projects").mkdir()
    store.init_db(TARGET)
    created = store.create_project(
        "Atlas",
        project_id=SOURCE,
        actor="bug337-test",
        purpose="BUG-337 source Host authority",
        boundary="BUG-337 source-only Host enrollment",
    )
    assert created.get("created") is True, created
    store.init_db(SOURCE)
    for project in (SOURCE, TARGET):
        store.set_project_access(
            project,
            "org-6th-element-labs",
            owner_user_id="user/bug337",
            created_by="bug337-test",
        )
    store.set_project_repo_topology(
        project=SOURCE,
        canonical_repo="6th-Element-Labs/ActionEngine",
        canonical_default_branch="main",
    )
    store.set_project_repo_topology(
        project=TARGET,
        canonical_repo="6th-Element-Labs/projectplanner",
        canonical_default_branch="master",
    )

    now = time.time()
    fingerprint = "sha256:" + "5" * 64
    with _conn(SOURCE) as connection:
        connection.execute(
            "INSERT INTO principals("
            "id,kind,display_name,project,scopes,token_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                HOST_PRINCIPAL,
                "host",
                HOST,
                SOURCE,
                json.dumps(["read", "write:agent_host"]),
                auth.token_hash(HOST_TOKEN),
                now,
            ),
        )
        connection.execute(
            "INSERT INTO agent_host_enrollments("
            "enrollment_id,project_id,requested_host_id,host_id,owner_user_id,"
            "tenant_allowlist_json,project_allowlist_json,provider_allowlist_json,"
            "execution_policy_json,bootstrap_hash,bootstrap_expires_at,"
            "bootstrap_consumed_at,principal_id,public_key_fingerprint,"
            "identity_generation,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "hostenroll-bug337",
                SOURCE,
                HOST,
                HOST,
                "user/bug337",
                json.dumps(["org-6th-element-labs"]),
                json.dumps([SOURCE, TARGET]),
                json.dumps(["openai-codex"]),
                json.dumps(PERSONAL_EXECUTION_POLICY),
                "bug337-bootstrap",
                now + 3600,
                now,
                HOST_PRINCIPAL,
                fingerprint,
                1,
                "active",
                now,
                now,
            ),
        )

    source_registration = store.register_host(
        inventory(), principal_id=HOST_PRINCIPAL, actor=HOST, project=SOURCE)
    assert source_registration.get("host_id") == HOST, source_registration
    grant = store.create_agent_host_project_grant(
        source_project=SOURCE,
        host_id=HOST,
        target_project=TARGET,
        canonical_repository="6th-Element-Labs/projectplanner",
        runtime="codex",
        provider="openai-codex",
        trust_zone="org_shared",
        isolation_mode="worktree",
        max_concurrency=4,
        actor="user/bug337",
    )
    assert grant.get("granted") is True, grant
    target_registration = store.register_host(
        inventory(), principal_id=HOST_PRINCIPAL, actor=HOST, project=TARGET)
    assert target_registration.get("host_id") == HOST, target_registration

    with _conn(TARGET) as connection:
        duplicate = connection.execute(
            "SELECT id FROM principals WHERE id=?", (HOST_PRINCIPAL,),
        ).fetchone()
        duplicate_enrollment = connection.execute(
            "SELECT enrollment_id FROM agent_host_enrollments WHERE principal_id=?",
            (HOST_PRINCIPAL,),
        ).fetchone()
    assert duplicate is None
    assert duplicate_enrollment is None

    valid = create_completion("valid", 337)
    terminal = valid["register"](
        valid["claim_id"],
        status="expired",
        terminalized_by="runner_lease_expiry",
        lease_epoch=2,
    )
    assert not terminal.get("error"), terminal
    completed_task = store.get_task(valid["task_id"], project=TARGET)
    assert completed_task["status"] == "In Review", completed_task
    assert completed_task["git_state"]["pr_number"] == 337, completed_task
    assert completed_task["git_state"]["head_sha"] == HEAD, completed_task
    assert claim_status(valid["claim_id"]) == "completed"
    assert work_session_status(valid["work_session_id"]) == "completed"
    assert orphan_activity_count(valid["task_id"]) == 0

    pending = create_completion("pending", 338)
    with _conn(TARGET) as connection:
        connection.execute(
            "UPDATE resource_leases SET lease_state='active' WHERE id=?",
            (pending["execution_id"],),
        )
    waiting = pending["register"](
        pending["claim_id"],
        status="expired",
        terminalized_by="runner_lease_expiry",
        lease_epoch=2,
    )
    assert waiting.get("completion_handoff_pending") is True, waiting
    assert waiting.get("completion_handoff", {}).get(
        "error_code") == "terminal_ack_execution_lease_invalid", waiting
    assert store.get_task(pending["task_id"], project=TARGET)[
        "status"] == "In Progress"
    assert claim_status(pending["claim_id"]) == "active"
    assert work_session_status(pending["work_session_id"]) == "active"
    assert execution_lease_state(pending["execution_id"]) == ("active", None)
    assert orphan_activity_count(pending["task_id"]) == 0
    assert pending_activity_count(pending["task_id"]) == 1

    with _conn(TARGET) as connection:
        connection.execute(
            "UPDATE resource_leases SET lease_state='stopping',released_at=NULL "
            "WHERE id=?",
            (pending["execution_id"],),
        )
    replay = pending["register"](
        pending["claim_id"],
        status="expired",
        terminalized_by="runner_lease_expiry",
        lease_epoch=2,
    )
    assert not replay.get("completion_handoff_pending"), replay
    assert store.get_task(pending["task_id"], project=TARGET)[
        "status"] == "In Review"
    assert claim_status(pending["claim_id"]) == "completed"
    assert work_session_status(pending["work_session_id"]) == "completed"
    assert orphan_activity_count(pending["task_id"]) == 0
    assert pending_activity_count(pending["task_id"]) == 1

    killed = create_completion("kill", 339)
    killed_receipt = store.complete_runner_control_request(
        killed["kill_request"]["request_id"],
        result={"status": "killed", "alive": False},
        status="completed", actor=HOST, project=TARGET,
    )
    assert killed_receipt.get("status") == "completed", killed_receipt
    assert claim_status(killed["claim_id"]) == "active"
    assert work_session_status(killed["work_session_id"]) == "active"
    assert execution_lease_state(killed["execution_id"]) == ("stopping", None)
    assert orphan_activity_count(killed["task_id"]) == 0
    pending_runners = store.list_runner_sessions(
        host_id=HOST, include_stale=True, pending_completion=True,
        project=TARGET,
    )
    assert killed["runner_id"] in {
        row["runner_session_id"] for row in pending_runners
    }, pending_runners
    kill_terminal = killed["register"](
        killed["claim_id"], status="killed",
        terminalized_by="host_supervisor", lease_epoch=2,
    )
    assert not kill_terminal.get("completion_handoff_pending"), kill_terminal
    assert store.get_task(killed["task_id"], project=TARGET)[
        "status"] == "In Review"
    assert claim_status(killed["claim_id"]) == "completed"
    assert work_session_status(killed["work_session_id"]) == "completed"
    assert orphan_activity_count(killed["task_id"]) == 0

    print("BUG-337 cross-project completion handoff: PASS")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
