#!/usr/bin/env python3
"""BUG-127: Connect runners use the claimed native host, not a fake credential id."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="bug127-connect-registration-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
import auth  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.application.commands import connect_dispatch  # noqa: E402
from switchboard.domain.runner_pty import planned_runner_session_id  # noqa: E402


P = "switchboard"
HOST = "host/bug127-native"
PRINCIPAL = "principal/bug127-native"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    store.init_db(P)
    task = store.create_task({
        "workstream_id": "BUG",
        "title": "BUG-127 Connect native runner registration",
    }, actor="bug127-test", project=P)
    task_id = task["task_id"]
    inventory = {
        "host_id": HOST,
        "hostname": "bug127-native",
        "agent_host_version": "0.3.885",
        "repo_root": str(ROOT),
        "runtimes": [{
            "runtime": "codex", "provider": "openai", "lanes": [],
            "capabilities": ["tests", "execution_lease_v2", "runner_lease_enforcement"],
            "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
        }],
        "limits": {"max_sessions": 2},
        "capacity": {"active_sessions": 0, "allow_work": True},
        "heartbeat_ttl_s": 60,
    }
    store.register_host(
        inventory, principal_id=PRINCIPAL, actor=HOST, project=P)
    now = time.time()
    with _conn(P) as connection:
        connection.execute(
            "INSERT INTO principals(id,kind,display_name,project,scopes,token_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (PRINCIPAL, "host", HOST, P,
             json.dumps(["read", "write:agent_host"]), "bug127-token", now),
        )
        connection.execute(
            "INSERT INTO agent_host_enrollments("
            "enrollment_id,project_id,requested_host_id,host_id,owner_user_id,"
            "tenant_allowlist_json,project_allowlist_json,provider_allowlist_json,"
            "bootstrap_hash,bootstrap_expires_at,bootstrap_consumed_at,principal_id,"
            "public_key_fingerprint,identity_generation,package_version,platform,"
            "hostname,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("hostenroll-bug127", P, HOST, HOST, "user/bug127", "[]",
             json.dumps([P]), json.dumps(["openai-codex"]), "bug127-bootstrap",
             now + 3600, now, PRINCIPAL, "sha256:" + "b" * 64, 1,
             "0.3.885", "macos", "bug127-native", "active", now, now),
        )

    dispatched = connect_dispatch.enqueue_task(
        task, project=P, actor="bug127-test", runtime="codex")
    wake = next(row for row in store.list_wake_intents(project=P)
                if row.get("wake_id") == dispatched.get("wake_id"))
    run_id = planned_runner_session_id(wake["wake_id"], HOST)
    claimed = store.claim_wake(
        HOST, wake["wake_id"], runner_session_id=run_id,
        principal_id=PRINCIPAL, actor=HOST, project=P)
    assignment = (claimed.get("wake") or wake).get("policy", {}).get("assignment", {})
    issued = store.issue_direct_session_mcp_token(
        wake["wake_id"], HOST, run_id, principal_id=PRINCIPAL,
        actor=HOST, project=P)
    session_principal = auth.principal_for_token_any_project(
        issued.get("token") or "")
    ok(issued.get("issued") is True
       and (session_principal or {}).get("id") == f"direct-session/{run_id}"
       and (session_principal or {}).get("bound_task_id") == task_id,
       "claimed Connect wake mints an exact task-scoped session bearer")
    record = {
        "runner_session_id": run_id,
        "host_id": HOST,
        "agent_id": (wake.get("selector") or {}).get("agent_id"),
        "runtime": "codex",
        "task_id": task_id,
        "claim_id": "",
        "status": "running",
        "cwd": str(ROOT),
        "control": {"tier": "T3", "managed_process": True, "runner_kill": True},
        "metadata": {
            "wake_id": wake["wake_id"],
            "wake_mode": "connect",
            "connect_assignment": True,
            "assignment_schema": assignment.get("schema"),
            "assignment_id": assignment.get("assignment_id"),
        },
    }
    registered = store.upsert_runner_session(
        record, principal_id=PRINCIPAL, actor=HOST, project=P)
    ok(claimed.get("claimed") is True,
       "Connect wake is atomically claimed by the selected native host")
    ok(not registered.get("error")
       and (registered.get("metadata") or {}).get("native_host_execution") is True,
       "claimed Connect runner registers without an execution_connection_id")
    completion_authority = store.check_direct_task_completion_authority(
        {
            "wake_id": wake["wake_id"], "host_id": HOST,
            "runner_session_id": run_id, "task_id": task_id,
            "agent_id": (wake.get("selector") or {}).get("agent_id"),
        },
        principal_id=PRINCIPAL, project=P,
    )
    ok(completion_authority.get("allowed") is True,
       "the same narrow host may complete its claimed registered Connect wake")

    forged = store.upsert_runner_session(
        {**record, "runner_session_id": "run_bug127_forged",
         "metadata": {**record["metadata"], "assignment_id": "assignment-forged"}},
        principal_id=PRINCIPAL, actor=HOST, project=P)
    ok(forged.get("error_code") == "runner_execution_binding_mismatch",
       "a mismatched Connect assignment still fails closed")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nBUG-127 Connect registration: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
