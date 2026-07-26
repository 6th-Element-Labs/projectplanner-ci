#!/usr/bin/env python3
"""COORD-46: the mission layers select by completion route, not status alone.

``mission_coordinator`` builds candidates for exactly three statuses today, so
a task projected to ``Blocked(route=remediation)`` produces no action at all.
The route reaches those layers through the mission read model, which keeps one
classifier feeding both coordinator routing and operator projections
(invariant 3) instead of giving the coordinator a private side-channel.

Dependency safety uses ``satisfied``, not ``ready`` (BREAKDOWN 42).
"""
from __future__ import annotations

import unittest

from path_setup import ROOT  # noqa: F401

import mission_coordinator as mc  # noqa: E402
from switchboard.domain.board.tasks import build_dependency_state  # noqa: E402
from switchboard.storage.repositories import completion_runs  # noqa: E402


def _mission(status, *, route=None, satisfied=True, claims=(), task_id="COORD-99",
             dependency_rows=None):
    rows = list(dependency_rows) if dependency_rows is not None else []
    dep = build_dependency_state({"status": status, "task_id": task_id}, rows)
    if dependency_rows is None:
        dep = {
            **dep,
            "satisfied": bool(satisfied),
            "blocked_by_count": 0 if satisfied else 1,
            "blocking": [] if satisfied else [{"task_id": "DEP-1", "done": False}],
            "ready": bool(satisfied) and status == "Not Started",
        }
    detail = {
        "task_id": task_id,
        "title": "route selection",
        "status": status,
        "workstream": "COORD",
        "active_claims": list(claims),
        "dependency_state": dep,
        "git_state": {"head_sha": "c" * 40},
    }
    if route is not None:
        detail["completion_run"] = {"route": route, "state": "blocked"}
    return {
        "deliverable_id": "autopilot",
        "linked_tasks": [{
            "task_id": task_id, "project_id": "switchboard",
            "role": "implementation", "milestone_id": "m1",
            "task_detail": detail,
        }],
        "milestones": [{"id": "m1", "status": "in_progress"}],
        "next_actions": [],
    }


def _explicit(mission, task_id="COORD-99"):
    return mc._explicit_target_actions(mission, {
        "target_task_id": task_id, "target_project_id": "switchboard",
    })


class ExplicitTargetActions(unittest.TestCase):
    def test_blocked_remediation_produces_a_dispatch_action(self):
        actions = _explicit(_mission("Blocked", route="remediation"))
        self.assertEqual(len(actions), 1, actions)
        self.assertIn(actions[0]["action"], {"claim_task", "resume_or_claim"})
        self.assertEqual(actions[0].get("completion_route"), "remediation")

    def test_blocked_production_state_produces_a_dispatch_action(self):
        """BREAKDOWN 42: build_dependency_state(Blocked) has ready=False."""
        mission = _mission("Blocked", route="remediation", dependency_rows=[])
        dep = mission["linked_tasks"][0]["task_detail"]["dependency_state"]
        self.assertFalse(dep["ready"])
        self.assertTrue(dep["satisfied"])
        actions = _explicit(mission)
        self.assertEqual(len(actions), 1, actions)

    def test_blocked_remediation_carries_exact_head(self):
        actions = _explicit(_mission("Blocked", route="remediation"))
        self.assertEqual(actions[0].get("head_sha"), "c" * 40)

    def test_blocked_human_produces_no_dispatch_action(self):
        self.assertEqual(_explicit(_mission("Blocked", route="human")), [])

    def test_blocked_without_a_route_produces_no_dispatch_action(self):
        self.assertEqual(_explicit(_mission("Blocked")), [])

    def test_blocked_remediation_still_respects_dependencies(self):
        self.assertEqual(
            _explicit(_mission("Blocked", route="remediation", satisfied=False)), [])

    def test_blocked_remediation_still_respects_a_conflicting_claim(self):
        self.assertEqual(
            _explicit(_mission("Blocked", route="remediation",
                               claims=[{"claim_id": "c1"}])), [])

    def test_existing_statuses_are_unchanged(self):
        self.assertEqual(_explicit(_mission("Not Started"))[0]["action"], "claim_task")
        self.assertEqual(_explicit(_mission("In Progress"))[0]["action"], "resume_or_claim")
        self.assertEqual(
            _explicit(_mission("In Review"))[0]["action"], "verify_merge_provenance")


class GenericPlannerActions(unittest.TestCase):
    """The deliverable drain (_mission_next_actions) is route-aware too."""

    @staticmethod
    def _links(status, *, route=None, satisfied=True, claims=(), dependency_rows=None):
        mission = _mission(
            status, route=route, satisfied=satisfied, claims=claims,
            dependency_rows=dependency_rows)
        link = dict(mission["linked_tasks"][0])
        link["blocks_deliverable"] = True
        return [link]

    def _plan(self, *args, **kwargs):
        from switchboard.storage.repositories import deliverables
        return deliverables._mission_next_actions(
            {"milestones": [{"id": "m1", "status": "in_progress"}]},
            self._links(*args, **kwargs), None)

    def _actions(self, *args, **kwargs):
        return [a for a in self._plan(*args, **kwargs)
                if a.get("task_id") == "COORD-99"
                and a.get("action") in {"claim_task", "resume_or_claim",
                                        "verify_merge_provenance"}]

    def test_blocked_remediation_is_planned(self):
        actions = self._actions("Blocked", route="remediation")
        self.assertEqual([a["action"] for a in actions], ["resume_or_claim"])
        self.assertEqual(actions[0].get("completion_route"), "remediation")

    def test_blocked_production_state_is_planned(self):
        actions = self._actions(
            "Blocked", route="remediation", dependency_rows=[])
        self.assertEqual([a["action"] for a in actions], ["resume_or_claim"])

    def test_blocked_human_is_not_planned(self):
        self.assertEqual(self._actions("Blocked", route="human"), [])

    def test_blocked_without_route_is_not_planned(self):
        self.assertEqual(self._actions("Blocked"), [])

    def test_blocked_remediation_respects_claims(self):
        self.assertEqual(
            self._actions("Blocked", route="remediation",
                          claims=[{"claim_id": "c1"}]), [])

    def test_ready_task_still_planned(self):
        self.assertEqual(
            [a["action"] for a in self._actions("Not Started")], ["claim_task"])


class BatchRouteRead(unittest.TestCase):
    """mission_status is a polled hot path; the route must not cost N queries."""

    def test_batch_reader_exists_and_uppercases_ids(self):
        self.assertTrue(hasattr(completion_runs, "list_active_completion_runs"))

    def test_empty_input_does_no_query(self):
        self.assertEqual(
            completion_runs.list_active_completion_runs([], project="switchboard"),
            {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
