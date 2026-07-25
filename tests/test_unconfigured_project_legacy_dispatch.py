#!/usr/bin/env python3
"""An unconfigured project must still dispatch on the legacy Connect path.

Regression for the 2026-07-25 evening outage. CO-20 (#863) removed the
COORD-47 guard in connect_dispatch.enqueue_task and made
execution_context.resolve unconditional. No project has ever been configured,
so within minutes of its own hands-off merge every dispatch on the board was
refused with "no project execution policy is configured" and the fleet
stopped. Same seam UI-63 broke that morning from task_execution's side
(BUG-190, #891): a change that tightens the no-policy contract, shipped before
the configured path was reachable.

This pins both sides at the dispatch boundary:
  - unconfigured project -> legacy wake policy, resolve never consulted
  - configured project   -> project-derived hybrid placement, fail closed

Delete the legacy branch only in the same change that configures the project
and proves a configured dispatch end-to-end; then update this test, not just
the code.
"""
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
saved_policy = project_execution_policy.get_project_execution_policy
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

    # --- 1. The outage case: unconfigured project dispatches on the legacy path.
    project_execution_policy.get_project_execution_policy = (
        lambda _project: {"configured": False})
    result = connect_dispatch.enqueue_task(
        dict(TASK), project="switchboard", actor="legacy-test")
    ok(result.get("dispatched") is True,
       "an unconfigured project still dispatches")
    ok(resolve_calls == [],
       "execution_context.resolve is never consulted for an unconfigured project")
    ok(result.get("error") is None
       and "project_execution_policy_missing" not in str(result),
       "no readiness refusal leaks into the legacy dispatch result")
    ok(len(captured) == 1, "exactly one wake is requested")
    legacy_policy = captured[0]["policy"]
    ok("assignment" in legacy_policy and "lifecycle" in legacy_policy,
       "legacy wake policy carries the assignment and lifecycle contracts")
    ok("placement" not in legacy_policy and "execution_context" not in legacy_policy,
       "legacy wake policy does not fabricate hybrid placement from an empty context")

    # --- 2. The configured path is untouched: project-derived, fail closed.
    captured.clear()
    resolve_calls.clear()
    project_execution_policy.get_project_execution_policy = (
        lambda _project: {"configured": True})
    result = connect_dispatch.enqueue_task(
        dict(TASK), project="switchboard", actor="legacy-test")
    ok(result.get("dispatched") is True and resolve_calls == ["CO-99"],
       "a configured project resolves its execution context")
    configured_policy = captured[0]["policy"]
    ok("placement" in configured_policy and "scheduler" in configured_policy
       and "execution_context" in configured_policy,
       "a configured project's wake carries hybrid placement")

    # --- 3. A configured project with a broken policy still fails closed.
    captured.clear()

    def refuse(**_kwargs):
        raise execution_context.ExecutionContextError(
            "project_execution_policy_missing", "policy incomplete")

    execution_context.resolve = refuse
    result = connect_dispatch.enqueue_task(
        dict(TASK), project="switchboard", actor="legacy-test")
    ok(result.get("dispatched") is False
       and result.get("error") == "project_execution_policy_missing",
       "a configured project with an unresolvable policy is refused, not downgraded")
    ok(captured == [], "the fail-closed refusal requests no wake")
finally:
    execution_context.resolve = saved_resolve
    project_execution_policy.get_project_execution_policy = saved_policy
    connect_dispatch.coordination_repo.request_wake = saved_request
    connect_dispatch.capacity_readback = saved_capacity

print(f"\nunconfigured-project legacy dispatch: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
