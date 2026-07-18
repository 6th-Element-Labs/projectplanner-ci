#!/usr/bin/env python3
"""UI-36: claim-bound wake completion and PTY Watch publication proof."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from path_setup import ROOT

TMP = Path(tempfile.mkdtemp(prefix="ui36-runner-watch-bind-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import json  # noqa: E402
import shutil  # noqa: E402
import time  # noqa: E402

import store  # noqa: E402
from adapters import agent_host  # noqa: E402
from db.connection import _conn  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


wake = {
    "wake_id": "wake-ui36",
    "task_id": "UI-36",
    "selector": {"agent_id": "codex/UI-36", "runtime": "codex"},
}
inventory = {"host_id": "host/ui36"}
partial = {
    "runner_session_id": "run_ui36",
    "task_id": "UI-36",
    "host_id": "host/ui36",
    "agent_id": "codex/UI-36",
    "runtime": "codex",
    "claim_id": "taskclaim-ui36",
    "status": "running",
    "stale": False,
    "metadata": {
        "wake_id": "wake-ui36",
        "work_session_id": "worksession-ui36",
        "credential_admission_phase": "preclaim",
    },
}
bound = {
    **partial,
    "cwd": "/worker/task-ui36",
    "metadata": {
        **partial["metadata"],
        "credential_admission_phase": "claim_bound",
    },
}

real_try = agent_host._try
responses = iter((partial, bound))


def fake_try(method, path, body=None, timeout=None):
    del method, path, body, timeout
    try:
        row = next(responses)
    except StopIteration:
        row = bound
    return {"sessions": [row]}


agent_host._try = fake_try
try:
    result = agent_host.wait_for_runner_binding(
        wake, inventory, "run_ui36", timeout_s=0.2,
        sleep=lambda _seconds: None)
finally:
    agent_host._try = real_try

ok(result.get("bound") is True and result.get("session") == bound,
   "binding wait ignores a preclaim-phase partial tuple and waits for claim_bound")

local = {
    "runner_session_id": "run_ui36",
    "task_id": "UI-36",
    "agent_id": "codex/UI-36",
    "runtime": "codex",
    "status": "running",
    "cwd": str(ROOT),
    "pty": True,
    "stream_bind": "127.0.0.1",
    "stream_port": 64536,
    "log_path": "/tmp/ui36.log",
    "command": ["codex", "exec"],
    "control": {"runner_open": True, "runner_inject": True},
    "metadata": {"credential_admission_phase": "preclaim"},
}
enriched = agent_host._enrich_bound_runner_record(local, bound)
ok(enriched.get("claim_id") == "taskclaim-ui36"
   and enriched.get("cwd") == "/worker/task-ui36"
   and enriched.get("metadata", {}).get("credential_admission_phase") == "claim_bound"
   and enriched.get("stream_port") == 64536
   and enriched.get("pty") is True,
   "enrichment preserves worker authority and adds supervisor PTY transport")

store.init_db("switchboard")
created = store.create_task(
    {"workstream_id": "UI", "title": "UI-36 running completion proof"},
    actor="ui36-test", project="switchboard")
server_task_id = created["task_id"]
server_agent_id = f"codex/{server_task_id}"
now = time.time()
with _conn("switchboard") as c:
    c.execute(
        "INSERT INTO wake_intents(wake_id,source,reason,selector_json,policy_json,"
        "status,requested_at,claimed_at,claimed_by_host,result_json,placement_json,task_id) "
        "VALUES (?,?,?,?,?,'claimed',?,?,?,'{}','{}',?)",
        ("wake-ui36-server", "ui36-test", "autopilot", json.dumps({
            "runtime": "codex", "agent_id": server_agent_id,
            "task_id": server_task_id,
        }), json.dumps({"require_runner_bind": True}), now, now,
         "host/ui36", server_task_id),
    )
    c.execute(
        "INSERT INTO runner_sessions(runner_session_id,host_id,agent_id,runtime,task_id,"
        "claim_id,status,control_json,metadata_json,last_snapshot_json,principal_id,"
        "started_at,heartbeat_at,heartbeat_ttl_s,updated_at) "
        "VALUES (?,?,?,?,?,?,'running','{}',?,'{}',?,?,?,?,?)",
        ("run_ui36_server", "host/ui36", server_agent_id, "codex",
         server_task_id, "claim-ui36", json.dumps({
             "credential_admission_phase": "claim_bound",
             "wake_id": "wake-ui36-server",
             "work_session_id": "worksession-ui36",
         }), "principal/ui36", now, now, 180, now),
    )
allowed = store.check_agent_host_bootstrap_authority({
    "wake_id": "wake-ui36-server", "host_id": "host/ui36",
    "runner_session_id": "run_ui36_server", "task_id": server_task_id,
    "agent_id": server_agent_id,
}, principal_id="principal/ui36", project="switchboard",
   action="complete_wake")
ok(allowed.get("allowed") is True,
   "claim-bound running runner may acknowledge successful wake startup")

redeploy = (ROOT / "deploy/redeploy.sh").read_text(encoding="utf-8")
inventory_source = (ROOT / "deploy/service-cut-inventory.json").read_text(
    encoding="utf-8")
ok('APP_SERVICES=(projectplanner-gateway projectplanner projectplanner-mcp "${CUT_SERVICES[@]}")'
   in redeploy and all(name in inventory_source for name in (
       "switchboard-auth", "switchboard-tasks", "switchboard-coord",
       "switchboard-deliverables")),
   "production redeploy restarts every segmented edge-owning service")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nUI-36 runner Watch bind proof: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
