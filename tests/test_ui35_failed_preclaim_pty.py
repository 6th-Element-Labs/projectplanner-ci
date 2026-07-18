#!/usr/bin/env python3
"""UI-35: failed-preclaim completion and bounded PTY readiness proof."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT

TMP = Path(tempfile.mkdtemp(prefix="ui35-failed-preclaim-pty-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from adapters.codex import supervisor  # noqa: E402
from db.connection import _conn  # noqa: E402

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
        {"workstream_id": "UI", "title": "UI-35 failed preclaim proof"},
        actor="ui35-test", project=P)
    task_id = task["task_id"]
    host_id = "host/ui35-mac"
    principal_id = "principal/ui35-host"
    wake_id = "wake-ui35"
    runner_id = "run_ui35"
    agent_id = f"codex/{task_id}"
    now = time.time()
    with _conn(P) as c:
        c.execute(
            "INSERT INTO wake_intents(wake_id,source,reason,selector_json,policy_json,"
            "status,requested_at,claimed_at,claimed_by_host,result_json,placement_json,task_id) "
            "VALUES (?,?,?,?,?,'claimed',?,?,?,'{}','{}',?)",
            (wake_id, "ui35-test", "autopilot", json.dumps({
                "runtime": "codex", "agent_id": agent_id, "task_id": task_id,
            }), json.dumps({"require_runner_bind": True, "mode": "co_fleet"}),
             now, now, host_id, task_id),
        )
        c.execute(
            "INSERT INTO runner_sessions(runner_session_id,host_id,agent_id,runtime,task_id,"
            "claim_id,status,control_json,metadata_json,last_snapshot_json,principal_id,"
            "started_at,heartbeat_at,heartbeat_ttl_s,updated_at) "
            "VALUES (?,?,?,?,?,NULL,'failed','{}',?,'{}',?,?,?,?,?)",
            (runner_id, host_id, agent_id, "codex", task_id, json.dumps({
                "credential_admission_phase": "preclaim_failed",
                "wake_id": wake_id,
                "failure_reason": "pty_stream_not_ready",
            }), principal_id, now, now, 60, now),
        )

    binding = {"wake_id": wake_id, "host_id": host_id,
               "runner_session_id": runner_id, "task_id": task_id,
               "agent_id": agent_id}
    exact = store.check_agent_host_bootstrap_authority(
        binding, principal_id=principal_id, project=P, action="complete_wake")
    ok(exact.get("allowed") is True,
       "exact unbound failed-preclaim runner may terminalize its wake")

    for field, wrong in (("host_id", "host/other"),
                         ("runner_session_id", "run_other"),
                         ("task_id", "UI-9999"),
                         ("agent_id", "codex/UI-9999"),
                         ("wake_id", "wake-other")):
        denied = store.check_agent_host_bootstrap_authority(
            {**binding, field: wrong}, principal_id=principal_id,
            project=P, action="complete_wake")
        ok(denied.get("allowed") is False,
           f"failed-preclaim cross-{field} completion remains denied")

    with _conn(P) as c:
        c.execute("UPDATE runner_sessions SET status='running' WHERE runner_session_id=?",
                  (runner_id,))
    nonterminal = store.check_agent_host_bootstrap_authority(
        binding, principal_id=principal_id, project=P, action="complete_wake")
    ok(nonterminal.get("allowed") is False
       and "runner_completion_status_invalid" in nonterminal.get("reason_codes", []),
       "failed-preclaim phase cannot terminalize a nonterminal runner")

    old_timeout = os.environ.get("PM_RUNNER_STREAM_READY_TIMEOUT_SECONDS")
    os.environ["PM_RUNNER_STREAM_READY_TIMEOUT_SECONDS"] = "1.25"
    start = time.time()
    ready = supervisor._await_stream_ready(TMP / "missing-ready.json")
    elapsed = time.time() - start
    if old_timeout is None:
        os.environ.pop("PM_RUNNER_STREAM_READY_TIMEOUT_SECONDS", None)
    else:
        os.environ["PM_RUNNER_STREAM_READY_TIMEOUT_SECONDS"] = old_timeout
    ok(ready == {} and 1.0 <= elapsed < 2.5,
       "PTY readiness uses the bounded configurable timeout")

    source = (ROOT / "adapters/codex/supervisor.py").read_text(encoding="utf-8")
    ok('PM_RUNNER_STREAM_READY_TIMEOUT_SECONDS", "15"' in source,
       "production PTY companion gets a 15-second transient startup window")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nUI-35 failed-preclaim + PTY proof: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
