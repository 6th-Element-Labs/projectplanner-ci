#!/usr/bin/env python3
"""Unconfigured projects fail closed instead of creating legacy Connect wakes."""
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

    # Every project resolves the same immutable context before a wake exists.
    result = connect_dispatch.enqueue_task(
        dict(TASK), project="switchboard", actor="legacy-test")
    ok(result.get("dispatched") is True,
       "a configured execution context dispatches")
    ok(resolve_calls == ["CO-99"],
       "execution_context.resolve is always consulted")
    ok(len(captured) == 1, "exactly one wake is requested")
    configured_policy = captured[0]["policy"]
    ok("placement" in configured_policy and "scheduler" in configured_policy
       and "execution_context" in configured_policy,
       "a configured project's wake carries hybrid placement")

    # Missing or invalid configuration never downgrades to a context-less wake.
    captured.clear()

    def refuse(**_kwargs):
        raise execution_context.ExecutionContextError(
            "project_execution_policy_missing", "policy incomplete")

    execution_context.resolve = refuse
    result = connect_dispatch.enqueue_task(
        dict(TASK), project="switchboard", actor="legacy-test")
    ok(result.get("dispatched") is False
       and result.get("error") == "project_execution_policy_missing",
       "an unconfigured project is refused, not downgraded")
    ok(captured == [], "the fail-closed refusal requests no wake")
finally:
    execution_context.resolve = saved_resolve
    connect_dispatch.coordination_repo.request_wake = saved_request
    connect_dispatch.capacity_readback = saved_capacity

print(f"\nunconfigured-project legacy dispatch: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
