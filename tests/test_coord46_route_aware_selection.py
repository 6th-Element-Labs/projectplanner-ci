#!/usr/bin/env python3
"""COORD-46 prerequisite: Autopilot candidate selection is route-aware.

The completion state machine projects ``remediation`` onto board ``Blocked``.
Every live candidate layer selected by status alone therefore stops producing
remediation work the moment that projection lands -- silently, with no error.

These tests pin the contract named in the design's "Prerequisites of the
COORD-20 status change":

  * Blocked + route=remediation stays a dispatchable candidate.
  * Blocked with no automatic route (human, or no run at all) does not.
"""
from __future__ import annotations

import unittest

from path_setup import ROOT  # noqa: F401

import coordinator_daemon as daemon_mod  # noqa: E402
from switchboard.domain.completion import routing  # noqa: E402


def _detail(status, *, route=None, ready=True, claims=(), task_id="COORD-99"):
    detail = {
        "task_id": task_id,
        "status": status,
        "active_claims": list(claims),
        "dependency_state": {"ready": ready},
    }
    if route is not None:
        detail["completion_run"] = {"route": route, "state": "blocked"}
    return detail


class RouteContract(unittest.TestCase):
    def test_remediation_is_an_automatic_route(self):
        self.assertTrue(routing.route_allows_dispatch("remediation"))

    def test_human_is_never_an_automatic_route(self):
        self.assertFalse(routing.route_allows_dispatch("human"))

    def test_absent_route_is_not_dispatchable(self):
        self.assertFalse(routing.route_allows_dispatch(""))
        self.assertFalse(routing.route_allows_dispatch(None))

    def test_route_is_read_from_the_completion_run_projection(self):
        self.assertEqual(
            routing.completion_route(_detail("Blocked", route="remediation")),
            "remediation")

    def test_route_reads_agent_state_projection(self):
        detail = {"agent_state": {"completion_run": {"route": "remediation"}}}
        self.assertEqual(routing.completion_route(detail), "remediation")

    def test_missing_route_resolves_empty_not_none(self):
        self.assertEqual(routing.completion_route(_detail("Blocked")), "")

    def test_route_lookup_falls_back_to_the_durable_store(self):
        class _Store:
            def get_active_completion_run(self, task_id, project=""):
                assert task_id == "COORD-99"
                return {"route": "remediation"}

        self.assertEqual(
            routing.resolve_completion_route(
                _detail("Blocked"), store=_Store(), project="switchboard"),
            "remediation")

    def test_store_lookup_is_skipped_when_projection_already_has_route(self):
        class _Boom:
            def get_active_completion_run(self, task_id, project=""):
                raise AssertionError("must not query when projection carries route")

        self.assertEqual(
            routing.resolve_completion_route(
                _detail("Blocked", route="human"), store=_Boom(),
                project="switchboard"),
            "human")

    def test_store_failure_never_makes_a_blocked_task_dispatchable(self):
        class _Broken:
            def get_active_completion_run(self, task_id, project=""):
                raise RuntimeError("db down")

        self.assertEqual(
            routing.resolve_completion_route(
                _detail("Blocked"), store=_Broken(), project="switchboard"),
            "")


class ReadyForDispatch(unittest.TestCase):
    """The shared predicate the daemon and the scope fallback both use."""

    def test_blocked_with_remediation_route_is_dispatchable(self):
        self.assertTrue(routing.task_ready_for_dispatch(
            _detail("Blocked", route="remediation")))

    def test_blocked_with_human_route_is_not_dispatchable(self):
        self.assertFalse(routing.task_ready_for_dispatch(
            _detail("Blocked", route="human")))

    def test_blocked_with_no_route_is_not_dispatchable(self):
        self.assertFalse(routing.task_ready_for_dispatch(_detail("Blocked")))

    def test_blocked_remediation_still_respects_dependencies(self):
        self.assertFalse(routing.task_ready_for_dispatch(
            _detail("Blocked", route="remediation", ready=False)))

    def test_blocked_remediation_still_respects_a_conflicting_claim(self):
        self.assertFalse(routing.task_ready_for_dispatch(
            _detail("Blocked", route="remediation",
                    claims=[{"claim_id": "c1"}])))

    def test_existing_status_behaviour_is_unchanged(self):
        self.assertTrue(routing.task_ready_for_dispatch(_detail("Not Started")))
        self.assertTrue(routing.task_ready_for_dispatch(_detail("In Review")))
        self.assertTrue(routing.task_ready_for_dispatch(_detail("In Progress")))
        self.assertFalse(routing.task_ready_for_dispatch(
            _detail("Not Started", ready=False)))
        self.assertFalse(routing.task_ready_for_dispatch(
            _detail("In Progress", claims=[{"claim_id": "c1"}])))


class DaemonUsesSharedPredicate(unittest.TestCase):
    """Wiring: the daemon must not keep its own status-only copy."""

    def test_daemon_delegates_to_the_shared_predicate(self):
        self.assertTrue(daemon_mod.CoordinatorDaemon._task_ready_for_dispatch(
            _detail("Blocked", route="remediation")))
        self.assertFalse(daemon_mod.CoordinatorDaemon._task_ready_for_dispatch(
            _detail("Blocked", route="human")))
        self.assertFalse(daemon_mod.CoordinatorDaemon._task_ready_for_dispatch(
            _detail("Blocked")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
