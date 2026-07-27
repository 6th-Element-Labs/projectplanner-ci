#!/usr/bin/env python3
"""COORD-88 production authority belongs only to the seven-action reducer."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from unittest.mock import patch

from switchboard.application import completion_driver
from switchboard.application.completion_driver import production_effect_adapters
from switchboard.domain.completion.executor import CompletionEffectAdapters
from switchboard.domain.completion import executor as completion_executor
from switchboard.domain.completion.normalization_law import (
    FreshTick,
    LAW_BY_ACTION,
    NormalizedAction,
    reduce_fresh_tick,
)


HEAD = "a" * 40


def reduce(decision, *, snapshot=None, run=None, now=10.0):
    snapshot = {
        "task_id": "COORD-88",
        "pr_number": 995,
        "head_sha": HEAD,
        "runner": {"live": False},
        "wait_started_at": 9.0,
        **(snapshot or {}),
    }
    sources = {
        source: now
        for row in LAW_BY_ACTION.values()
        for source in row.authoritative_sources
    }
    return reduce_fresh_tick(
        decision=decision,
        snapshot=snapshot,
        run=run or {"run_id": "run-1", "state_version": 3, "attempt": 1},
        tick=FreshTick(
            observed_at=now,
            live_clock_at=now,
            source_observed_at=sources,
            head_sha=HEAD,
            wait_started_at=snapshot["wait_started_at"],
            snapshot_id="snapshot-1",
            controller_build_sha="b" * 40,
        ),
    )


def test_generic_coordination_retry_is_an_owned_block_not_an_agent_dispatch():
    result = reduce({
        "route": "coordination_retry",
        "effect": "update_branch",
        "reason_code": "mergeability_behind",
    })
    assert result["action"] == "BLOCK"
    assert result["command"]["effect"] == "escalate_human"
    assert result["command"]["fence_required"] is False
    assert result["command"]["retry_policy"] == "live_facts_only"


def test_infrastructure_retry_is_exact_run_bounded_and_never_remediation():
    first = reduce({
        "route": "coordination_retry",
        "reason_code": "required_ci_infrastructure_failure",
        "failing_check_url": (
            "https://github.com/example/project/actions/runs/123"
        ),
        "failing_run_attempt": 1,
    })
    assert first["action"] == "RETRY_CI"
    assert first["command"]["effect"] == "retry_ci"
    assert first["command"]["role"] is None

    exhausted = reduce({
        "route": "coordination_retry",
        "reason_code": "required_ci_infrastructure_failure",
        "failing_check_url": (
            "https://github.com/example/project/actions/runs/123"
        ),
        "failing_run_attempt": 2,
    })
    assert exhausted["action"] == "BLOCK"
    assert exhausted["reason_code"] == "ci_retry_bound_exhausted"
    assert exhausted["command"]["effect"] == "escalate_human"


def test_start_uses_only_start_task_role_and_live_runner_becomes_wait():
    start = reduce({
        "route": "remediation",
        "desired_role": "remediation",
        "reason_code": "required_exact_head_ci_failed",
    })
    assert start["action"] == "START"
    assert start["command"]["effect"] == "start_remediation"
    assert start["command"]["role"] == "remediation"

    waiting = reduce(
        {
            "route": "review_merge",
            "desired_role": "review_merge",
            "reason_code": "review_required",
        },
        snapshot={
            "runner": {
                "live": True,
                "role": "implementation",
                "runner_session_id": "run-live",
            },
        },
    )
    assert waiting["action"] == "WAIT"
    assert waiting["command"]["effect"] == "attach_and_wait"
    assert waiting["command"]["fence_identity"] is None


def test_wrong_role_runner_waits_without_borrowing_capacity_authority():
    waiting = reduce(
        {
            "route": "coordination_retry",
            "effect": "fence_runner",
            "reason_code": "live_runner_not_desired",
        },
        snapshot={
            "runner": {
                "live": True,
                "role": "review_merge",
                "runner_session_id": "run-live",
            },
        },
    )
    assert waiting["action"] == "WAIT"
    assert waiting["command"]["effect"] == "wait"
    assert waiting["command"]["fence_required"] is False
    assert waiting["command"]["fence_identity"] is None


def test_live_implementation_owns_capacity_during_pr_ci_hydration():
    waiting = reduce(
        {
            "route": "human",
            "effect": "escalate_human",
            "reason_code": "required_ci_hydration_missing",
        },
        snapshot={
            "runner": {
                "live": True,
                "role": "implementation",
                "runner_session_id": "run-live",
            },
            "github_pr": {"state": "OPEN", "draft": False},
            "status_contexts": {},
        },
    )
    assert waiting["action"] == "WAIT"
    assert waiting["reason_code"] == "required_ci_hydration_missing"
    assert waiting["command"]["effect"] == "attach_and_wait"
    assert waiting["command"]["mutates"] is False
    assert "block" not in waiting


def test_ready_queue_and_merge_observation_stay_mechanical():
    ready = reduce({
        "route": "review_merge",
        "effect": "mark_ready_then_reread",
        "reason_code": "pr_draft",
    })
    assert ready["action"] == "MARK_READY"
    assert ready["command"]["effect"] == "mark_ready"

    queue = reduce({
        "route": "review_merge",
        "effect": "enqueue",
        "reason_code": "merge_authorized",
    })
    assert queue["action"] == "ARM_MERGE"
    assert queue["command"]["effect"] == "enqueue"

    merged = reduce(
        {
            "route": "reconcile",
            "reason_code": "canonical_merge_observed",
        },
        snapshot={
            "github_pr": {"state": "MERGED"},
            "merge_provenance": {"merged_sha": "c" * 40},
            "runner": {"live": True, "role": "implementation"},
        },
    )
    assert merged["action"] == "MERGED"
    assert merged["controller_may_author_done"] is False


def test_production_adapter_set_has_no_legacy_lifecycle_side_doors():
    from switchboard.storage.repositories import projects, provenance

    original_repo = projects.get_project_github_repo
    original_token = provenance._github_token
    try:
        projects.get_project_github_repo = lambda _project: "example/project"
        provenance._github_token = lambda: ""
        adapters = production_effect_adapters(
            project="switchboard",
            actor="test",
            agent_id="codex/COORD-88",
        )
    finally:
        projects.get_project_github_repo = original_repo
        provenance._github_token = original_token
    assert adapters.update_branch is None
    assert adapters.repair_dispatch is None
    assert adapters.fence_runner is None
    assert adapters.reconcile_provenance is None


def test_retired_planner_is_not_loaded_by_production_reducer():
    snapshot = {
        "schema": "switchboard.completion_snapshot.v1",
        "task_id": "COORD-88",
        "pr_number": 995,
        "head_sha": HEAD,
        "observed_at": 10.0,
        "hydration_started_at": 10.0,
        "wait_started_at": 9.0,
        "source_observed_at": {
            source: 10.0
            for row in LAW_BY_ACTION.values()
            for source in row.authoritative_sources
        },
        "github_pr": {"state": "OPEN", "draft": False},
        "status_contexts": {},
        "review_verdict": {},
        "merge_gate": {},
        "merge_queue": {},
        "runner": {"live": False},
    }
    decision = {
        "state": "waiting",
        "route": "wait",
        "reason_code": "required_exact_head_ci_pending",
        "effect": "wait",
    }
    with (
        patch.object(completion_driver, "classify_completion",
                     return_value=decision),
        patch.object(completion_driver, "ensure_completion_run",
                     return_value={"run_id": "run-1", "state_version": 1}),
        patch(
            "switchboard.storage.repositories.completion_runs."
            "get_active_completion_run",
            return_value={},
        ),
        patch(
            "switchboard.storage.repositories.decision_records."
            "record_decision_episode",
            return_value={},
        ),
        patch("time.time", return_value=10.0),
    ):
        result = completion_driver.run_completion_tick(
            "COORD-88",
            project="switchboard",
            actor="test",
            agent_id="codex/COORD-88",
            hydrator=lambda *_args, **_kwargs: snapshot,
            adapters=CompletionEffectAdapters(),
            shadow_observer=lambda _observation: {"recorded": True},
        )
    assert result["normalized"]["action"] == "WAIT"
    assert result["plan"]["effect"] == "wait"
    assert result["observation"]["action"] == "WAIT"
    assert not hasattr(completion_driver, "plan_effect")
    assert not hasattr(completion_executor, "plan_effect")


if __name__ == "__main__":
    passed = failed = 0
    for name, case in sorted(dict(globals()).items()):
        if not name.startswith("test_") or not callable(case):
            continue
        try:
            case()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {exc}")
        else:
            passed += 1
            print(f"PASS  {name}")
    print(f"\nCOORD-88 thin cutover: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
