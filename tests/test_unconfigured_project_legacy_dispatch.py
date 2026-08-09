#!/usr/bin/env python3
"""Unconfigured projects retain the project-independent Connect launch path."""
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


def counting_resolve(**kwargs):
    resolve_calls.append(kwargs.get("task_id") or "")
    return {
        "schema": execution_context.SCHEMA,
        "project_id": "switchboard",
        "repository": "6th-Element-Labs/projectplanner",
        "repo_role": "canonical",
        "base_sha": "a" * 40,
        "placement": {"host_classes": ["personal"], "trust_zones": ["personal"],
                      "burst": {"enabled": False}},
        "provider": {"provider": "openai-codex", "account_affinity_id": "affinity-a"},
        "workspace": {"isolation": "worktree"},
        "scm": {"provider": "github_app"},
    }


try:
    execution_context.resolve = counting_resolve
    connect_dispatch.coordination_repo.request_wake = fake_request_wake
    connect_dispatch.capacity_readback = lambda *_a, **_kw: {}

    project_execution_policy.get_project_execution_policy = (
        lambda _project: {"configured": True, "activated": True})
    result = connect_dispatch.enqueue_task(
        dict(TASK), project="switchboard", actor="legacy-test")
    ok(result.get("dispatched") is True,
       "a configured execution context dispatches")
    ok(resolve_calls == ["CO-99"],
       "a configured project resolves immutable execution context")
    ok(len(captured) == 1, "exactly one wake is requested")
    configured_policy = captured[0]["policy"]
    ok("placement" in configured_policy and "scheduler" in configured_policy
       and "execution_context" in configured_policy,
       "a configured project's wake carries hybrid placement")

    # No policy means the normal provider-neutral path for every board project.
    captured.clear()
    resolve_calls.clear()
    project_execution_policy.get_project_execution_policy = lambda project: {
        "configured": project == "saved-draft-policy",
        "activated": False,
        "lifecycle": {"status": "draft"},
    }
    projects_repo.get_project_repo_topology = lambda project: {
        "roles": {"canonical": {
            "configured": True,
            "repo": "6th-Element-Labs/ActionEngine",
            "default_branch": "main",
        }},
    }

    def refuse_if_called(**_kwargs):
        raise AssertionError(
            "unconfigured launch must not resolve execution context")

    execution_context.resolve = refuse_if_called
    for project in (
        "maxwell", "saved-draft-policy", "future-board-created-after-deploy",
    ):
        result = connect_dispatch.enqueue_task(
            dict(TASK), project=project, actor="legacy-test")
        ok(result.get("dispatched") is True,
           f"unconfigured project {project} dispatches")
        wake_policy = captured[-1]["policy"] if captured else {}
        ok("execution_context" not in wake_policy
           and "placement" not in wake_policy
           and "scheduler" not in wake_policy,
           f"unconfigured project {project} does not invent policy authority")
        ok((wake_policy.get("assignment") or {}).get("workspace_ref")
           == "repo:canonical",
           f"unconfigured project {project} uses the compatibility workspace ref")
        ok(wake_policy.get("repository_binding") == {
            "schema": "switchboard.repository_binding.v1",
            "project": project,
            "repo_role": "canonical",
            "repository": "6th-Element-Labs/ActionEngine",
            "default_branch": "main",
        }, f"unconfigured project {project} carries its canonical repository binding")
    ok(resolve_calls == [],
       "unconfigured projects never resolve immutable execution context")
    ok(len(captured) == 3,
       "each unconfigured project requests exactly one wake")
finally:
    execution_context.resolve = saved_resolve
    connect_dispatch.coordination_repo.request_wake = saved_request
    connect_dispatch.capacity_readback = saved_capacity
    project_execution_policy.get_project_execution_policy = saved_policy
    projects_repo.get_project_repo_topology = saved_topology

print(f"\nunconfigured-project legacy dispatch: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
