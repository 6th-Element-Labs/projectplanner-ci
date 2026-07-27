#!/usr/bin/env python3
"""COORD-84 normalization-law schema and full-axis totality."""
from __future__ import annotations

from pathlib import Path
import sys

TESTS = Path(__file__).resolve().parents[1]
ROOT = TESTS.parent
SRC = ROOT / "src"
HERE = Path(__file__).resolve().parent
for entry in (str(HERE), str(TESTS), str(SRC), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from switchboard.domain.completion.effects import plan_effect  # noqa: E402
from switchboard.domain.completion.normalization_law import (  # noqa: E402
    FreshTick,
    LAW_BY_ACTION,
    NormalizedAction,
    normalize_fresh_tick,
    validate_law_table,
)
from switchboard.domain.completion.state_machine import classify_completion  # noqa: E402

import _shared  # noqa: E402
import test_property_invariants as properties  # noqa: E402


NOW = 1_785_134_800.0


def _tick(action: NormalizedAction, head: str, prior_head: str = "") -> FreshTick:
    sources = {
        source: NOW - 1
        for source in LAW_BY_ACTION[action].authoritative_sources
    }
    return FreshTick(
        observed_at=NOW,
        live_clock_at=NOW,
        source_observed_at=sources,
        head_sha=head,
        prior_head_sha=prior_head,
        wait_started_at=NOW - 1 if action is NormalizedAction.WAIT else None,
    )


def _action_for(plan: dict) -> NormalizedAction:
    effect = plan["effect"]
    return {
        "wait": NormalizedAction.WAIT,
        "attach_and_wait": NormalizedAction.WAIT,
        "none": NormalizedAction.WAIT,
        "ensure_review_generation": NormalizedAction.START,
        "start_remediation": NormalizedAction.START,
        "repair_dispatch": NormalizedAction.RETRY_CI,
        "update_branch": NormalizedAction.RETRY_CI,
        "mark_ready": NormalizedAction.MARK_READY,
        "enqueue": NormalizedAction.ARM_MERGE,
        "escalate_human": NormalizedAction.BLOCK,
        "reconcile_provenance": NormalizedAction.MERGED,
    }[effect]


def test_law_table_is_versioned_unique_and_total():
    result = validate_law_table()
    assert result["valid"], result["errors"]
    assert set(result["actions"]) == {action.value for action in NormalizedAction}


def test_every_existing_conformance_axis_cell_normalizes_once():
    count = 0
    for scenario in properties.pr_worlds():
        snapshot = _shared.build_snapshot(
            scenario, task_id=f"LAW-{scenario['id']}")
        run = {"run_id": f"run-{scenario['id']}", "attempt": 0, "state_version": 1}
        decision = classify_completion(run, snapshot)
        plan = plan_effect(decision, snapshot, run)
        if plan["effect"] == "update_branch":
            try:
                normalize_fresh_tick(
                    decision=decision,
                    plan=plan,
                    snapshot=snapshot,
                    tick=_tick(
                        NormalizedAction.RETRY_CI, snapshot["head_sha"]),
                )
            except ValueError as exc:
                assert "unsupported completion effect" in str(exc)
            else:
                raise AssertionError("update_branch acquired retry authority")
            count += 1
            continue
        action = _action_for(plan)
        normalized = normalize_fresh_tick(
            decision=decision,
            plan=plan,
            snapshot=snapshot,
            tick=_tick(action, snapshot["head_sha"]),
        )
        assert normalized["action"] in {action.value, "BLOCK"}
        assert normalized["head_sha"] == snapshot["head_sha"]
        assert normalized["idempotency_key"].startswith(plan["idem_key"])
        count += 1
    assert count == 1080


def test_new_head_invalidates_all_head_scoped_history():
    scenario = next(iter(properties.pr_worlds()))
    snapshot = _shared.build_snapshot(scenario, task_id="LAW-NEW-HEAD")
    run = {"run_id": "run-new-head", "attempt": 0, "state_version": 2}
    decision = classify_completion(run, snapshot)
    plan = plan_effect(decision, snapshot, run)
    action = _action_for(plan)
    normalized = normalize_fresh_tick(
        decision=decision,
        plan=plan,
        snapshot=snapshot,
        tick=_tick(action, snapshot["head_sha"], prior_head="d" * 40),
    )
    assert normalized["head_changed"] is True
    assert normalized["invalidated_evidence"] == [
        "ci", "review", "merge_queue", "retry"]


def test_wait_and_block_have_required_operating_contracts():
    wait_scenario = properties._scenario("law-wait", ci="pending")
    wait_snapshot = _shared.build_snapshot(wait_scenario, task_id="LAW-WAIT")
    run = {"run_id": "run-wait", "attempt": 0, "state_version": 1}
    wait_decision = classify_completion(run, wait_snapshot)
    wait_plan = plan_effect(wait_decision, wait_snapshot, run)
    waited = normalize_fresh_tick(
        decision=wait_decision,
        plan=wait_plan,
        snapshot=wait_snapshot,
        tick=_tick(NormalizedAction.WAIT, wait_snapshot["head_sha"]),
    )
    assert set(waited["wait"]) == {
        "reason", "evidence", "owner", "deadline_at", "next_observation"}

    block_scenario = properties._scenario(
        "law-block", ci="action_required")
    block_snapshot = _shared.build_snapshot(block_scenario, task_id="LAW-BLOCK")
    block_decision = classify_completion(run, block_snapshot)
    block_plan = plan_effect(block_decision, block_snapshot, run)
    blocked = normalize_fresh_tick(
        decision=block_decision,
        plan=block_plan,
        snapshot=block_snapshot,
        tick=_tick(NormalizedAction.BLOCK, block_snapshot["head_sha"]),
    )
    assert set(blocked["block"]) == {
        "reason", "owner", "live_evidence", "minimum_repair"}


def test_wait_deadline_is_immutable_across_ticks_and_expires_to_owned_block():
    scenario = properties._scenario("law-wait-multi-tick", ci="pending")
    snapshot = _shared.build_snapshot(scenario, task_id="LAW-WAIT-MULTI-TICK")
    run = {"run_id": "run-wait-multi-tick", "attempt": 0, "state_version": 1}
    decision = classify_completion(run, snapshot)
    plan = plan_effect(decision, snapshot, run)
    wait_started_at = NOW - 10

    def normalize_at(live_clock_at: float) -> dict:
        return normalize_fresh_tick(
            decision=decision,
            plan=plan,
            snapshot=snapshot,
            tick=FreshTick(
                observed_at=live_clock_at,
                live_clock_at=live_clock_at,
                source_observed_at={
                    source: live_clock_at - 1
                    for source in LAW_BY_ACTION[
                        NormalizedAction.WAIT].authoritative_sources
                },
                head_sha=snapshot["head_sha"],
                wait_started_at=wait_started_at,
            ),
        )

    first = normalize_at(NOW)
    second = normalize_at(NOW + 60)
    expired = normalize_at(
        wait_started_at
        + LAW_BY_ACTION[NormalizedAction.WAIT].live_clock_bound_s
    )

    expected_deadline = (
        wait_started_at
        + LAW_BY_ACTION[NormalizedAction.WAIT].live_clock_bound_s
    )
    assert first["action"] == second["action"] == "WAIT"
    assert first["wait"]["deadline_at"] == expected_deadline
    assert second["wait"]["deadline_at"] == expected_deadline
    assert expired["action"] == "BLOCK"
    assert expired["reason_code"] == "wait_deadline_expired"
    assert expired["idempotency_key"] == (
        f"{plan['idem_key']}:wait_deadline_expired"
    )
    assert expired["owner"] == first["wait"]["owner"]
    assert expired["block"]["owner"] == first["wait"]["owner"]
    assert expired["expired_wait"]["deadline_at"] == expected_deadline


def test_stale_authoritative_source_fails_closed():
    scenario = properties._scenario(
        "law-infra", ci="cancelled")
    snapshot = _shared.build_snapshot(scenario, task_id="LAW-INFRA")
    run = {"run_id": "run-infra", "attempt": 0, "state_version": 1}
    decision = classify_completion(run, snapshot)
    plan = plan_effect(decision, snapshot, run)
    action = _action_for(plan)
    stale = _tick(action, snapshot["head_sha"])
    stale = FreshTick(
        observed_at=stale.observed_at,
        live_clock_at=stale.live_clock_at,
        source_observed_at={
            source: NOW - LAW_BY_ACTION[action].live_clock_bound_s - 1
            for source in LAW_BY_ACTION[action].authoritative_sources
        },
        head_sha=stale.head_sha,
    )
    try:
        normalize_fresh_tick(
            decision=decision, plan=plan, snapshot=snapshot, tick=stale)
    except ValueError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("stale source was accepted")


def test_merged_is_observation_and_never_controller_authored_done():
    scenario = properties._scenario("law-merged")
    scenario["world"]["pr_state"] = "merged"
    snapshot = _shared.build_snapshot(scenario, task_id="LAW-MERGED")
    run = {"run_id": "run-merged", "attempt": 0, "state_version": 1}
    decision = classify_completion(run, snapshot)
    plan = plan_effect(decision, snapshot, run)
    normalized = normalize_fresh_tick(
        decision=decision,
        plan=plan,
        snapshot=snapshot,
        tick=_tick(NormalizedAction.MERGED, snapshot["head_sha"]),
    )
    assert normalized["action"] == "MERGED"
    assert normalized["controller_may_author_done"] is False
    assert "no Done mutation" in normalized["effect"]


if __name__ == "__main__":
    passed = failed = 0
    for name, case in sorted(dict(globals()).items()):
        if not name.startswith("test_") or not callable(case):
            continue
        try:
            case()
        except Exception as exc:  # noqa: BLE001 - direct CI runner reports all
            failed += 1
            print(f"FAIL  {name}: {exc}")
        else:
            passed += 1
            print(f"PASS  {name}")
    print(f"\nCOORD-84 normalization law: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
