import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.application.queries import task_session


class TaskSessionDoctorTest(unittest.TestCase):
    def test_start_repair_targets_the_existing_task_action(self):
        source = (ROOT / "static/js/runner-session.js").read_text()
        self.assertIn("getElementById('task-primary-start')?.click()", source)
        self.assertNotIn("getElementById('task-session-start')", source)

    def test_live_projection_is_watchable(self):
        aggregate = {
            "lifecycle_phase": "running",
            "active_runner": {"runner_session_id": "run_1", "status": "running"},
            "active_attempt": None,
            "last_dispatch_outcome": None,
        }
        with patch.object(task_session, "execute_for", return_value=aggregate):
            doctor = task_session.doctor_for("UI-57", project="switchboard")
        self.assertEqual(doctor["execution_id"], "run_1")
        self.assertTrue(doctor["watchable_now"])
        self.assertEqual(doctor["repair"], {"action": "watch", "label": "Watch execution"})
        self.assertTrue(doctor["reopenable"])

    def test_failed_projection_has_one_blocker_and_repair(self):
        aggregate = {
            "lifecycle_phase": "start_failed_retry",
            "active_runner": None,
            "active_attempt": {"wake_id": "wake_1"},
            "last_dispatch_outcome": {
                "state": "launch_failed", "message": "No host accepted the execution."},
        }
        with patch.object(task_session, "execute_for", return_value=aggregate):
            doctor = task_session.doctor_for("UI-57", project="switchboard")
        self.assertEqual(doctor["blocked_at"], "launch_failed")
        self.assertEqual(doctor["message"], "No host accepted the execution.")
        self.assertEqual(doctor["repair"], {"action": "retry", "label": "Retry start"})
        self.assertFalse(doctor["watchable_now"])

    def test_missing_task_stays_missing(self):
        with patch.object(task_session, "execute_for", return_value=None):
            self.assertIsNone(task_session.doctor_for("NOPE", project="switchboard"))


if __name__ == "__main__":
    unittest.main()
