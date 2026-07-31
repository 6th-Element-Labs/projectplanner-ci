import unittest
from unittest.mock import patch

from path_setup import ROOT as _ROOT  # noqa: F401
from switchboard.application.queries import mission_context


class FakeJournal:
    def __init__(self, mission=None, events=None):
        self.mission = mission
        self.events = list(events or [])

    def get_item(self, task_id, *, project):
        return self.mission

    def list_events(self, task_id, *, project, after_sequence=0, limit=50):
        return [
            event for event in self.events
            if int(event["sequence"]) > int(after_sequence)
        ][:limit]


class Adapter45MissionProtocolTest(unittest.TestCase):
    def test_no_mission_is_inert_and_names_the_missing_authority(self):
        with patch.object(
            mission_context.tasks_repository,
            "get_task",
            side_effect=AssertionError("task store must not be queried"),
        ):
            result = mission_context.get(
                "QA-1", project="switchboard", repository=FakeJournal(),
            )
        self.assertIsNone(result["mission"])
        self.assertIsNone(result["current"])
        self.assertEqual([], result["recent_history"])
        self.assertEqual(["mission_journal"], result["missing_sources"])
        self.assertFalse(result["context_complete"])

    def test_context_exposes_facts_without_route_or_merge_derivation(self):
        journal = FakeJournal(
            mission={
                "task_id": "QA-1", "state": "ACTIVE", "latest_sequence": 1,
            },
            events=[{"sequence": 1, "event_type": "mission_started"}],
        )
        task = {
            "task_id": "QA-1",
            "status": "in_review",
            "dependency_state": {"satisfied": True},
            "git_state": {},
            "external_ci": {},
        }
        topology = {
            "roles": {"canonical": {
                "repo": "6th-Element-Labs/projectplanner",
                "required_status_contexts": [],
            }},
        }
        with (
            patch.object(
                mission_context.tasks_repository, "get_task", return_value=task,
            ),
            patch.object(
                mission_context.projects_repository,
                "get_project_repo_topology",
                return_value=topology,
            ),
            patch.object(
                mission_context.runner_repository,
                "list_runner_sessions",
                return_value=[{
                    "runner_session_id": "runner-1",
                    "host_id": "host/mac",
                    "agent_id": "codex/QA-1",
                    "runtime": "codex",
                    "status": "running",
                    "stale": False,
                    "execution": {"execution_id": "exec-1", "generation": 1},
                    "metadata": {"direct_session_token": "must-not-leak"},
                    "control": {"secret": "must-not-leak"},
                }],
            ),
        ):
            result = mission_context.get(
                "QA-1", project="switchboard", repository=journal,
            )
        serialized = repr(result)
        for forbidden in (
            "ready_to_merge", "fixable", "needs_human", "requested_route",
            "next_role", "start_task",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual("in_review", result["current"]["task_status"])
        self.assertTrue(result["current"]["runner"]["live"])
        self.assertNotIn("must-not-leak", serialized)
        self.assertTrue(result["context_complete"])

    def test_missing_source_preserves_exception_type_and_message(self):
        journal = FakeJournal(mission={"latest_sequence": 0}, events=[])
        with (
            patch.object(
                mission_context.tasks_repository,
                "get_task",
                side_effect=RuntimeError("task database unavailable"),
            ),
            patch.object(
                mission_context.projects_repository,
                "get_project_repo_topology",
                return_value={"roles": {"canonical": {"repo": ""}}},
            ),
            patch.object(
                mission_context.runner_repository,
                "list_runner_sessions",
                return_value=[],
            ),
        ):
            result = mission_context.get(
                "QA-1", project="switchboard", repository=journal,
            )
        task_error = next(
            detail for detail in result["missing_source_details"]
            if detail["source"] == "task_store"
        )
        self.assertEqual("RuntimeError", task_error["error_type"])
        self.assertEqual("task database unavailable", task_error["message"])

    def test_history_has_exact_has_more_and_forward_cursor(self):
        events = [
            {"sequence": sequence, "event_type": "task_changed"}
            for sequence in range(1, 5)
        ]
        result = mission_context.list_history(
            "QA-1",
            project="switchboard",
            after_sequence=1,
            limit=2,
            repository=FakeJournal(events=events),
        )
        self.assertEqual([2, 3], [row["sequence"] for row in result["events"]])
        self.assertEqual(3, result["next_cursor"])
        self.assertTrue(result["has_more"])


if __name__ == "__main__":
    unittest.main()
