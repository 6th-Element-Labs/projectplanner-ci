#!/usr/bin/env python3
"""COORD-117: v4 cannot silently wait when a material event is missing."""
from __future__ import annotations

import unittest

from path_setup import ROOT as _ROOT  # noqa: F401
from switchboard.application.mission_bot_v4 import (
    ScopedMissionWorkerPorts,
    assess_stuck_mission_invariant,
    tick_scoped_mission,
)
from switchboard.domain.mission_bot_v4 import (
    active_mission_failure,
    decide_mission_transition,
)


PROJECT = "switchboard"
TASK = "COORD-117"


def context(**updates):
    value = {
        "scope_active": True,
        "terminal_provenance": False,
        "dependencies_satisfied": True,
        "project": PROJECT,
        "task_id": TASK,
        "mission_state": "ACTIVE",
        "runner_live": False,
        "requested_role": "implementation",
        "handled_through": 4,
        "latest_sequence": 4,
    }
    value.update(updates)
    return value


class FakeJournal:
    def __init__(self, items):
        self.items = items

    def active_task_ids(self, *, project):
        assert project == PROJECT
        return sorted(self.items)

    def get_item(self, task_id, *, project):
        assert project == PROJECT
        item = self.items.get(task_id)
        return dict(item) if item is not None else None


class StuckMissionInvariantTest(unittest.TestCase):
    def test_active_blind_wait_is_a_typed_release_blocker(self):
        failure = active_mission_failure(context())
        self.assertIsNotNone(failure)
        self.assertEqual("switchboard.mission_stuck_invariant.v1", failure["schema"])
        self.assertEqual("missing_mission_event", failure["reason"])
        self.assertEqual("missing_data", failure["failure_class"])
        self.assertTrue(failure["release_blocked"])
        self.assertTrue(failure["missing_producer"])
        self.assertEqual({
            "project": PROJECT,
            "task_id": TASK,
            "mission_state": "ACTIVE",
            "requested_role": "implementation",
            "handled_through": 4,
            "latest_sequence": 4,
            "runner_live": False,
            "runner_liveness_source": "runner_sessions",
            "human_parked": False,
            "terminal_provenance": False,
        }, failure["evidence"])

        decision = decide_mission_transition(context())
        self.assertEqual("block_release", decision["action"])
        self.assertEqual("missing_mission_event", decision["reason"])

    def test_cursor_corruption_is_named_instead_of_misreported(self):
        failure = active_mission_failure(context(handled_through=5, latest_sequence=4))
        self.assertEqual("mission_cursor_ahead", failure["reason"])
        self.assertEqual("invalid_input", failure["failure_class"])
        self.assertFalse(failure["missing_producer"])

    def test_truthful_runner_human_wait_and_unhandled_event_are_not_blocked(self):
        self.assertIsNone(active_mission_failure(context(runner_live=True)))
        self.assertIsNone(active_mission_failure(context(mission_state="HUMAN")))
        self.assertIsNone(active_mission_failure(context(mission_state="WAITING")))
        self.assertIsNone(active_mission_failure(context(
            handled_through=4, latest_sequence=5,
        )))
        self.assertEqual(
            "runner_live",
            decide_mission_transition(context(runner_live=True))["reason"],
        )
        self.assertEqual(
            "authenticated_agent_request",
            decide_mission_transition(context(mission_state="HUMAN"))["reason"],
        )
        self.assertEqual(
            "start_task",
            decide_mission_transition(context(latest_sequence=5))["action"],
        )

    def test_worker_blocks_without_mutation_or_fallback_start(self):
        class Journal:
            @staticmethod
            def get_item(_task_id, *, project):
                assert project == PROJECT
                return {
                    "state": "ACTIVE",
                    "requested_role": "implementation",
                    "handled_through": 2,
                    "latest_sequence": 2,
                }

        result = tick_scoped_mission(
            TASK,
            project=PROJECT,
            scope_authority={"generation": 1},
            actor="test",
            ports=ScopedMissionWorkerPorts(
                validate_scope=lambda *_args, **_kwargs: {"allowed": True},
                get_task=lambda *_args, **_kwargs: {
                    "dependency_state": {"satisfied": True},
                },
                has_live_execution=lambda *_args, **_kwargs: False,
                start_task=lambda *_args, **_kwargs: self.fail(
                    "stuck invariant must not call start_task"
                ),
                journal=Journal(),
            ),
        )
        self.assertEqual("block_release", result["action"])
        self.assertEqual(0, result["mutations"])
        self.assertTrue(result["release_blocked"])
        self.assertEqual("runner_sessions", result["failure"]["evidence"][
            "runner_liveness_source"
        ])

    def test_release_scan_blocks_only_the_exact_stuck_active_mission(self):
        journal = FakeJournal({
            "QA-1": {
                "state": "ACTIVE", "requested_role": "implementation",
                "handled_through": 1, "latest_sequence": 1,
            },
            "QA-2": {
                "state": "ACTIVE", "requested_role": "implementation",
                "handled_through": 1, "latest_sequence": 2,
            },
            "QA-3": {
                "state": "ACTIVE", "requested_role": "review_merge",
                "handled_through": 2, "latest_sequence": 2,
            },
            "QA-4": {
                "state": "HUMAN", "requested_role": "remediation",
                "handled_through": 3, "latest_sequence": 3,
            },
            "QA-5": {
                "state": "WAITING", "requested_role": "review_merge",
                "handled_through": 4, "latest_sequence": 4,
            },
        })
        observed = []

        def has_live(task_id, *, project):
            observed.append((task_id, project))
            return task_id == "QA-3"

        gate = assess_stuck_mission_invariant(
            project=PROJECT,
            journal=journal,
            has_live_execution=has_live,
        )
        self.assertFalse(gate["passed"])
        self.assertTrue(gate["release_blocked"])
        self.assertFalse(gate["cutover_authorized"])
        self.assertEqual(5, gate["checked_task_count"])
        self.assertEqual(1, gate["blocker_count"])
        self.assertEqual("QA-1", gate["blockers"][0]["evidence"]["task_id"])
        self.assertEqual(
            [(task_id, PROJECT) for task_id in sorted(journal.items)],
            observed,
        )

    def test_broken_capacity_read_fails_loudly(self):
        journal = FakeJournal({
            TASK: {
                "state": "ACTIVE", "requested_role": "implementation",
                "handled_through": 1, "latest_sequence": 1,
            },
        })

        def broken(_task_id, *, project):
            raise RuntimeError(f"runner registry unavailable for {project}")

        with self.assertRaisesRegex(RuntimeError, "runner registry unavailable"):
            assess_stuck_mission_invariant(
                project=PROJECT,
                journal=journal,
                has_live_execution=broken,
            )


if __name__ == "__main__":
    unittest.main()
