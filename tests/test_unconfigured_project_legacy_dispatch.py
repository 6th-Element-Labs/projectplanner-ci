#!/usr/bin/env python3
"""Every project uses the policy-free canonical Connect launch path."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="legacy-dispatch-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

from switchboard.application.commands import connect_dispatch, execution_context  # noqa: E402
from switchboard.storage.repositories import project_execution_policy  # noqa: E402
from switchboard.storage.repositories import projects as projects_repo  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


TASK = {"task_id": "CO-99", "_wsId": "CO", "git_state": {}}

captured: list[dict] = []
resolve_calls: list[str] = []
saved_resolve = execution_context.resolve
saved_request = connect_dispatch.coordination_repo.request_wake
saved_capacity = connect_dispatch.capacity_readback
saved_policy = project_execution_policy.get_project_execution_policy
saved_topology = projects_repo.get_project_repo_topology


def fake_request_wake(**kwargs):
    captured.append(kwargs)
    return {"wake_id": "wake-legacy", "status": "pending"}


try:
    connect_dispatch.coordination_repo.request_wake = fake_request_wake
    connect_dispatch.capacity_readback = lambda *_a, **_kw: {}
    project_execution_policy.get_project_execution_policy = lambda project: {
        "configured": project in {"switchboard", "saved-draft-policy"},
        "activated": project == "switchboard",
        "lifecycle": {"status": "active" if project == "switchboard" else "draft"},
    }
    projects_repo.get_project_repo_topology = lambda project: {
        "roles": {"canonical": {
            "configured": True,
            "repo": ("6th-Element-Labs/projectplanner" if project == "switchboard"
                     else "6th-Element-Labs/ActionEngine"),
            "default_branch": "master" if project == "switchboard" else "main",
        }},
    }

    def refuse_if_called(**_kwargs):
        raise AssertionError(
            "policy-free launch must not resolve execution context")

    execution_context.resolve = refuse_if_called
    for project in (
        "switchboard", "maxwell", "saved-draft-policy",
        "future-board-created-after-deploy",
    ):
        result = connect_dispatch.enqueue_task(
            dict(TASK), project=project, actor="legacy-test")
        ok(result.get("dispatched") is True,
           f"project {project} dispatches regardless of policy state")
        wake_policy = captured[-1]["policy"] if captured else {}
        ok("execution_context" not in wake_policy
           and "placement" not in wake_policy
           and "scheduler" not in wake_policy,
           f"project {project} does not invent policy authority")
        ok((wake_policy.get("assignment") or {}).get("workspace_ref")
           == "repo:canonical",
           f"project {project} uses the canonical workspace ref")
        expected_repo = ("6th-Element-Labs/projectplanner" if project == "switchboard"
                         else "6th-Element-Labs/ActionEngine")
        expected_branch = "master" if project == "switchboard" else "main"
        ok(wake_policy.get("repository_binding") == {
            "schema": "switchboard.repository_binding.v1",
            "project": project,
            "repo_role": "canonical",
            "repository": expected_repo,
            "default_branch": expected_branch,
        }, f"project {project} carries its canonical repository binding")
    ok(resolve_calls == [],
       "no project resolves immutable execution context while launching")
    ok(len(captured) == 4,
       "each project requests exactly one wake")
finally:
    execution_context.resolve = saved_resolve
    connect_dispatch.coordination_repo.request_wake = saved_request
    connect_dispatch.capacity_readback = saved_capacity
    project_execution_policy.get_project_execution_policy = saved_policy
    projects_repo.get_project_repo_topology = saved_topology

print(f"\nunconfigured-project legacy dispatch: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
