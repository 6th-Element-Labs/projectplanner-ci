#!/usr/bin/env python3
"""Gold decision-quality gate: scenario -> production tick, graded vs. spec.

    python3.11 tests/conformance/gold_decisions.py

Every scenario under ``scenarios/gold/`` was promoted by
``scripts/completion_conformance/build_gold_catalog.py`` *only* after its
route was predicted from ``docs/AUTOPILOT-COMPLETION-STATE-MACHINE.md``
before the tick ran (see ``tests/conformance/GOLD_COVERAGE.md``). This suite
re-proves the same tick still produces that locked decision.

Deliberately separate from ``test_completion_conformance.py`` (T1 seed /
axis-coverage): a decision-quality regression here must never destabilize
T1's coverage gate, and vice versa. CI discovers
``test_gold_decisions.py``, which delegates here.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

TESTS = Path(__file__).resolve().parents[1]
ROOT = TESTS.parent
SRC = ROOT / "src"
HERE = Path(__file__).resolve().parent
for entry in (str(HERE), str(TESTS), str(SRC), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import _gold_shared  # noqa: E402
import _shared  # noqa: E402


GOLD_DIR = HERE / "scenarios" / "gold"


def load_gold_scenarios() -> list[dict[str, Any]]:
    return _shared.load_scenarios(GOLD_DIR)


def main() -> int:
    scenarios = load_gold_scenarios()
    passed = 0
    spec_mismatches: list[dict[str, str]] = []

    for scenario in scenarios:
        try:
            result = _gold_shared.assert_gold_scenario(scenario)
        except Exception as exc:  # noqa: BLE001 - collect every failure, don't stop
            spec_mismatches.append({
                "scenario_id": scenario["id"],
                "spec_route": scenario["expect"]["route"],
                "error": str(exc),
            })
            print(f"FAIL  {scenario['id']}: {exc}")
        else:
            passed += 1
            print("PASS  " + json.dumps(result, sort_keys=True))

    failed = len(spec_mismatches)
    print("\nSCOREBOARD " + json.dumps(
        {"gold_total": len(scenarios), "passed": passed, "failed": failed},
        sort_keys=True,
    ))

    if spec_mismatches:
        print(
            "\nSPEC_MISMATCH (a previously spec-verified gold scenario no "
            "longer matches production behavior -- this is a routing "
            "regression, not a harness flake):"
        )
        for row in spec_mismatches:
            print(
                f"  {row['scenario_id']}: expected route "
                f"{row['spec_route']!r}; {row['error']}"
            )

    print(
        f"\nCompletion gold decisions: {passed} passed, {failed} failed "
        f"(of {len(scenarios)} gold scenarios)"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
