#!/usr/bin/env python3
"""COORD-32: partial wake completion preserves delegated runner binding."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = tempfile.mkdtemp(prefix="coord32-runner-binding-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402


P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_db(P)
    task = store.create_task(
        {"workstream_id": "COORD", "title": "runner binding regression"},
        actor="coord32-test", project=P)
    task_id = task["task_id"]
    host_id = "host/coord32"
    runner_id = "run_coord32"

    store.register_host({
        "host_id": host_id,
        "runtimes": [{"runtime": "claude-code", "lanes": ["COORD"]}],
        "capacity": {"max_sessions": 1, "active_sessions": 0},
        "heartbeat_ttl_s": 60,
    }, actor=host_id, project=P)
    wake = store.request_wake(
        {"runtime": "claude-code", "lane": "COORD",
         "agent_id": f"claude-code/{task_id}"},
        reason="COORD-32 regression", source="coord32-test", task_id=task_id,
        actor="coord32-test", project=P)
    claimed = store.claim_wake(host_id, wake["wake_id"],
                               actor=host_id, project=P)
    ok(claimed.get("claimed") is True,
       "host claims the delegated wake")

    # The host's preclaim row is intentionally short-lived.
    store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": host_id,
        "agent_id": f"claude-code/{task_id}",
        "runtime": "claude-code",
        "task_id": task_id,
        "status": "starting",
        "cwd": "/srv/preclaim",
        "control": {"managed_process": True, "runner_kill": True, "tier": "T3"},
        "metadata": {"credential_admission_phase": "preclaim"},
        "heartbeat_ttl_s": 60,
    }, actor=host_id, project=P)

    # The delegated worker then establishes the authoritative claim/session binding.
    store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": host_id,
        "agent_id": f"claude-code/{task_id}",
        "runtime": "claude-code",
        "task_id": task_id,
        "claim_id": "taskclaim-coord32",
        "status": "running",
        "cwd": "/srv/worktrees/coord32",
        "control": {"managed_process": True, "runner_kill": True, "tier": "T3"},
        "metadata": {
            "credential_admission_phase": "claim_bound",
            "work_session_id": "worksession-coord32",
            "provider": "anthropic-claude",
        },
        "heartbeat_ttl_s": 1800,
    }, actor=f"claude-code/{task_id}", project=P)

    # Personal-worker wake completion contains provider proof only.  It must not
    # downgrade the already-bound runner row back to preclaim defaults.
    completed = store.complete_wake(
        wake["wake_id"], runner_session_id=runner_id,
        agent_id=f"claude-code/{task_id}",
        result={
            "started": True,
            "reason": "personal_subscription_smoke_completed",
            "provider": "anthropic-claude",
            "auth_mode": "oauth_personal",
            "credential_values_redacted": True,
        },
        actor=f"claude-code/{task_id}", project=P)
    runner = store.get_runner_session(runner_id, project=P)

    ok(completed.get("status") == "completed",
       "partial provider receipt completes the wake")
    ok(runner.get("claim_id") == "taskclaim-coord32"
       and runner.get("cwd") == "/srv/worktrees/coord32"
       and runner.get("heartbeat_ttl_s") == 1800,
       "wake completion preserves claim, workspace, and authoritative TTL")
    ok(runner.get("metadata", {}).get("work_session_id") == "worksession-coord32"
       and runner.get("metadata", {}).get("credential_admission_phase") == "claim_bound"
       and runner.get("metadata", {}).get("wake_id") == wake["wake_id"],
       "wake evidence is merged without erasing claim-bound metadata")
    ok(runner.get("control", {}).get("runner_kill") is True
       and runner.get("control", {}).get("tier") == "T3",
       "wake completion preserves managed runner control fidelity")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nCOORD-32 wake runner binding: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
