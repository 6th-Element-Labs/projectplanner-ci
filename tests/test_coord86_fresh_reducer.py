#!/usr/bin/env python3
"""COORD-86 fresh reducer identity and typed-command boundaries."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from switchboard.application.completion_driver import (  # noqa: E402
    _fresh_decision_context,
)
from switchboard.application.commands.completion_orchestration import (  # noqa: E402
    execute_normalized_command,
)
from switchboard.domain.completion.normalization_law import (  # noqa: E402
    FreshTick,
    LAW_BY_ACTION,
    NormalizedAction,
    normalize_fresh_tick,
)


def test_cached_verdict_is_not_a_decision_input():
    current = {
        "state": "blocked",
        "route": "remediation",
        "reason_code": "cached_product_verdict",
        "desired_role": "remediation",
        "board_status": "Blocked",
        "evidence_refs": {
            "decision": {"route": "remediation"},
            "review": {"verdict": "changes_requested"},
            "convergence": {
                "head_sha": "a" * 40,
                "churn": 2,
                "stable_replays": 0,
            },
        },
    }
    assert _fresh_decision_context(current) == {}


def test_source_observation_times_are_preserved_not_fabricated():
    row = LAW_BY_ACTION[NormalizedAction.MARK_READY]
    source_times = {
        "github_pr": 9.25,
        "review_verdict": 9.75,
    }
    normalized = normalize_fresh_tick(
        decision={"reason_code": "pr_draft"},
        plan={
            "effect": "mark_ready",
            "task_id": "COORD-86",
            "head_sha": "a" * 40,
            "idem_key": "idem-ready",
        },
        snapshot={"task_id": "COORD-86", "head_sha": "a" * 40},
        tick=FreshTick(
            observed_at=10.0,
            live_clock_at=10.0,
            source_observed_at=source_times,
            head_sha="a" * 40,
            snapshot_id="snapshot-ready",
            controller_build_sha="b" * 40,
        ),
    )
    assert set(source_times) == set(row.authoritative_sources)
    assert normalized["source_observed_at"] == source_times
    assert len(set(normalized["source_observed_at"].values())) == 2


def test_wait_requires_immutable_origin_and_replay_keeps_fixed_deadline():
    row = LAW_BY_ACTION[NormalizedAction.WAIT]
    sources = {source: 10.0 for source in row.authoritative_sources}
    plan = {
        "effect": "wait",
        "task_id": "COORD-86",
        "head_sha": "a" * 40,
        "idem_key": "idem-wait",
    }
    snapshot = {"task_id": "COORD-86", "head_sha": "a" * 40}
    try:
        normalize_fresh_tick(
            decision={},
            plan=plan,
            snapshot=snapshot,
            tick=FreshTick(
                observed_at=10.0,
                live_clock_at=10.0,
                source_observed_at=sources,
                head_sha="a" * 40,
            ),
        )
    except ValueError as exc:
        assert "authoritative wait_started_at" in str(exc)
    else:
        raise AssertionError("WAIT synthesized a mutable origin")

    deadlines = []
    for live_clock_at in (10.0, 20.0):
        replay_sources = {
            source: live_clock_at for source in row.authoritative_sources
        }
        normalized = normalize_fresh_tick(
            decision={},
            plan=plan,
            snapshot=snapshot,
            tick=FreshTick(
                observed_at=live_clock_at,
                live_clock_at=live_clock_at,
                source_observed_at=replay_sources,
                head_sha="a" * 40,
                wait_started_at=5.0,
            ),
        )
        deadlines.append(normalized["wait"]["deadline_at"])
    assert deadlines == [5.0 + row.live_clock_bound_s] * 2


def test_update_branch_has_no_identical_sha_retry_mapping():
    row = LAW_BY_ACTION[NormalizedAction.RETRY_CI]
    try:
        normalize_fresh_tick(
            decision={},
            plan={
                "effect": "update_branch",
                "task_id": "COORD-86",
                "head_sha": "a" * 40,
                "idem_key": "idem-update-branch",
            },
            snapshot={"task_id": "COORD-86", "head_sha": "a" * 40},
            tick=FreshTick(
                observed_at=10.0,
                live_clock_at=10.0,
                source_observed_at={
                    source: 10.0 for source in row.authoritative_sources
                },
                head_sha="a" * 40,
            ),
        )
    except ValueError as exc:
        assert "unsupported completion effect" in str(exc)
    else:
        raise AssertionError("head-changing update_branch mapped to RETRY_CI")


def test_typed_command_requires_the_normalized_command():
    normalized = {
        "action": "ARM_MERGE",
        "snapshot_id": "snapshot-1",
        "observed_at": 1.0,
        "controller_build_sha": "b" * 40,
        "table_version": "1",
        "idempotency_key": "idem-1",
    }
    try:
        execute_normalized_command(
            normalized,
            decision={},
            snapshot={},
            run={},
            project="switchboard",
            actor="test",
        )
    except ValueError as exc:
        assert "normalized completion command is required" in str(exc)
    else:
        raise AssertionError("missing normalized command reached an adapter")


def test_reconcile_without_merge_provenance_becomes_owned_block():
    row = LAW_BY_ACTION[NormalizedAction.MERGED]
    normalized = normalize_fresh_tick(
        decision={"reason_code": "completion_not_converging_reconcile"},
        plan={
            "effect": "reconcile_provenance",
            "task_id": "COORD-86",
            "head_sha": "a" * 40,
            "idem_key": "idem-reconcile",
        },
        snapshot={
            "task_id": "COORD-86",
            "head_sha": "a" * 40,
            "github_pr": {"state": "OPEN"},
        },
        tick=FreshTick(
            observed_at=10.0,
            live_clock_at=10.0,
            source_observed_at={
                source: 10.0 for source in row.authoritative_sources
            },
            head_sha="a" * 40,
            snapshot_id="snapshot-reconcile",
            controller_build_sha="b" * 40,
        ),
    )
    assert normalized["action"] == "BLOCK"
    assert normalized["reason_code"] == "canonical_merge_provenance_missing"
    assert normalized["block"]["owner"]


def test_infrastructure_failure_without_run_identity_blocks_instead_of_dispatching():
    row = LAW_BY_ACTION[NormalizedAction.RETRY_CI]
    normalized = normalize_fresh_tick(
        decision={"reason_code": "required_ci_infrastructure_failure"},
        plan={
            "effect": "repair_dispatch",
            "task_id": "COORD-86",
            "head_sha": "a" * 40,
            "idem_key": "idem-infra",
        },
        snapshot={"task_id": "COORD-86", "head_sha": "a" * 40},
        tick=FreshTick(
            observed_at=10.0,
            live_clock_at=10.0,
            source_observed_at={
                source: 10.0 for source in row.authoritative_sources
            },
            head_sha="a" * 40,
            snapshot_id="snapshot-infra",
            controller_build_sha="b" * 40,
        ),
    )
    assert normalized["action"] == "BLOCK"
    assert normalized["reason_code"] == "ci_retry_identity_missing"


def test_infrastructure_retry_uses_live_run_attempt_bound():
    row = LAW_BY_ACTION[NormalizedAction.RETRY_CI]

    def normalize(attempt):
        return normalize_fresh_tick(
            decision={"reason_code": "required_ci_infrastructure_failure"},
            plan={
                "effect": "repair_dispatch",
                "task_id": "COORD-86",
                "head_sha": "a" * 40,
                "idem_key": f"idem-infra-{attempt}",
                "failing_check_url": (
                    "https://github.com/example/repo/actions/runs/123"
                ),
                "failing_run_attempt": attempt,
            },
            snapshot={"task_id": "COORD-86", "head_sha": "a" * 40},
            tick=FreshTick(
                observed_at=10.0,
                live_clock_at=10.0,
                source_observed_at={
                    source: 10.0 for source in row.authoritative_sources
                },
                head_sha="a" * 40,
            ),
        )

    assert normalize(1)["action"] == "RETRY_CI"
    exhausted = normalize(2)
    assert exhausted["action"] == "BLOCK"
    assert exhausted["reason_code"] == "ci_retry_bound_exhausted"


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
    print(f"\nCOORD-86 fresh reducer: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
