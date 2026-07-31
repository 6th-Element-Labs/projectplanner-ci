#!/usr/bin/env python3
"""Property invariants over every completion-conformance axis cell (COORD-79).

Gold scenarios lock spec-predicted decisions.  This suite covers the rest of
the finite state space without inventing a per-cell oracle:

* all 1,080 PR-world cells (draft × CI × mergeability × review × queue × runner);
* all 375 board-evidence cells on a clean PR world; and
* the COORD-77 convergence/no-spin reactor over every PR-world cell.

The two products deliberately remain separate.  Crossing them would repeat
the same properties 405,000 times without adding an axis interaction: evidence
is already fed through the real merge gate when each snapshot is built.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import product
import json
from pathlib import Path
import sys
from typing import Any, Iterable

TESTS = Path(__file__).resolve().parents[1]
ROOT = TESTS.parent
SRC = ROOT / "src"
HERE = Path(__file__).resolve().parent
for entry in (str(HERE), str(TESTS), str(SRC), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from switchboard.domain.completion.effects import (  # noqa: E402
    MUTATING_EFFECTS,
    plan_effect,
)
from switchboard.domain.completion.state_machine import (  # noqa: E402
    classify_completion,
)
from switchboard.domain.decisions.reason_codes import is_registered  # noqa: E402

import _shared  # noqa: E402


BASE_SCENARIO = json.loads(
    (HERE / "scenarios" / "merge_ok.json").read_text(encoding="utf-8")
)
PROCESS_FINDING_EVIDENCE = {
    "executed_test_run": "missing",
    "external_ci": "failed",
    "work_session": "missing",
    "review_verdict": "missing",
}
MAX_TICKS = 12


def _runner(role: str) -> dict[str, Any]:
    if role == "none":
        return {"live": False, "role": "none", "head": "none"}
    return {"live": True, "role": role, "head": "same"}


def _merge_state(mergeable: bool | None) -> str:
    if mergeable is True:
        return "CLEAN"
    if mergeable is False:
        return "CONFLICTING"
    return "UNKNOWN"


def _scenario(
    scenario_id: str,
    *,
    draft: bool = False,
    ci: str = "pass",
    mergeability: bool | None = True,
    review: str = "passed",
    queue: str = "none",
    runner: str = "none",
    evidence: dict[str, str] | None = None,
) -> dict[str, Any]:
    scenario = deepcopy(BASE_SCENARIO)
    scenario["id"] = scenario_id
    world = scenario["world"]
    world.update({
        "draft": draft,
        "ci": ci,
        "ci_attribution": "product",
        "mergeable": mergeability,
        "merge_state_status": _merge_state(mergeability),
        "review": review,
        "queue": queue,
        "runner": _runner(runner),
    })
    if evidence is None:
        world.pop("evidence", None)
    else:
        world["evidence"] = dict(evidence)
    return scenario


def pr_worlds() -> Iterable[dict[str, Any]]:
    for index, values in enumerate(_shared.coverage_cells()):
        cell = dict(zip(_shared.AXES, values, strict=True))
        yield _scenario(f"property-pr-{index}", **cell)


def evidence_worlds() -> Iterable[dict[str, Any]]:
    names = tuple(_shared.EVIDENCE_AXES)
    values = (_shared.EVIDENCE_AXES[name] for name in names)
    for index, cell in enumerate(product(*values)):
        evidence = dict(zip(names, cell, strict=True))
        yield _scenario(f"property-evidence-{index}", evidence=evidence)


def _decision_and_plan(
    scenario: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    snapshot = _shared.build_snapshot(
        scenario, task_id=f"PROPERTY-{scenario['id']}"
    )
    run = {
        "run_id": f"run-{scenario['id']}",
        "attempt": 0,
        "state_version": 1,
    }
    first = classify_completion(run, snapshot)
    second = classify_completion(deepcopy(run), deepcopy(snapshot))
    if first != second:
        raise AssertionError(
            f"{scenario['id']}: classifier is impure: {first!r} != {second!r}"
        )
    first_plan = plan_effect(first, snapshot, run)
    second_plan = plan_effect(
        deepcopy(second), deepcopy(snapshot), deepcopy(run)
    )
    if first_plan != second_plan:
        raise AssertionError(
            f"{scenario['id']}: planner is impure: "
            f"{first_plan!r} != {second_plan!r}"
        )
    return snapshot, first, first_plan


def _assert_global_properties(
    scenario: dict[str, Any],
    snapshot: dict[str, Any],
    decision: dict[str, Any],
    plan: dict[str, Any],
) -> None:
    scenario_id = scenario["id"]
    reason = str(decision.get("reason_code") or "").strip()

    if (
        decision.get("board_projection") == "Blocked"
        or decision.get("route") == "human"
    ):
        if not reason or reason.lower() == "unknown" or not is_registered(reason):
            raise AssertionError(
                f"{scenario_id}: blocked terminal has unactionable reason {reason!r}"
            )

    desired_role = str(decision.get("desired_role") or "")
    runner = snapshot.get("runner") or {}
    if (
        runner.get("live")
        and runner.get("role") == desired_role
        and runner.get("head_sha") == snapshot.get("head_sha")
        and plan.get("fence_required")
    ):
        raise AssertionError(
            f"{scenario_id}: matching live {desired_role!r} exact-head runner fenced"
        )

    evidence = _shared.effective_evidence(scenario["world"])
    if (
        evidence["external_ci"] == "green_exact_head"
        and evidence["executed_test_run"] in {"missing", "near_miss_key"}
        and plan.get("effect") == "repair_dispatch"
        and reason == "missing_executed_test_run"
    ):
        raise AssertionError(
            f"{scenario_id}: planned repair for test evidence derivable from "
            "the green exact-head CI receipt"
        )


def _assert_red_product_ci_precedence(scenario: dict[str, Any]) -> None:
    """COORD-49: product-red CI beats simultaneous process/evidence findings."""
    process_world = deepcopy(scenario)
    process_world["id"] += "-process-findings"
    process_world["world"]["ci"] = "fail"
    process_world["world"]["ci_attribution"] = "product"
    process_world["world"]["evidence"] = dict(PROCESS_FINDING_EVIDENCE)
    # Keep only the two facts whose precedence this property compares.  Review
    # judgment, merge conflicts, queue attribution, and draft state have their
    # own explicitly stronger or orthogonal contracts.
    process_world["world"].update({
        "draft": False,
        "review": "passed",
        "mergeable": True,
        "merge_state_status": "CLEAN",
        "queue": "none",
        # Mission Bot hangs up while any live runner owns capacity. This
        # property compares CI vs process findings only — clear the runner.
        "runner": {"live": False, "role": "none", "head": "none"},
    })
    _, decision, _ = _decision_and_plan(process_world)
    if (
        decision.get("route") != "remediation"
        or decision.get("reason_code") != "required_exact_head_ci_failed"
    ):
        raise AssertionError(
            f"{process_world['id']}: process finding outranked product-red CI: "
            f"{decision!r}"
        )


def _react(world: dict[str, Any], effect: str) -> None:
    if effect == "mark_ready":
        world["draft"] = False
    elif effect == "update_branch":
        world["merge_state_status"] = "CLEAN"
    elif effect == "enqueue":
        world["queue"] = "queued"
    elif effect == "ensure_review_generation":
        world["runner"] = {
            "live": True, "role": "review_merge", "head": "same",
        }
    elif effect == "start_remediation":
        world["runner"] = {
            "live": True, "role": "remediation", "head": "same",
        }
    elif effect == "fence_runner":
        world["runner"] = {"live": False, "role": "none", "head": "none"}


def _world_key(world: dict[str, Any]) -> str:
    return json.dumps(world, sort_keys=True, separators=(",", ":"))


def _assert_no_spin(
    scenario: dict[str, Any],
    snapshot_cache: dict[str, dict[str, Any]],
) -> None:
    """Run COORD-77's convergence invariant over one synthesized world.

    COORD-77's own merge-gated test proves persistence/ledger integration.  The
    property sweep intentionally drives the pure classifier/planner reactor so
    all 1,080 cells stay cheap enough for every PR.
    """
    shaped = deepcopy(scenario)
    run: dict[str, Any] = {
        "run_id": f"convergence-{scenario['id']}",
        "attempt": 0,
        "state_version": 1,
        "evidence_refs": {},
    }
    executed: set[tuple[str, str, str]] = set()
    stable_replays = 0

    for _tick in range(1, MAX_TICKS + 1):
        cache_key = _world_key(shaped["world"])
        snapshot = deepcopy(snapshot_cache.get(cache_key))
        if not snapshot:
            snapshot = _shared.build_snapshot(
                shaped, task_id=f"CONVERGENCE-{scenario['id']}"
            )
            snapshot_cache[cache_key] = deepcopy(snapshot)
        decision = classify_completion(run, snapshot)
        plan = plan_effect(decision, snapshot, run)
        route = str(decision.get("route") or "")
        reason = str(decision.get("reason_code") or "")
        effect = str(plan.get("effect") or "")
        world_before = _world_key(shaped["world"])

        if route == "human":
            if not is_registered(reason):
                raise AssertionError(
                    f"{scenario['id']}: no-spin human terminal has "
                    f"unregistered reason {reason!r}"
                )
            return
        if effect not in MUTATING_EFFECTS:
            return

        key = (effect, reason, world_before)
        if key in executed:
            # The external-effect ledger turns this into a verified replay, not
            # a second execution.  Feed that pressure into the same convergence
            # ladder COORD-77 tests through storage.
            stable_replays += 1
            if stable_replays >= 6:
                raise AssertionError(
                    f"{scenario['id']}: stable mutating replay livelocked: "
                    f"effect={effect!r} reason={reason!r}"
                )
        else:
            stable_replays = 0
            executed.add(key)
            _react(shaped["world"], effect)
        world_after = _world_key(shaped["world"])

        convergence = {
            "head_sha": _shared.HEAD,
            "churn": 0,
            "stable_replays": 0,
            "reconcile_tried": effect == "reconcile_provenance",
        }
        if world_after == world_before:
            previous = (
                run.get("evidence_refs", {}).get("convergence", {})
            )
            convergence["churn"] = int(previous.get("churn") or 0)
            convergence["stable_replays"] = (
                int(previous.get("stable_replays") or 0) + 1
            )
            convergence["reconcile_tried"] = bool(
                previous.get("reconcile_tried")
                or effect == "reconcile_provenance"
            )
        else:
            run["state_version"] += 1
            run["attempt"] += 1
        run["evidence_refs"] = {"convergence": convergence}

    raise AssertionError(
        f"{scenario['id']}: no terminal within {MAX_TICKS} ticks"
    )


def main() -> int:
    pr_cells = list(pr_worlds())
    evidence_cells = list(evidence_worlds())
    failures: list[str] = []
    snapshot_cache: dict[str, dict[str, Any]] = {}

    for scenario in [*pr_cells, *evidence_cells]:
        try:
            snapshot, decision, plan = _decision_and_plan(scenario)
            snapshot_cache[_world_key(scenario["world"])] = deepcopy(snapshot)
            _assert_global_properties(scenario, snapshot, decision, plan)
        except Exception as exc:  # noqa: BLE001 - report every failing cell
            failures.append(str(exc))

    # The collision constructor overwrites every axis except runner state.
    # Exercise each runner value once instead of repeating an identical world.
    precedence_cells = {
        scenario["world"]["runner"]["role"]: scenario
        for scenario in pr_cells
    }
    for scenario in precedence_cells.values():
        try:
            _assert_red_product_ci_precedence(scenario)
        except Exception as exc:  # noqa: BLE001 - report every failing cell
            failures.append(str(exc))

    for scenario in pr_cells:
        try:
            _assert_no_spin(scenario, snapshot_cache)
        except Exception as exc:  # noqa: BLE001 - report every failing cell
            failures.append(str(exc))

    if failures:
        for failure in failures:
            print(f"FAIL  {failure}")
        print(
            f"\nProperty invariants: {len(failures)} failure(s); "
            f"{len(pr_cells)} PR cells, {len(evidence_cells)} evidence cells"
        )
        return 1

    print(
        "Property invariants: "
        f"{len(pr_cells)} PR cells + {len(evidence_cells)} evidence cells; "
        f"no-spin across all {len(pr_cells)} PR cells"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
