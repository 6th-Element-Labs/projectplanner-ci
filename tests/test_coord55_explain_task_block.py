from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands import task_execution
from switchboard.mcp.tools import task_execution as mcp_task_execution


def _projection(*, activity=None, runner=None):
    return {
        "task": {
            "task_id": "CO-20",
            "activity": activity or [],
            "dependency_state": {"satisfied": True, "blocking": []},
            "git_state": {"head_sha": "a" * 40},
        },
        "completion_projection": {
            "state": "assessing",
            "route": "review_merge",
            "reason_code": "review_required",
            "current_effect": "ensure_review_generation",
            "retry_deadline": 123.0,
        },
        "active_runner": runner,
        "lifecycle_phase": "review",
        "last_dispatch_outcome": None,
    }


class ExplainTaskBlockTest(unittest.TestCase):
    def test_co20_replay_preserves_effect_failure_message(self):
        effects = [{
            "effect_type": "completion_effect",
            "resource": "ensure_review_generation",
            "status": "failed",
            "retry_count": 12,
            "last_error": "Project execution readiness is blocked.",
            "updated_at": 99.0,
        }]
        with patch.object(task_execution, "_projection",
                          return_value=_projection()), patch.object(
                task_execution.external_effects_repo, "list_external_effects",
                return_value=effects):
            result = task_execution.explain_task_block(
                "CO-20", project="switchboard")

        self.assertEqual(result["blocked_by"], {
            "gate": "external_effect",
            "message": "Project execution readiness is blocked.",
            "resource": "ensure_review_generation",
        })
        self.assertEqual(result["classifier"]["planned_effect"],
                         "ensure_review_generation")
        self.assertEqual(result["external_effects"][0]["retry_count"], 12)
        self.assertLess(len(json.dumps(result)), 10_000)

    def test_pre_888_replay_preserves_completion_persistence_error(self):
        projection = _projection(activity=[{
            "kind": "coordinator.receipt",
            "created_at": 1.0,
            "payload": {
                "error": "CompletionRunError",
                "message": "unsupported completion state: assessing",
            },
        }])
        with patch.object(task_execution, "_projection",
                          return_value=projection), patch.object(
                task_execution.external_effects_repo, "list_external_effects",
                return_value=[]):
            result = task_execution.explain_task_block(
                "CO-20", project="switchboard")

        self.assertEqual(result["blocked_by"], {
            "gate": "classifier_persistence",
            "message": "unsupported completion state: assessing",
        })

    def test_runner_fence_is_compact_and_exact_head_aware(self):
        projection = _projection(runner={
            "runner_session_id": "run-1",
            "metadata": {
                "assignment_id": "assignment-1",
                "execution_id": "execution-1",
                "execution_generation": 2,
                "execution_role": "implementation",
                "execution_head_sha": "b" * 40,
            },
        })
        with patch.object(task_execution, "_projection",
                          return_value=projection), patch.object(
                task_execution.external_effects_repo, "list_external_effects",
                return_value=[]):
            result = task_execution.explain_task_block(
                "CO-20", project="switchboard")

        self.assertTrue(result["runner"]["fence_identity_complete"])
        self.assertFalse(result["runner"]["head_matches"])
        self.assertEqual(result["blocked_by"]["gate"], "runner_fence")

    def test_prod_mcp_registry_contains_read_tool(self):
        self.assertIn("explain_task_block",
                      mcp_task_execution.TASK_EXECUTION_TOOL_NAMES)


if __name__ == "__main__":
    unittest.main()
