#!/usr/bin/env python3
"""UI-33: narrow Agent Host exact-wake bootstrap and truthful fast-exit proof."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from path_setup import ROOT

TMP = Path(tempfile.mkdtemp(prefix="ui33-agent-host-bootstrap-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from adapters import switchboard_core as sb  # noqa: E402
from adapters.codex import supervisor  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    store.init_db(P)
    task = store.create_task(
        {"workstream_id": "UI", "title": "UI-33 exact bootstrap proof"},
        actor="ui33-test", project=P)
    task_id = task["task_id"]
    host_id = "host/ui33-mac"
    principal_id = "principal/ui33-host"
    wake_id = "wake-ui33"
    runner_id = "run_ui33"
    agent_id = f"codex/{task_id}"
    now = time.time()
    selector = {"runtime": "codex", "agent_id": agent_id, "task_id": task_id}
    policy = {"require_runner_bind": True, "mode": "co_fleet"}
    with _conn(P) as c:
        c.execute(
            "INSERT INTO wake_intents(wake_id,source,reason,selector_json,policy_json,"
            "status,requested_at,claimed_at,claimed_by_host,result_json,placement_json,task_id) "
            "VALUES (?,?,?,?,?,'claimed',?,?,?,'{}','{}',?)",
            (wake_id, "ui33-test", "autopilot", json.dumps(selector),
             json.dumps(policy), now, now, host_id, task_id),
        )
        c.execute(
            "INSERT INTO runner_sessions(runner_session_id,host_id,agent_id,runtime,task_id,"
            "claim_id,status,control_json,metadata_json,last_snapshot_json,principal_id,"
            "started_at,heartbeat_at,heartbeat_ttl_s,updated_at) "
            "VALUES (?,?,?,?,?,NULL,'starting','{}',?,'{}',?,?,?,?,?)",
            (runner_id, host_id, agent_id, "codex", task_id,
             json.dumps({"credential_admission_phase": "preclaim", "wake_id": wake_id}),
             principal_id, now, now, 60, now),
        )

    binding = {
        "wake_id": wake_id,
        "host_id": host_id,
        "runner_session_id": runner_id,
        "task_id": task_id,
        "agent_id": agent_id,
    }
    allowed = store.check_agent_host_bootstrap_authority(
        binding, principal_id=principal_id, project=P,
        action="create_work_session")
    ok(allowed.get("allowed") is True,
       "exact claimed wake + owned preclaim runner authorizes Work Session bootstrap")

    for field, wrong in (
        ("host_id", "host/other"),
        ("runner_session_id", "run_other"),
        ("task_id", "UI-9999"),
        ("agent_id", "codex/UI-9999"),
        ("wake_id", "wake-other"),
    ):
        denied = store.check_agent_host_bootstrap_authority(
            {**binding, field: wrong}, principal_id=principal_id, project=P,
            action="create_work_session")
        ok(denied.get("allowed") is False
           and denied.get("error_code") == "agent_host_bootstrap_binding_denied",
           f"cross-{field} bootstrap is denied")

    wrong_principal = store.check_agent_host_bootstrap_authority(
        binding, principal_id="principal/other", project=P,
        action="create_work_session")
    ok(wrong_principal.get("allowed") is False
       and "runner_principal_id_mismatch" in wrong_principal.get("reason_codes", []),
       "another host principal cannot use the preclaim runner")

    created = store.create_work_session({
        "task_id": task_id,
        "agent_id": agent_id,
        "runtime": "codex",
        "repo_role": "canonical",
        "branch": f"codex/{task_id}-autopilot-test",
        "upstream": "origin/master",
        "base_sha": "a" * 40,
        "head_sha": "a" * 40,
        "worktree_path": str(ROOT),
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": "clean",
        "conflict_marker_count": 0,
        "policy_profile": "code_strict",
    }, actor=host_id, principal_id=principal_id, project=P)
    work_session_id = (created.get("work_session") or {})["work_session_id"]
    claim_allowed = store.check_agent_host_bootstrap_authority(
        binding, principal_id=principal_id, project=P,
        work_session_id=work_session_id, action="claim_task")
    ok(claim_allowed.get("allowed") is True,
       "same host may claim only through its exact newly-created Work Session")

    with _conn(P) as c:
        c.execute("UPDATE wake_intents SET status='failed' WHERE wake_id=?", (wake_id,))
        c.execute("UPDATE runner_sessions SET status='failed' WHERE runner_session_id=?",
                  (runner_id,))
    cleanup_allowed = store.check_agent_host_bootstrap_authority(
        binding, principal_id=principal_id, project=P,
        work_session_id=work_session_id, action="expire_work_session")
    ok(cleanup_allowed.get("allowed") is True,
       "terminal wake race still permits cleanup of only the exact host-owned Work Session")
    with _conn(P) as c:
        c.execute("UPDATE wake_intents SET status='claimed' WHERE wake_id=?", (wake_id,))
        c.execute("UPDATE runner_sessions SET status='starting' WHERE runner_session_id=?",
                  (runner_id,))

    other_session = store.create_work_session({
        "task_id": task_id,
        "agent_id": agent_id,
        "repo_role": "canonical",
        "branch": f"codex/{task_id}-other",
        "storage_mode": "external",
        "status": "active",
        "dirty_status": "clean",
        "policy_profile": "docs_review",
    }, actor="other", principal_id="principal/other", project=P)
    other_session_id = (other_session.get("work_session") or {})["work_session_id"]
    session_denied = store.check_agent_host_bootstrap_authority(
        binding, principal_id=principal_id, project=P,
        work_session_id=other_session_id, action="claim_task")
    ok(session_denied.get("allowed") is False
       and "work_session_principal_id_mismatch" in session_denied.get("reason_codes", []),
       "host cannot claim through another principal's Work Session")

    old_env = {key: os.environ.get(key) for key in (
        "PM_CO_WAKE_ID", "PM_CO_HOST_ID", "PM_RUNNER_SESSION_ID",
        "PM_TASK_ID", "PM_AGENT_ID",
    )}
    os.environ.update({
        "PM_CO_WAKE_ID": wake_id,
        "PM_CO_HOST_ID": host_id,
        "PM_RUNNER_SESSION_ID": runner_id,
        "PM_TASK_ID": task_id,
        "PM_AGENT_ID": agent_id,
    })
    calls = []
    real_http = sb._http
    try:
        sb._http = lambda method, path, body=None, **kwargs: (
            calls.append((method, path, body)) or {"claimed": True})
        sb.claim_task(P, task_id, agent_id, work_session_id=work_session_id)
        sb.expire_external_work_session(P, work_session_id, agent_id)
    finally:
        sb._http = real_http
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    ok(all((body or {}).get("agent_host_bootstrap_binding") == binding
           for _method, _path, body in calls),
       "worker sends the exact bootstrap tuple on claim and cleanup mutations")

    runner_dir = TMP / "runner"
    receipt = supervisor.start_session(
        [sys.executable, "-c", "print('fast exit evidence')"],
        agent_id="codex/fast-exit", task_id="UI-FAST",
        runner_dir=str(runner_dir), runner_session_id="run_fast_exit")
    time.sleep(0.2)
    status = supervisor.status_session("run_fast_exit", runner_dir=str(runner_dir))
    ok(receipt.get("stream_port") and status.get("status") == "exited"
       and Path(status["log_path"]).read_text().strip() == "fast exit evidence",
       "fast child exit preserves a truthful supervisor receipt and PTY log")

    claims_source = (ROOT / "src/switchboard/api/routers/claims.py").read_text()
    sessions_source = (ROOT / "src/switchboard/api/routers/ixp_work_sessions.py").read_text()
    ok("require_agent_host_bootstrap_authority" in claims_source
       and "require_agent_host_bootstrap_authority" in sessions_source,
       "REST claim and Work Session mutations enforce the narrow bootstrap gate")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nUI-33 Agent Host bootstrap proof: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
