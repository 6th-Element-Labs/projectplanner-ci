#!/usr/bin/env python3
"""Regression tests for HARDEN-24 runner environment health/log/control surface.

Run:
    python3 test_runner_environment.py
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="runner-environment-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "required"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

ROOT = Path(__file__).resolve().parent
P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


try:
    store.init_project_registry()
    store.init_db(P)
    now = time.time()
    task = store.create_task({"workstream_id": "HARDEN", "title": "runner env"},
                             actor="test", project=P)
    managed = store.upsert_runner_session({
        "runner_session_id": "run-env-managed",
        "host_id": "host/env",
        "agent_id": "codex/env",
        "runtime": "codex",
        "task_id": task["task_id"],
        "pid": 123,
        "status": "running",
        "cwd": str(ROOT),
        "started_at": now - 125,
        "heartbeat_at": now,
        "heartbeat_ttl_s": 60,
        "control": {"managed_process": True, "runner_kill": True},
        "metadata": {
            "command": ["python3", "worker.py"],
            "log_path": "/tmp/run-env-managed.log",
        },
        "last_snapshot": {"captured_at": now - 10, "log_tail": "line one\nline two"},
    }, actor="test", project=P)
    actions = set(managed["available_actions"])
    env = managed["environment"]
    ok({"health", "logs", "snapshot", "kill"}.issubset(actions),
       "managed runner advertises health/log/snapshot/kill actions")
    ok(env["capabilities"]["health"] == "supported" and
       env["capabilities"]["logs"] == "supported" and
       env["capabilities"]["open"] == "not_supported",
       "environment capabilities are explicit per action")
    ok(env["status"] == "running" and env["uptime_seconds"] >= 120,
       "environment reports health status and uptime")
    ok(env["last_command"] == ["python3", "worker.py"] and "line two" in env["log_tail"],
       "environment reports last command and log tail")

    unmanaged = store.upsert_runner_session({
        "runner_session_id": "run-env-unmanaged",
        "agent_id": "codex/unmanaged",
        "runtime": "codex",
        "status": "running",
        "control": {"runner_kill": True},
    }, actor="test", project=P)
    ok(unmanaged["environment"]["capabilities"]["health"] == "not_supported",
       "unmanaged runner reports unsupported health")
    refused = store.request_runner_control(
        "run-env-unmanaged", "health", actor="operator", project=P)
    ok(refused["requested"] is False and refused["status"] == "refused" and
       refused["result"]["reason"] == "not_supported",
       "unsupported health request is audited as a refused control request")
    requested = store.request_runner_control(
        "run-env-managed", "health", actor="operator", project=P)
    ok(requested["requested"] is True and requested["action"] == "health",
       "managed runner health request is queued")
    completed = store.complete_runner_control_request(
        requested["request_id"],
        result={"health": {"status": "running", "alive": True}},
        snapshot={"captured_at": now, "status": "running", "health": {"alive": True}},
        actor="host/env",
        project=P,
    )
    ok(completed["status"] == "completed" and
       completed["result"]["health"]["alive"] is True,
       "runner health completion preserves result evidence")
    refreshed = store.get_runner_session("run-env-managed", project=P)
    ok((refreshed["last_snapshot"] or {}).get("health", {}).get("alive") is True,
       "runner health completion updates session snapshot")

    agent_host = _load("agent_host_env_test", ROOT / "adapters" / "agent_host.py")
    inventory = {"host_id": "host/env"}
    calls = []
    runner_actions = []
    health_req = {
        "request_id": "runnerreq-health",
        "runner_session_id": "run-env-managed",
        "host_id": "host/env",
        "action": "health",
        "options": {},
    }
    logs_req = dict(health_req, request_id="runnerreq-logs", action="logs")

    def fake_try(method, path, body=None):
        calls.append((method, path, body or {}))
        if path.startswith(agent_host.P_LIST_RUNNER_CONTROLS):
            return {"requests": [health_req, logs_req]}
        if path == agent_host.P_CLAIM_RUNNER_CONTROL:
            return {"claimed": True}
        if path == agent_host.P_COMPLETE_RUNNER_CONTROL:
            return {"status": body.get("status")}
        return {"ok": True}

    def fake_supervisor_action(action, runner_session_id, options=None):
        runner_actions.append((action, runner_session_id))
        if action == "health":
            return {"status": "running", "alive": True,
                    "health": {"status": "running", "alive": True}}
        return {"last_snapshot": {"runner_session_id": runner_session_id,
                                  "log_tail": "tail from host"}}

    agent_host._try = fake_try
    agent_host.supervisor_action = fake_supervisor_action
    handled = agent_host.handle_runner_controls(inventory)
    complete_bodies = [c[2] for c in calls if c[1] == agent_host.P_COMPLETE_RUNNER_CONTROL]
    ok([h["action"] for h in handled] == ["health", "logs"],
       "Agent Host handles health and logs runner controls")
    ok(("health", "run-env-managed") in runner_actions and
       ("logs", "run-env-managed") in runner_actions,
       "Agent Host dispatches health/log actions to the supervisor")
    ok(any((b.get("snapshot") or {}).get("source") == "supervisor_status"
           for b in complete_bodies),
       "Agent Host completes health controls with a status snapshot")
    ok(any((b.get("snapshot") or {}).get("log_tail") == "tail from host"
           for b in complete_bodies),
       "Agent Host completes logs controls with log snapshot evidence")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
