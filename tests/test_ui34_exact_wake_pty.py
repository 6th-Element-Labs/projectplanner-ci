#!/usr/bin/env python3
"""UI-34: deterministic PTY startup and exact generic-wake completion proof."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from path_setup import ROOT

TMP = Path(tempfile.mkdtemp(prefix="ui34-exact-wake-pty-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from adapters import agent_host  # noqa: E402
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
        {"workstream_id": "UI", "title": "UI-34 exact wake proof"},
        actor="ui34-test", project=P)
    task_id = task["task_id"]
    host_id = "host/ui34-mac"
    principal_id = "principal/ui34-host"
    wake_id = "wake-ui34"
    runner_id = "run_ui34"
    agent_id = f"codex/{task_id}"
    now = time.time()
    selector = {"runtime": "codex", "agent_id": agent_id, "task_id": task_id}
    policy = {"require_runner_bind": True, "mode": "co_fleet"}
    with _conn(P) as c:
        c.execute(
            "INSERT INTO wake_intents(wake_id,source,reason,selector_json,policy_json,"
            "status,requested_at,claimed_at,claimed_by_host,result_json,placement_json,task_id) "
            "VALUES (?,?,?,?,?,'claimed',?,?,?,'{}','{}',?)",
            (wake_id, "ui34-test", "autopilot", json.dumps(selector),
             json.dumps(policy), now, now, host_id, task_id),
        )
        c.execute(
            "INSERT INTO runner_sessions(runner_session_id,host_id,agent_id,runtime,task_id,"
            "claim_id,status,control_json,metadata_json,last_snapshot_json,principal_id,"
            "started_at,heartbeat_at,heartbeat_ttl_s,updated_at) "
            "VALUES (?,?,?,?,?,NULL,'failed','{}',?,'{}',?,?,?,?,?)",
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
        binding, principal_id=principal_id, project=P, action="complete_wake")
    ok(allowed.get("allowed") is True,
       "exact host may terminalize its failed preclaim wake")

    for field, wrong in (
        ("host_id", "host/other"),
        ("runner_session_id", "run_other"),
        ("task_id", "UI-9999"),
        ("agent_id", "codex/UI-9999"),
        ("wake_id", "wake-other"),
    ):
        denied = store.check_agent_host_bootstrap_authority(
            {**binding, field: wrong}, principal_id=principal_id,
            project=P, action="complete_wake")
        ok(denied.get("allowed") is False,
           f"cross-{field} generic wake completion is denied")

    with _conn(P) as c:
        c.execute(
            "UPDATE runner_sessions SET claim_id=?,status='completed',metadata_json=? "
            "WHERE runner_session_id=?",
            ("claim-ui34", json.dumps({
                "credential_admission_phase": "claim_bound",
                "wake_id": wake_id,
                "work_session_id": "worksession-ui34",
            }), runner_id),
        )
    bound_allowed = store.check_agent_host_bootstrap_authority(
        binding, principal_id=principal_id, project=P, action="complete_wake")
    ok(bound_allowed.get("allowed") is True,
       "exact claim-bound runner may complete the same wake")

    with _conn(P) as c:
        c.execute(
            "UPDATE runner_sessions SET metadata_json=? WHERE runner_session_id=?",
            (json.dumps({
                "credential_admission_phase": "claim_bound", "wake_id": wake_id,
            }), runner_id),
        )
    partial = store.check_agent_host_bootstrap_authority(
        binding, principal_id=principal_id, project=P, action="complete_wake")
    ok(partial.get("allowed") is False
       and "runner_completion_binding_partial" in partial.get("reason_codes", []),
       "partial claim-without-Work-Session completion is denied")

    with _conn(P) as c:
        c.execute(
            "UPDATE runner_sessions SET claim_id=NULL,status='running',metadata_json=? "
            "WHERE runner_session_id=?",
            (json.dumps({
                "credential_admission_phase": "claim_bound", "wake_id": wake_id,
            }), runner_id),
        )
    wrong_phase = store.check_agent_host_bootstrap_authority(
        binding, principal_id=principal_id, project=P, action="complete_wake")
    ok(wrong_phase.get("allowed") is False
       and "runner_completion_phase_invalid" in wrong_phase.get("reason_codes", [])
       and "runner_completion_status_invalid" in wrong_phase.get("reason_codes", []),
       "unbound nonterminal runner cannot complete through a claim-bound phase")

    task_wake = {
        "task_id": task_id,
        "selector": {"runtime": "codex", "agent_id": agent_id, "lane": "UI"},
        "policy": {"require_runner_bind": True},
    }
    inventory = {
        "host_id": host_id,
        "repo_root": str(ROOT),
        "policy": {"allow_work": True, "allow_global_claim": False},
        "runtimes": [{"runtime": "codex", "lanes": ["UI"]}],
    }
    old_module = os.environ.get("PM_AGENT_WORK_MODULE_CODEX")
    os.environ["PM_AGENT_WORK_MODULE_CODEX"] = "adapters.codex_local_worker:run"
    try:
        command, mode = agent_host.launch_command(task_wake, inventory, "run-ui34")
    finally:
        if old_module is None:
            os.environ.pop("PM_AGENT_WORK_MODULE_CODEX", None)
        else:
            os.environ["PM_AGENT_WORK_MODULE_CODEX"] = old_module
    ok(mode == "claim_next" and "--auto-work-session" in command,
       "task-bound wake explicitly selects exact auto-Work-Session routing")

    agent_source = (ROOT / "adapters/agent_host.py").read_text(encoding="utf-8")
    wakes_source = (ROOT / "src/switchboard/api/routers/wakes.py").read_text(
        encoding="utf-8")
    ok('"PM_RUNTIME": str((claimed_wake.get("selector")' in agent_source,
       "Agent Host passes the selected runtime into exact Work Session creation")
    ok('"complete_wake"' in wakes_source
       and "require_agent_host_bootstrap_authority" in wakes_source,
       "narrow generic wake completion uses the durable exact-binding gate")

    fast_root = TMP / "fast-runner"
    receipt = supervisor.start_session(
        [sys.executable, "-c", "print('ui34 fast child')"],
        agent_id="codex/ui34-fast", task_id=task_id,
        runner_dir=str(fast_root), runner_session_id="run_ui34_fast")
    time.sleep(0.2)
    fast_status = supervisor.status_session(
        "run_ui34_fast", runner_dir=str(fast_root))
    ok(receipt.get("pty") is True and "stream_port" not in receipt
       and fast_status.get("status") == "exited"
       and Path(fast_status["log_path"]).read_text().strip() == "ui34 fast child",
       "PTY is ready before an immediate child exit and preserves its output")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nUI-34 exact wake + PTY proof: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
