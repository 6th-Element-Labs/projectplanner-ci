#!/usr/bin/env python3
"""COORD-123: one durable CI event survives into remediation assignment."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="coord123-pointer-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from execution_policy_fixture import (  # noqa: E402
    install_ready_execution_policy,
    ready_execution_context,
)
from switchboard.application.commands import connect_dispatch, task_execution  # noqa: E402
from switchboard.connect.execution_assignment import contract_fingerprint  # noqa: E402


P = "switchboard"
HEAD = "a" * 40
POINTER = {
    "schema": "switchboard.mission_launch_pointer.v4",
    "event_id": "missionevent-coord123",
    "event_sequence": 17,
    "ci_context": "Switchboard CI / VM gate",
    "failure_state": "failure",
    "evidence_url": "https://github.test/actions/runs/123",
    "exact_head_sha": HEAD,
}


def task(title: str) -> dict:
    created = store.create_task(
        {"workstream_id": "COORD", "title": title},
        actor="coord123-test",
        project=P,
    )
    created["git_state"] = {
        "branch": f"codex/{created['task_id']}-pointer",
        "head_sha": HEAD,
        "pr_number": 123,
        "pr_url": "https://github.test/pull/123",
    }
    return created


try:
    assert contract_fingerprint().startswith("eac1:")
    store.init_db(P)
    install_ready_execution_policy(P)
    connect_dispatch.execution_context.resolve = lambda **kwargs: (
        ready_execution_context(kwargs["task_id"], runtime=kwargs["runtime"])
    )

    exact = task("preserve exact remediation pointer")
    result = connect_dispatch.enqueue_task(
        exact,
        project=P,
        actor="coord123-test",
        runtime="codex",
        generation_ref="v4:1:COORD-123:17:remediation",
        role="remediation",
        source_sha=HEAD,
        mission_key="v4:1:COORD-123:17:remediation",
        mission_launch_pointer=POINTER,
    )
    assert result["dispatched"] is True
    wake = next(
        row for row in store.list_wake_intents(project=P)
        if row.get("wake_id") == result.get("wake_id")
    )
    assert wake["policy"]["lifecycle"]["mission_launch_pointer"] == POINTER
    assert wake["policy"]["execution_assignment"]["launch_pointer"] == POINTER

    production_missing = task("refuse actually missing production pointer")
    saved_projection = task_execution._projection
    saved_live_executions = task_execution.runner_repo.task_live_executions
    try:
        task_execution._projection = lambda *_args, **_kwargs: {
            "task": production_missing,
        }
        task_execution.runner_repo.task_live_executions = (
            lambda *_args, **_kwargs: []
        )
        try:
            task_execution.start_task(
                production_missing["task_id"],
                project=P,
                actor="coord123-test",
                role="remediation",
                source_sha=HEAD,
                mission_key="v4:1:COORD-126:20:remediation",
            )
        except task_execution.TaskExecutionError as exc:
            refused_production_missing = exc
        else:
            raise AssertionError("missing v4 remediation pointer was admitted")
    finally:
        task_execution._projection = saved_projection
        task_execution.runner_repo.task_live_executions = saved_live_executions
    assert refused_production_missing.code == "start_refused"
    assert refused_production_missing.message == (
        "execution_assignment_remediation_pointer_missing"
    )
    assert refused_production_missing.details["start_error"] == (
        "execution_assignment_remediation_pointer_missing"
    )

    ordinary_remediation = task("preserve ordinary remediation pointer")
    admitted_ordinary = connect_dispatch.enqueue_task(
        ordinary_remediation,
        project=P,
        actor="coord123-test",
        runtime="codex",
        generation_ref="remediation:ordinary",
        role="remediation",
        source_sha=HEAD,
        mission_key="remediation:ordinary",
    )
    assert admitted_ordinary["dispatched"] is True
    ordinary_wake = next(
        row for row in store.list_wake_intents(project=P)
        if row.get("wake_id") == admitted_ordinary.get("wake_id")
    )
    assert set(
        ordinary_wake["policy"]["execution_assignment"]["launch_pointer"]
    ) == {"trigger", "evidence_url"}

    missing = task("refuse missing remediation pointer")
    refused_missing = connect_dispatch.enqueue_task(
        missing,
        project=P,
        actor="coord123-test",
        runtime="codex",
        generation_ref="v4:1:COORD-124:18:remediation",
        role="remediation",
        source_sha=HEAD,
        mission_key="v4:1:COORD-124:18:remediation",
        mission_launch_pointer={"schema": POINTER["schema"]},
    )
    assert refused_missing == {
        "dispatched": False,
        "error": "execution_assignment_remediation_pointer_invalid",
        "diagnostic_cause": "execution_assignment_remediation_pointer_invalid",
        "failure_class": "missing_data",
        "role": "remediation",
        "task_id": missing["task_id"],
    }

    mismatch = task("refuse mismatched remediation head")
    refused_mismatch = connect_dispatch.enqueue_task(
        mismatch,
        project=P,
        actor="coord123-test",
        runtime="codex",
        generation_ref="v4:1:COORD-125:19:remediation",
        role="remediation",
        source_sha=HEAD,
        mission_key="v4:1:COORD-125:19:remediation",
        mission_launch_pointer={**POINTER, "exact_head_sha": "b" * 40},
    )
    assert refused_mismatch == {
        "dispatched": False,
        "error": "execution_assignment_remediation_pointer_head_mismatch",
        "diagnostic_cause": "execution_assignment_remediation_pointer_head_mismatch",
        "failure_class": "missing_data",
        "role": "remediation",
        "task_id": mismatch["task_id"],
    }
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print("COORD-123 remediation launch pointer: PASS")
