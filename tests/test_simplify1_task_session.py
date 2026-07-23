import unittest
from unittest.mock import patch
from path_setup import ROOT  # noqa: F401

from switchboard.application.queries import task_session


def _task():
    return {
        "task_id": "SIMPLIFY-1", "status": "In Progress", "_wsId": "SIMPLIFY",
        "agent_state": {}, "git_state": {},
    }


class TaskSessionProjectionTest(unittest.TestCase):
    @patch.object(task_session.completion_repo, "get_completion", return_value={
        "phase": "merge_queue", "outcome": "succeeded", "head_sha": "a" * 40,
        "runner_generation": 2,
    })
    @patch.object(task_session.deliverables_repo, "list_task_deliverable_links", return_value=[])
    @patch.object(task_session.runner_repo, "latest_dispatch_outcome", return_value={})
    @patch.object(task_session.tasks_repo, "get_task", return_value={
        "task_id": "SIMPLIFY-1", "status": "In Review", "agent_state": {},
        "git_state": {"pr_number": 780, "head_sha": "a" * 40},
    })
    def test_durable_completion_owns_post_runner_phase(
            self, _task_mock, _outcome, _links, completion):
        wake = {"wake_id": "wake-1", "status": "completed", "requested_at": 1,
                "policy": {"assignment": {"generation": 2}}, "result": {}}
        with patch.object(task_session.runner_repo, "list_runner_sessions", return_value=[]), \
                patch.object(task_session.coordination_repo, "list_wake_intents", return_value=[wake]), \
                patch.object(task_session.runner_repo, "resolve_task_active_runner",
                             return_value={"active": False, "session": None}):
            view = task_session.execute_for("SIMPLIFY-1", project="switchboard")
        self.assertEqual(view["completion_phase"], "merge_queue")
        self.assertEqual(view["lifecycle_phase"], "review")
        completion.assert_called_once_with(
            "SIMPLIFY-1", pr_number=780, head_sha="a" * 40,
            runner_generation=2, project="switchboard")

    @patch.object(task_session.deliverables_repo, "list_task_deliverable_links", return_value=[])
    @patch.object(task_session.runner_repo, "latest_dispatch_outcome", return_value={})
    @patch.object(task_session.tasks_repo, "get_task", side_effect=lambda *a, **k: _task())
    def test_single_running_projection(self, _task_mock, _outcome, _links):
        runner = {
            "runner_session_id": "run-1", "task_id": "SIMPLIFY-1",
            "host_id": "host/mac", "status": "running", "stale": False,
            "claim_id": "claim-1", "metadata": {
                "wake_id": "wake-1", "work_session_id": "ws-1",
                "transcript_ref": "transcript://run-1",
            },
        }
        wake = {
            "wake_id": "wake-1", "status": "completed", "requested_at": 1,
            "claimed_by_host": "host/mac", "runner_session_id": "run-1",
            "policy": {"assignment": {"role": "implementation"}}, "result": {},
        }
        with patch.object(task_session.runner_repo, "list_runner_sessions", return_value=[runner]), \
                patch.object(task_session.coordination_repo, "list_wake_intents", return_value=[wake]), \
                patch.object(task_session.runner_repo, "resolve_task_active_runner",
                             return_value={"active": True, "session": runner}):
            view = task_session.execute_for("SIMPLIFY-1", project="switchboard")
        self.assertEqual(view["schema"], "switchboard.task_session.v1")
        self.assertEqual(view["lifecycle_phase"], "running")
        self.assertEqual(view["active_runner"]["runner_session_id"], "run-1")
        self.assertEqual(view["active_attempt"]["runner_session_id"], "run-1")
        self.assertEqual(view["active_host"], {"host_id": "host/mac"})
        self.assertEqual(view["transcript_ref"], "transcript://run-1")

    @patch.object(task_session.deliverables_repo, "list_task_deliverable_links", return_value=[])
    @patch.object(task_session.tasks_repo, "get_task", side_effect=lambda *a, **k: _task())
    def test_terminal_runner_overrides_claimed_wake(self, _task_mock, _links):
        runner = {
            "runner_session_id": "run-dead", "task_id": "SIMPLIFY-1",
            "host_id": "host/mac", "status": "failed", "stale": False,
            "metadata": {"wake_id": "wake-1", "failure_reason": "CLI exited 17"},
        }
        wake = {
            "wake_id": "wake-1", "status": "claimed", "requested_at": 1,
            "claimed_by_host": "host/mac", "policy": {}, "result": {},
        }
        with patch.object(task_session.runner_repo, "list_runner_sessions", return_value=[runner]), \
                patch.object(task_session.coordination_repo, "list_wake_intents", return_value=[wake]), \
                patch.object(task_session.runner_repo, "resolve_task_active_runner",
                             return_value={"active": False, "session": None}), \
                patch.object(task_session.runner_repo, "latest_dispatch_outcome",
                             return_value={"state": "dispatching"}):
            view = task_session.execute_for("SIMPLIFY-1", project="switchboard")
        self.assertIsNone(view["active_runner"])
        self.assertEqual(view["lifecycle_phase"], "start_failed_retry")
        self.assertEqual(view["last_dispatch_outcome"]["state"], "launch_failed")
        self.assertTrue(view["last_dispatch_outcome"]["retry_available"])
        self.assertEqual(view["last_dispatch_outcome"]["reason"], "CLI exited 17")


if __name__ == "__main__":
    unittest.main()
