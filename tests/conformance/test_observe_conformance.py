#!/usr/bin/env python3
"""Tier-2 completion conformance: observe cut proves decisions, spends no capacity.

Runs standalone via ``python tests/conformance/test_observe_conformance.py``
(BUG-181: some CI paths only execute test files directly, not through
pytest), and also under pytest. Both must produce a non-zero exit on failure;
``unittest.main()`` (invoked at module bottom) already guarantees that.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent
ROOT = TESTS.parent
SRC = ROOT / "src"
for _entry in (str(ROOT), str(TESTS), str(SRC)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from tests.conformance import _shared  # noqa: E402
from tests.conformance.observe.adapters import (  # noqa: E402
    EFFECT_PORTS,
    ObserveEffectAdapters,
)
from tests.conformance.observe.assignment_preview import (  # noqa: E402
    preview_execution_assignment,
)
from tests.conformance.observe.runner import (  # noqa: E402
    LIVE_ENV_VAR,
    run_observe_tick,
    run_observe_tick_fixture,
)
from tests.conformance.observe.scoreboard import (  # noqa: E402
    build_row,
    classify_human,
)


SCENARIO_DIR = HERE / "scenarios"


class ObserveAdaptersCaptureOnly(unittest.TestCase):
    """Observe ports capture the plan and never invoke a real adapter."""

    def test_start_remediation_and_enqueue_are_captured_not_invoked(self):
        observe = ObserveEffectAdapters(run_id="test-run")
        adapters = observe.as_completion_adapters()
        with (
            patch(
                "switchboard.application.commands.task_execution.start_task",
            ) as start_task,
            patch(
                "switchboard.storage.repositories.provenance._github_pr",
            ) as github_pr,
        ):
            remediation_receipt = adapters.start_remediation({
                "task_id": "COORD-65", "effect": "start_remediation",
                "route": "remediation", "role": "remediation",
                "head_sha": "c" * 40, "reason_code": "required_exact_head_ci_failed",
                "idem_key": "k1",
            })
            enqueue_receipt = adapters.enqueue({
                "task_id": "COORD-65", "effect": "enqueue", "route": "review_merge",
                "pr_number": 650, "idem_key": "k2",
            })
            start_task.assert_not_called()
            github_pr.assert_not_called()
        self.assertEqual(remediation_receipt["action"], "observe_cut")
        self.assertTrue(remediation_receipt["observed"])
        self.assertEqual(remediation_receipt["effect"], "start_remediation")
        self.assertEqual(enqueue_receipt["action"], "observe_cut")
        self.assertTrue(enqueue_receipt["observed"])
        self.assertEqual(
            [row["effect"] for row in observe.receipts],
            ["start_remediation", "enqueue"],
        )

    def test_every_effect_port_is_bound_to_a_capture(self):
        adapters = ObserveEffectAdapters().as_completion_adapters()
        for name in EFFECT_PORTS:
            self.assertIsNotNone(
                adapters.for_effect(name), f"{name} has no observe-cut port bound",
            )


class ScoreboardHumanClassification(unittest.TestCase):
    """expect.terminal == human is a pass, not a failure, when it lands there."""

    @staticmethod
    def _human_tick() -> dict:
        return {
            "decision": {
                "route": "human", "board_projection": "Blocked",
                "reason_code": "acceptance_findings_escalated", "desired_role": "",
            },
            "plan": {"effect": "escalate_human"},
        }

    def test_expected_human_is_pass(self):
        self.assertEqual(classify_human("human", "human"), "expected_human")
        row = build_row(
            {"id": "human_seed", "expect": {"terminal": "human"}},
            self._human_tick(),
        )
        self.assertEqual(row["human_classification"], "expected_human")
        self.assertEqual(row["status"], "pass")

    def test_unexpected_human_is_fail(self):
        self.assertEqual(classify_human("human", "merged"), "unexpected_human")
        row = build_row(
            {"id": "should_have_merged", "expect": {"terminal": "merged"}},
            self._human_tick(),
        )
        self.assertEqual(row["human_classification"], "unexpected_human")
        self.assertEqual(row["status"], "fail")

    def test_non_human_terminal_has_no_human_classification(self):
        self.assertIsNone(classify_human("merged", "merged"))


class ObserveMatchesT1SeedsWithoutSpendingCapacity(unittest.TestCase):
    """Integration-style: the 3 T1 seed scenarios, run through the observe cut.

    Same expects T1 asserts (route/effect/terminal), but proves the harness
    never calls the real ``start_task`` port while doing it.
    """

    def test_seed_scenarios_route_match_t1_and_never_start_task(self):
        scenarios = _shared.load_scenarios(SCENARIO_DIR)
        self.assertTrue(scenarios, "expected the 3 T1 seed scenarios to be present")
        with patch(
            "switchboard.application.commands.task_execution.start_task",
        ) as start_task:
            for scenario in scenarios:
                outcome = run_observe_tick_fixture(scenario)
                first = outcome["ticks"][0]
                expect = scenario["expect"]
                scenario_id = scenario["id"]
                self.assertEqual(
                    _shared.terminal_for(first), expect["terminal"], scenario_id,
                )
                self.assertEqual(
                    first["decision"]["route"], expect["route"], scenario_id,
                )
                self.assertEqual(
                    first["plan"]["effect"], expect["effect"], scenario_id,
                )
                # Wait/none are non-mutating: the observe cut may capture no
                # adapter receipt. Mutating effects must still prove a receipt.
                if expect["effect"] in {"wait", "none"}:
                    self.assertEqual(
                        outcome["receipts"],
                        [],
                        f"{scenario_id}: wait/none must not fire adapters",
                    )
                else:
                    self.assertTrue(
                        outcome["receipts"],
                        f"{scenario_id}: observe cut captured no effect",
                    )
                for receipt in outcome["receipts"]:
                    self.assertEqual(receipt["action"], "observe_cut")
                    self.assertTrue(receipt["observed"])
                    self.assertEqual(receipt["run_id"], outcome["run_id"])
            start_task.assert_not_called()


class RunObserveTickDispatch(unittest.TestCase):
    def setUp(self):
        self._prior_flag = os.environ.pop(LIVE_ENV_VAR, None)

    def tearDown(self):
        if self._prior_flag is None:
            os.environ.pop(LIVE_ENV_VAR, None)
        else:
            os.environ[LIVE_ENV_VAR] = self._prior_flag

    def test_requires_exactly_one_of_scenario_or_task_id(self):
        with self.assertRaises(ValueError):
            run_observe_tick()
        with self.assertRaises(ValueError):
            run_observe_tick(scenario={"id": "x"}, task_id="COORD-65")

    def test_live_mode_refuses_without_env_flag(self):
        with self.assertRaises(RuntimeError):
            run_observe_tick(task_id="COORD-65")

    def test_live_mode_refuses_live_product_project_even_with_flag(self):
        os.environ[LIVE_ENV_VAR] = "1"
        with self.assertRaises(RuntimeError):
            run_observe_tick(task_id="COORD-65", project="switchboard")


class AssignmentPreview(unittest.TestCase):
    def test_launch_effect_preview_shape(self):
        preview = preview_execution_assignment({
            "effect": "start_remediation", "task_id": "COORD-65",
            "role": "remediation", "head_sha": "c" * 40,
            "reason_code": "required_exact_head_ci_failed", "route": "remediation",
        })
        self.assertTrue(preview["launches_runner"])
        self.assertEqual(preview["role"], "remediation")
        self.assertEqual(preview["source_sha"], "c" * 40)

    def test_non_launch_effect_preview_shape(self):
        preview = preview_execution_assignment({
            "effect": "enqueue", "route": "review_merge",
        })
        self.assertFalse(preview["launches_runner"])


if __name__ == "__main__":
    unittest.main()
