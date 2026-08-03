#!/usr/bin/env python3
"""HOST-4: every Coordination start door stamps the bounded profile name."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="host4-coordination-profile-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from execution_policy_fixture import (  # noqa: E402
    install_ready_execution_policy,
    ready_execution_context,
)
from switchboard.application.commands import connect_dispatch  # noqa: E402
from switchboard.connect.execution_assignment import (  # noqa: E402
    SWITCHBOARD_CI_VERIFICATION_PROFILE,
)


PROJECT = "switchboard"


try:
    store.init_db(PROJECT)
    install_ready_execution_policy(PROJECT)
    connect_dispatch.execution_context.resolve = lambda **kwargs: (
        ready_execution_context(
            kwargs["task_id"], project=PROJECT, runtime=kwargs["runtime"])
    )

    strict = store.create_task({
        "workstream_id": "HOST",
        "title": "Prove one locked verification profile",
        "description": "policy_profile:code_strict",
    }, actor="host4-test", project=PROJECT)
    result = connect_dispatch.enqueue_task(
        strict,
        project=PROJECT,
        actor="host4-test",
        runtime="codex",
        generation_ref="host4-strict-profile",
        role="implementation",
        session_policy_profile="code_strict",
    )
    assert result.get("dispatched") is True, result
    wake = next(
        row for row in store.list_wake_intents(project=PROJECT)
        if row.get("wake_id") == result.get("wake_id")
    )
    lifecycle = wake["policy"]["lifecycle"]
    assignment = wake["policy"]["execution_assignment"]
    assert lifecycle["verification_profile"] == (
        SWITCHBOARD_CI_VERIFICATION_PROFILE)
    assert assignment["verification_profile"] == (
        SWITCHBOARD_CI_VERIFICATION_PROFILE)
    assert assignment["session_policy_profile"] == "code_strict"
    assert "command" not in assignment

    relaxed = store.create_task({
        "workstream_id": "HOST",
        "title": "Documentation-only launch",
        "description": "policy_profile:docs_review",
    }, actor="host4-test", project=PROJECT)
    relaxed_result = connect_dispatch.enqueue_task(
        relaxed,
        project=PROJECT,
        actor="host4-test",
        runtime="codex",
        generation_ref="host4-docs-profile",
        role="implementation",
        session_policy_profile="docs_review",
    )
    assert relaxed_result.get("dispatched") is True, relaxed_result
    relaxed_wake = next(
        row for row in store.list_wake_intents(project=PROJECT)
        if row.get("wake_id") == relaxed_result.get("wake_id")
    )
    assert "verification_profile" not in relaxed_wake["policy"]["lifecycle"]
    assert "verification_profile" not in (
        relaxed_wake["policy"]["execution_assignment"])
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print("HOST-4 Coordination verification profile selection: PASS")
