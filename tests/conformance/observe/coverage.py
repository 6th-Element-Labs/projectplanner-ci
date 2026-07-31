"""Axis coverage for the observe scoreboard — the T1 idea, thinly reused.

Same six axes T1 grades scenario coverage against
(``tests.conformance._shared.AXES``), so "which cells are proven" means the
same thing at both tiers. The difference is *what counts as proof*: T1 grades
fixture-tick route/effect; T2 grades an observed terminal from a real-sandbox
(or fixture-mode) tick.
"""
from __future__ import annotations

from typing import Any, Mapping

from tests.conformance._shared import AXES, coverage_cells


def scenario_cell(scenario: Mapping[str, Any]) -> tuple[Any, ...]:
    world = scenario["world"]
    return (
        world["draft"],
        world["ci"],
        world["mergeable"],
        world["review"],
        world["queue"],
        world["runner"]["role"] if world["runner"]["live"] else "none",
    )


def coverage_report(scenarios: list[Mapping[str, Any]]) -> dict[str, Any]:
    covered = {scenario_cell(scenario): scenario["id"] for scenario in scenarios}
    total = undefined = 0
    rows: list[dict[str, Any]] = []
    for cell in coverage_cells():
        total += 1
        scenario_id = covered.get(cell)
        undefined += int(scenario_id is None)
        rows.append({
            "cell": dict(zip(AXES, cell)),
            "scenario_id": scenario_id,
            "status": "SCENARIO_DEFINED" if scenario_id else "NO_SCENARIO_DEFINED",
        })
    return {
        "defined": len(covered),
        "undefined": undefined,
        "total": total,
        "rows": rows,
    }


def undefined_cells(scenarios: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        row["cell"] for row in coverage_report(scenarios)["rows"]
        if row["scenario_id"] is None
    ]


__all__ = ["scenario_cell", "coverage_report", "undefined_cells"]
