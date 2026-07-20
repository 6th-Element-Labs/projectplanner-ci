"""SIMPLIFY-3: merge provenance alone must not Done a multi-command spec.

Regression: COORD-44 / PR #656 was marked Done when only start_task shipped.
"""
from __future__ import annotations

import unittest

from path_setup import ROOT  # noqa: F401

from switchboard.domain.provenance import semantic


COORD44_SHAPE = {
    "task_id": "COORD-44",
    "status": "In Review",
    "description": (
        "session_profile:code_strict\n"
        "acceptance_required: true\n"
        "required_commands: start_task, stop_task, retry_task, "
        "watch_task, inject_task, doctor_task, reopen_task\n"
        "\nShip the unified Start/Retry command surface.\n"
    ),
}


class MultiCommandDoneGateTest(unittest.TestCase):
    def test_acceptance_required_blocks_merge_only_done(self):
        gate = semantic.merge_done_gate(COORD44_SHAPE, evidence={
            "merged_sha": "abc123",
            "pr_number": 656,
        })
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["status"], "merged_evidence_only")
        self.assertIn("acceptance_required", gate["reasons"])

    def test_acceptance_passed_allows_done(self):
        gate = semantic.merge_done_gate(COORD44_SHAPE, evidence={
            "merged_sha": "abc123",
            "acceptance_passed": True,
        })
        self.assertTrue(gate["ok"])
        self.assertEqual(gate["status"], "passed")

    def test_ordinary_task_still_dones_on_merge(self):
        gate = semantic.merge_done_gate({
            "task_id": "SEG-1",
            "description": "Ordinary single-outcome task.",
        }, evidence={"merged_sha": "def456"})
        self.assertTrue(gate["ok"])


if __name__ == "__main__":
    unittest.main()
