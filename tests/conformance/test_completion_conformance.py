#!/usr/bin/env python3
"""Tier-1 completion conformance: scenario -> production tick -> coverage."""
from __future__ import annotations

from itertools import product
import json
import os
from pathlib import Path
import sys
from typing import Any

TESTS = Path(__file__).resolve().parents[1]
ROOT = TESTS.parent
SRC = ROOT / "src"
HERE = Path(__file__).resolve().parent
for entry in (str(HERE), str(TESTS), str(SRC), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from switchboard.application import completion_driver  # noqa: E402
from switchboard.domain.completion.executor import (  # noqa: E402
    CompletionEffectAdapters,
)

import _shared  # noqa: E402
import _gold_shared  # noqa: E402


SCENARIO_DIR = HERE / "scenarios"
GOLD_DIR = SCENARIO_DIR / "gold"
AXES = _shared.AXES


def load_scenarios() -> list[dict[str, Any]]:
    return _shared.load_scenarios(SCENARIO_DIR)


def run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    ledger = _shared.EffectLedger()
    run = {
        "run_id": f"fixture-run-{scenario['id']}",
        "state_version": 1,
        "attempt": 0,
    }

    def effect(plan: dict[str, Any]) -> dict[str, Any]:
        calls.append(dict(plan))
        return {"action": "completed", "scenario_id": scenario["id"]}

    adapters = CompletionEffectAdapters(
        ensure_review_generation=effect,
        start_remediation=effect,
        mark_ready=effect,
        update_branch=effect,
        enqueue=effect,
        repair_dispatch=effect,
        fence_runner=effect,
        reconcile_provenance=effect,
    )
    snapshot = _shared.build_snapshot(scenario)
    with (
        _shared.hermetic_completion_patches(run, ledger),
        _gold_shared._human_route_db_patches("COORD-65"),
    ):
        first = completion_driver.run_completion_tick(
            "COORD-65",
            project="switchboard",
            actor="conformance",
            agent_id="conformance",
            store_mod=object(),
            hydrator=lambda *_args, **_kwargs: snapshot,
            adapters=adapters,
        )
        second = completion_driver.run_completion_tick(
            "COORD-65",
            project="switchboard",
            actor="conformance",
            agent_id="conformance",
            store_mod=object(),
            hydrator=lambda *_args, **_kwargs: snapshot,
            adapters=adapters,
        )

    expect = scenario["expect"]
    if os.environ.get("CONFORMANCE_PROVE_FAILURE") == scenario["id"]:
        expect = {**expect, "reason_code": "intentional_failure_proof"}
    actual_roles = (
        [first["decision"]["desired_role"]]
        if first["decision"].get("desired_role")
        else []
    )
    _shared.require(
        _shared.terminal_for(first) == expect["terminal"],
        f"{scenario['id']}: terminal {_shared.terminal_for(first)!r} "
        f"!= {expect['terminal']!r}",
    )
    _shared.require(
        first["decision"]["reason_code"] == expect["reason_code"],
        f"{scenario['id']}: reason {first['decision']['reason_code']!r} "
        f"!= {expect['reason_code']!r}",
    )
    _shared.require(
        actual_roles == expect["role_sequence"],
        f"{scenario['id']}: roles {actual_roles!r} != {expect['role_sequence']!r}",
    )
    _shared.require(
        first["decision"]["route"] == expect["route"],
        f"{scenario['id']}: route mismatch",
    )
    _shared.require(
        first["plan"]["effect"] == expect["effect"],
        f"{scenario['id']}: effect mismatch "
        f"(got {first['plan']['effect']!r})",
    )
    _shared.require(
        first["decision"] == second["decision"],
        f"{scenario['id']}: second tick changed the decision",
    )
    _shared.require(
        first["plan"]["idem_key"] == second["plan"]["idem_key"],
        f"{scenario['id']}: second tick changed effect identity",
    )
    # Wait/none and human escalation do not call an effect adapter. Human
    # escalation uses the transactional attention-request boundary instead.
    # Other mutating effects fire exactly once, then replay their ledger proof.
    if expect["effect"] in {"wait", "none", "attach_and_wait", "escalate_human"}:
        _shared.require(
            len(calls) == 0,
            f"{scenario['id']}: {expect['effect']} must not fire adapters "
            f"(fired {len(calls)})",
        )
    else:
        _shared.require(
            len(calls) == 1,
            f"{scenario['id']}: effect adapter fired {len(calls)} times",
        )
        _shared.require(
            second["execution"]["receipt"].get("idempotent_replay") is True,
            f"{scenario['id']}: second tick was not a verified replay",
        )
    # COORD-78/CO-21: when a scenario declares near-miss keys, the refusal must
    # still be NAMING them by the time it reaches a repair dispatch. The reason
    # code alone passed all the way through the CO-21 outage.
    near_miss = _shared.missing_artifact_near_miss_keys(first["decision"])
    expected_near_miss = expect.get("missing_artifact_near_miss_keys")
    if expected_near_miss is not None:
        _shared.require(
            near_miss == sorted(expected_near_miss),
            f"{scenario['id']}: near-miss keys {near_miss!r} != "
            f"{sorted(expected_near_miss)!r}",
        )
    result = {
        "scenario_id": scenario["id"],
        "terminal": _shared.terminal_for(first),
        "route": first["decision"]["route"],
        "effect": first["plan"]["effect"],
        "reason_code": first["decision"]["reason_code"],
        "roles": actual_roles,
        "second_tick": "idempotent_replay",
    }
    if near_miss:
        result["missing_artifact_near_miss_keys"] = near_miss
    return result


def coverage_report(scenarios: list[dict[str, Any]]) -> dict[str, int]:
    covered = {
        (
            scenario["world"]["draft"],
            scenario["world"]["ci"],
            scenario["world"]["mergeable"],
            scenario["world"]["review"],
            scenario["world"]["queue"],
            (
                scenario["world"]["runner"]["role"]
                if scenario["world"]["runner"]["live"]
                else "none"
            ),
        ): scenario["id"]
        for scenario in scenarios
    }
    total = undefined = 0
    for cell in product(*(AXES[name] for name in AXES)):
        total += 1
        scenario_id = covered.get(cell)
        status = (
            f"SCENARIO_DEFINED:{scenario_id}"
            if scenario_id
            else "NO_SCENARIO_DEFINED"
        )
        undefined += int(scenario_id is None)
        rendered = ",".join(
            f"{name}={value!r}" for name, value in zip(AXES, cell)
        )
        print(f"COVERAGE {rendered} status={status}")
    print(
        "COVERAGE SUMMARY "
        f"defined={len(covered)} undefined={undefined} total={total}"
    )
    return {"defined": len(covered), "undefined": undefined, "total": total}


def evidence_coverage_report(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    """Report the COORD-78 evidence axes one axis at a time.

    Not crossed into the PR-world product above: 810 PR cells x 375 evidence
    combinations is ~304,000 rows, and a coverage table nobody reads proves
    nothing. Per-axis is what an operator can act on — "no scenario reaches
    external_ci=green_other_head" is a to-do; a quarter-million-row table is
    not. Every value is reported against the EFFECTIVE evidence world (defaults
    applied), because that is the world the real merge gate was run against.

    Scored over the T1 seeds AND the gold catalog. The PR-world table above is
    deliberately scored over the three seeds alone — its job is to keep 807
    undefined cells visible — but an evidence axis reported as uncovered while
    a gold scenario covers it would be a false to-do, and a coverage report
    that cries wolf gets ignored.
    """
    report = _shared.evidence_coverage(scenarios + _shared.load_scenarios(GOLD_DIR))
    for axis, values in report["covered"].items():
        for value, ids in values.items():
            # Named examples, not the full list: the default evidence world is
            # shared by every PR-world scenario, so `valid`/`pass` would print
            # forty ids and bury the cells that have one or none.
            shown = sorted(ids)[:3]
            status = (
                f"SCENARIO_DEFINED:{len(ids)}:{','.join(shown)}"
                if ids
                else "NO_SCENARIO_DEFINED"
            )
            print(f"EVIDENCE_COVERAGE {axis}={value!r} status={status}")
    print(
        "EVIDENCE_COVERAGE SUMMARY "
        f"undefined={len(report['undefined'])} "
        f"cells={sum(len(v) for v in report['covered'].values())}"
    )
    return report


def main() -> int:
    passed = failed = 0
    scenarios = load_scenarios()
    for scenario in scenarios:
        try:
            result = run_scenario(scenario)
        except Exception as exc:
            failed += 1
            print(f"FAIL  {scenario['id']}: {exc}")
        else:
            passed += 1
            print("PASS  " + json.dumps(result, sort_keys=True))
    coverage = coverage_report(scenarios)
    _shared.require(
        coverage["undefined"] > 0,
        "coverage must expose undefined cells, not grade only known scenarios",
    )
    evidence = evidence_coverage_report(scenarios)
    print(
        f"\nCompletion conformance: {passed} passed, {failed} failed; "
        f"{coverage['undefined']} undefined cells; "
        f"{len(evidence['undefined'])} undefined evidence cells"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
