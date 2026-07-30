#!/usr/bin/env python3
"""BUG-244: v4 receives exact terminal facts without reviving v1."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from path_setup import ROOT as _ROOT  # noqa: F401
import external_ci_mirror
from switchboard.application.commands import runner_control


class MissionV4WakeWiringTest(unittest.TestCase):
    def test_terminal_external_ci_projects_one_status_fact(self):
        updated = {
            "status": "success",
            "source_sha": "a" * 40,
            "source_repo": "6th-Element-Labs/projectplanner",
            "status_context": "Switchboard CI / VM gate",
            "run_url": "https://example.test/actions/runs/1",
        }
        with patch(
            "switchboard.application.commands.github_mission_events.project_delivery",
            return_value={"action": "github_mission_events_projected", "events": []},
        ) as delivery:
            receipt = external_ci_mirror._project_terminal_mission_event(
                updated, project="switchboard")
        self.assertEqual("github_mission_events_projected", receipt["action"])
        event, payload = delivery.call_args.args
        self.assertEqual("status", event)
        self.assertEqual("a" * 40, payload["sha"])
        self.assertEqual("success", payload["state"])
        self.assertEqual("Switchboard CI / VM gate", payload["context"])
        self.assertEqual("switchboard", delivery.call_args.kwargs["project"])

    def test_nonterminal_external_ci_is_not_a_wake_edge(self):
        receipt = external_ci_mirror._project_terminal_mission_event(
            {"status": "running"}, project="switchboard")
        self.assertEqual("ignored", receipt["action"])

    def test_terminal_runner_projects_exact_execution_and_c3_role(self):
        terminal = {
            "runner_session_id": "runner-244",
            "task_id": "BUG-244",
            "status": "completed",
            "metadata": {
                "execution_id": "execution-244",
                "execution_generation": 4,
                "execution_head_sha": "b" * 40,
            },
            "completion": {"completed": True},
        }
        with (
            patch.object(
                runner_control.runner_repo,
                "upsert_runner_session",
                return_value=dict(terminal),
            ),
            patch(
                "switchboard.storage.repositories.mission_journal."
                "default_mission_journal_repository.record_runner_terminal",
                return_value={"created": True, "event_type": "runner_ended"},
            ) as projected,
            patch.object(
                runner_control.runner_repo,
                "get_runner_session",
                return_value=None,
            ),
        ):
            result = runner_control.upsert_session_mapping_result(
                terminal, actor="host", principal_id="principal")
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, projected.call_count)
        kwargs = projected.call_args.kwargs
        self.assertEqual("execution-244", kwargs["execution_id"])
        self.assertEqual(4, kwargs["generation"])
        self.assertEqual("review_merge", kwargs["accepted_role"])
        self.assertEqual("b" * 40, kwargs["head_sha"])


if __name__ == "__main__":
    unittest.main()
