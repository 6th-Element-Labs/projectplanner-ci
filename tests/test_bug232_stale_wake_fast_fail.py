#!/usr/bin/env python3
"""BUG-232/BUG-345: stale execution policy cannot fail a policy-free wake."""
from __future__ import annotations

import os
import shutil
import tempfile
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="bug232-stale-wake-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_RUNNER_DIR"] = os.path.join(TMP, "runner")

import store  # noqa: E402
from execution_readiness_fixture import configure_ready_project  # noqa: E402
from switchboard.application.commands import execution_context, task_execution  # noqa: E402

P = "switchboard"


def start(task_id: str, attempt: int) -> dict:
    return task_execution.start_task(
        task_id,
        project=P,
        actor="bug232-test",
        role="implementation",
        decision_attempt=attempt,
        state_version=attempt,
    )


try:
    store.init_db(P)
    configure_ready_project(P, actor="bug232-test")
    task = store.create_task(
        {"workstream_id": "BUG", "title": "BUG-232 regression", "ui_impact": "no"},
        actor="bug232-test",
        project=P,
    )
    task_id = task["task_id"]
    first = start(task_id, 1)
    assert first.get("started") is True, first
    wake_id = first["wake_id"]
    registered = store.register_host({
        "host_id": "host/bug232",
        "runtimes": [{
            "runtime": "codex",
            "lanes": ["BUG"],
            "capabilities": [
                "execution_lease_v2", "runner_lease_enforcement"],
        }],
        "capacity": {"active_sessions": 0},
        "heartbeat_ttl_s": 60,
    }, actor="host/bug232", project=P)
    assert not registered.get("error"), registered

    stale = execution_context.ExecutionContextError(
        "stale_execution_context",
        "canonical base changed",
        expected_digest="sha256:old",
        current_digest="sha256:new",
    )
    with patch.object(execution_context, "require_current", side_effect=stale):
        claimed = store.claim_wake(
            "host/bug232",
            wake_id,
            actor="bug232-host",
            project=P,
        )

    assert claimed["claimed"] is True, claimed
    assert "execution_context" not in claimed["wake"]["policy"], claimed

    completed = store.complete_wake(
        wake_id,
        runner_session_id="run-bug232",
        agent_id=f"agent/codex/{task_id.lower()}",
        result={"started": True},
        actor="host/bug232",
        project=P,
    )
    assert completed["status"] == "completed", completed

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("BUG-232 stale policy cannot fail policy-free wake: passed")
