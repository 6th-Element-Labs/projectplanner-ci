#!/usr/bin/env python3
"""ARCH-MS-128: Mission Bot v4 canonical release-decision contract."""
from __future__ import annotations

import unittest

from path_setup import ROOT as _ROOT  # noqa: F401
from switchboard.contracts.reviews import ReviewFinding
from switchboard.domain.mission_bot_v4 import decide_mission_transition
from switchboard.domain.mission_bot_v4.review_routing import route_review_findings


def _finding(*, category: str, finding_class: str) -> dict[str, object]:
    return ReviewFinding.model_validate({
        "schema": "switchboard.review_finding.v1",
        "id": f"ARCH-MS-128-{category}",
        "location": "src/switchboard/example.py:10",
        "category": category,
        "severity": "high",
        "invariant_violated": "The exact-head gate is red.",
        "repair_requirement": "Repair the exact-head finding and rerun the gate.",
        "class": finding_class,
        "state": "open",
    }).model_dump(mode="json", by_alias=True)


def _context(**overrides: object) -> dict[str, object]:
    return {
        "scope_active": True,
        "dependencies_satisfied": True,
        "requested_role": "remediation",
        "mission_state": "ACTIVE",
        "runner_live": False,
        "capacity_attempt_pending": False,
        "handled_through": 1,
        "latest_sequence": 1,
        **overrides,
    }


class MissionBotV4ReleaseContractTest(unittest.TestCase):
    def test_machine_findings_continue_at_any_round(self) -> None:
        for category in ("ci", "security", "permission", "conflict", "correctness"):
            finding = _finding(category=category, finding_class="auto")
            for round_no in (4, 10, 100):
                decision = route_review_findings([finding], round_no=round_no)
                self.assertEqual("continue", decision["result"])
                self.assertEqual("remediation", decision["requested_role"])
                self.assertEqual(round_no, decision["round_no"])
                self.assertFalse(decision["human_required"])

    def test_only_explicit_escalate_finding_is_human(self) -> None:
        finding = _finding(category="human_decision", finding_class="escalate")
        self.assertEqual(
            "human", route_review_findings([finding], round_no=100)["result"],
        )

    def test_capacity_pending_is_wait(self) -> None:
        decision = decide_mission_transition(_context(capacity_attempt_pending=True))
        self.assertEqual("wait", decision["result"])
        self.assertEqual("capacity_attempt_pending", decision["reason"])

    def test_later_unhandled_event_is_continue(self) -> None:
        decision = decide_mission_transition(_context(latest_sequence=2))
        self.assertEqual("continue", decision["result"])
        self.assertEqual("unhandled_event", decision["reason"])

    def test_authenticated_agent_requires_human_is_human(self) -> None:
        decision = decide_mission_transition(_context(human_request={
            "schema": "switchboard.work_session_human_blocker.v1",
            "source_tool": "agent_requires_human",
            "binding": "registered_agent",
            "agent_id": "agent-arch-ms-128",
            "provenance_stamp": "switchboard.resolve_write_actor.v1",
        }))
        self.assertEqual("human", decision["result"])

    def test_board_blocked_is_ignored(self) -> None:
        decision = decide_mission_transition(_context(
            board_status="Blocked", latest_sequence=2,
        ))
        self.assertEqual("continue", decision["result"])

    def test_canonical_terminal_provenance_is_done(self) -> None:
        decision = decide_mission_transition(_context(terminal_provenance=True))
        self.assertEqual("done", decision["result"])

    def test_active_mission_missing_input_is_wait_with_release_health(self) -> None:
        decision = decide_mission_transition(_context(
            handled_through=1, latest_sequence=1,
        ))
        self.assertEqual("wait", decision["result"])
        self.assertTrue(decision["release_blocked"])


if __name__ == "__main__":
    unittest.main()
