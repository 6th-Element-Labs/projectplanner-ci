"""T2 scoreboard: one row per observed tick, with human-terminal classification.

Design doc: "Landing on `human` when expected is a **pass**, not a failure
(`expected_human` vs `unexpected_human` on the scoreboard)." Everything else on
a row is a projection over a completion-tick result plus the scenario's
expectation — no new decision logic lives here.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from tests.conformance._shared import terminal_for

ROW_SCHEMA = "switchboard.completion_observe_scoreboard_row.v1"


def classify_human(actual_terminal: str, expected_terminal: str) -> Optional[str]:
    """``expected_human`` is a pass; ``unexpected_human`` pulls a person in."""
    if actual_terminal != "human":
        return None
    return "expected_human" if expected_terminal == "human" else "unexpected_human"


def build_row(
    scenario: Mapping[str, Any],
    tick_result: Mapping[str, Any],
    *,
    duration_s: float = 0.0,
    timed_out: bool = False,
) -> dict[str, Any]:
    """One scoreboard row for one scenario's observed tick."""
    scenario = dict(scenario)
    tick_result = dict(tick_result)
    decision = dict(tick_result.get("decision") or {})
    expect = dict(scenario.get("expect") or {})
    expected_terminal = str(expect.get("terminal") or "")
    actual_terminal = "timeout" if timed_out else terminal_for(tick_result)
    human_classification = classify_human(actual_terminal, expected_terminal)
    role_sequence = (
        [decision["desired_role"]] if decision.get("desired_role") else []
    )
    if timed_out:
        status = "timeout"
    elif human_classification == "unexpected_human":
        status = "fail"
    else:
        status = "pass" if actual_terminal == expected_terminal else "fail"
    return {
        "schema": ROW_SCHEMA,
        "scenario_id": scenario.get("id"),
        "expected_terminal": expected_terminal,
        "actual_terminal": actual_terminal,
        "human_classification": human_classification,
        "role_sequence": role_sequence,
        "reason_codes": (
            [decision["reason_code"]] if decision.get("reason_code") else []
        ),
        "duration_s": duration_s,
        "status": status,
    }


def summarize(rows: list[Mapping[str, Any]]) -> dict[str, int]:
    """Rows worth reading per the design doc: ``timeout`` and ``unexpected_human``."""
    summary = {"pass": 0, "fail": 0, "timeout": 0, "unexpected_human": 0}
    for row in rows:
        status = str(row.get("status") or "fail")
        summary[status] = summary.get(status, 0) + 1
        if row.get("human_classification") == "unexpected_human":
            summary["unexpected_human"] += 1
    return summary


__all__ = ["ROW_SCHEMA", "classify_human", "build_row", "summarize"]
