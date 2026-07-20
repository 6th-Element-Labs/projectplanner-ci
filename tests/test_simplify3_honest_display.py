"""SIMPLIFY-3: board-visible honest display — no In-Progress corpses."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

import mission_graph
from switchboard.application.queries import task_session


def _seg5_view():
    """SEG-5 2026-07-20 shape: workflow In Progress, failed attempts, no runner."""
    return {
        "schema": task_session.SCHEMA,
        "lifecycle_phase": "start_failed_retry",
        "active_runner": None,
        "last_dispatch_outcome": {
            "state": "launch_failed",
            "reason": "capacity exhausted for co-general: cap=4",
            "message": "The last dispatch failed: capacity exhausted for co-general: cap=4",
            "retry_available": True,
            "dispatch_attempt": 5,
        },
        "task": {"task_id": "SEG-5", "status": "In Progress"},
    }


class HonestDisplayProjectionTest(unittest.TestCase):
    def test_start_failed_retry_label(self):
        display = task_session.display_projection(_seg5_view())
        self.assertEqual(display["label"], "Start failed / Retry available")
        self.assertTrue(display["retry_available"])
        self.assertEqual(display["reason"], "capacity exhausted for co-general: cap=4")
        self.assertEqual(display["graph_state"], "start_failed")
        self.assertEqual(display["lifecycle_phase"], "start_failed_retry")

    def test_running_keeps_in_progress_graph_state(self):
        display = task_session.display_projection({
            "lifecycle_phase": "running",
            "active_runner": {"runner_session_id": "run-1"},
            "last_dispatch_outcome": None,
            "task": {"status": "In Progress"},
        })
        self.assertEqual(display["label"], "In Progress")
        self.assertEqual(display["graph_state"], "in_progress")
        self.assertFalse(display["retry_available"])


class HonestGraphStateTest(unittest.TestCase):
    def test_seg5_shape_is_start_failed_not_in_progress(self):
        detail = {
            "status": "In Progress",
            "lifecycle_phase": "start_failed_retry",
            "honest_display": task_session.display_projection(_seg5_view()),
            "provenance": {},
        }
        self.assertEqual(mission_graph.node_execution_state(detail), "start_failed")

    def test_plain_in_progress_unchanged(self):
        self.assertEqual(
            mission_graph.node_execution_state({"status": "In Progress"}),
            "in_progress",
        )


class FailedWakeProjectsStartFailedTest(unittest.TestCase):
    @patch.object(task_session.deliverables_repo, "list_task_deliverable_links", return_value=[])
    def test_failed_wake_no_runner_is_start_failed_retry(self, _links):
        task = {
            "task_id": "SEG-5", "status": "In Progress",
            "agent_state": {}, "git_state": {},
        }
        wake = {
            "wake_id": "wake-fail", "status": "failed", "requested_at": 5,
            "claimed_by_host": "host/aws", "policy": {"dispatch_attempt": 5},
            "result": {"reason": "capacity exhausted for co-general: cap=4",
                       "failure_class": "launch_failed"},
        }
        with patch.object(task_session.tasks_repo, "get_task", return_value=task), \
                patch.object(task_session.runner_repo, "list_runner_sessions", return_value=[]), \
                patch.object(task_session.coordination_repo, "list_wake_intents",
                             return_value=[wake]), \
                patch.object(task_session.runner_repo, "resolve_task_active_runner",
                             return_value={"active": False, "session": None}), \
                patch.object(task_session.runner_repo, "latest_dispatch_outcome",
                             return_value={
                                 "state": "launch_failed",
                                 "reason": "capacity exhausted for co-general: cap=4",
                                 "message": "The last dispatch failed: capacity exhausted for co-general: cap=4",
                                 "retry_available": True,
                                 "dispatch_attempt": 5,
                             }):
            view = task_session.execute_for("SEG-5", project="switchboard")
        self.assertEqual(view["lifecycle_phase"], "start_failed_retry")
        display = task_session.display_projection(view)
        self.assertEqual(display["label"], "Start failed / Retry available")
        self.assertIn("capacity exhausted", display["reason"])


if __name__ == "__main__":
    unittest.main()
