#!/usr/bin/env python3
"""BUG-128: a Connect host may complete only its exact claimed native wake."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="bug128-connect-completion-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "required"

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import store  # noqa: E402
from app import app  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.application.commands import connect_dispatch  # noqa: E402


P = "switchboard"
HOST = "host/bug128-native"
PRINCIPAL = "principal/bug128-native"
TOKEN = "bug128-narrow-host-token"
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
        "title": "BUG-128 Connect completion",
    }, actor="bug128-test", project=P)
    task_id = task["task_id"]
    now = time.time()
    store.register_host({
        "host_id": HOST,
        "hostname": "bug128-native",
        "agent_host_version": "0.3.885",
        "repo_root": str(ROOT),
        "runtimes": [{
            "runtime": "codex", "provider": "openai", "lanes": [],
            "capabilities": [
                "tests", "execution_lease_v2", "runner_lease_enforcement"],
            "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
        }],
        "limits": {"max_sessions": 2},
        "capacity": {"active_sessions": 0, "allow_work": True},
        "heartbeat_ttl_s": 60,
    }, principal_id=PRINCIPAL, actor=HOST, project=P)
    with _conn(P) as connection:
        connection.execute(
            "INSERT INTO principals(id,kind,display_name,project,scopes,token_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (PRINCIPAL, "host", HOST, P,
             json.dumps(["read", "write:agent_host"]), auth.token_hash(TOKEN), now),
        )
        connection.execute(
            "INSERT INTO agent_host_enrollments("
            "enrollment_id,project_id,requested_host_id,host_id,owner_user_id,"
            "tenant_allowlist_json,project_allowlist_json,provider_allowlist_json,"
            "bootstrap_hash,bootstrap_expires_at,bootstrap_consumed_at,principal_id,"
            "public_key_fingerprint,identity_generation,package_version,platform,"
            "hostname,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("hostenroll-bug128", P, HOST, HOST, "user/bug128", "[]",
             json.dumps([P]), json.dumps(["openai-codex"]), "bug128-bootstrap",
             now + 3600, now, PRINCIPAL, "sha256:" + "b" * 64, 1,
             "0.3.885", "macos", "bug128-native", "active", now, now),
        )

    dispatched = connect_dispatch.enqueue_task(
        task, project=P, actor="bug128-test", runtime="codex")
    wake = next(row for row in store.list_wake_intents(project=P)
                if row.get("wake_id") == dispatched.get("wake_id"))
    runner_id = "run_bug128_connect_native"
    claimed = store.claim_wake(
        HOST, wake["wake_id"], runner_session_id=runner_id,
        principal_id=PRINCIPAL, actor=HOST, project=P)
    assignment = (claimed.get("wake") or wake).get("policy", {}).get("assignment", {})
    registered = store.upsert_runner_session({
        "runner_session_id": runner_id,
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
            "assignment_schema": "switchboard.connect.assignment.v1",
            "assignment_id": assignment.get("assignment_id"),
        },
    }, principal_id=PRINCIPAL, actor=HOST, project=P)
    ok((registered.get("metadata") or {}).get("native_host_execution") is True,
       "Connect runner has server-derived native execution authority")

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    forged = client.post("/txp/v1/complete_wake", headers=headers, json={
        "project": P, "wake_id": wake["wake_id"],
        "runner_session_id": "run_forged", "agent_id": registered["agent_id"],
        "result": {"started": True},
    })
    ok(forged.status_code == 403,
       "narrow host cannot complete Connect through an unregistered runner")

    completed = client.post("/txp/v1/complete_wake", headers=headers, json={
        "project": P, "wake_id": wake["wake_id"],
        "runner_session_id": runner_id, "agent_id": registered["agent_id"],
        "result": {"started": True, "reason": "native_codex_execution_completed"},
    })
    completed_body = completed.json()
    ok(completed.status_code == 200 and completed_body.get("status") == "completed",
       "narrow enrolled host completes its exact claimed Connect wake over HTTP")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nBUG-128 Connect completion: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
