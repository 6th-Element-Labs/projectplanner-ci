#!/usr/bin/env python3
"""BUG-164: public production completion driver and effect ports."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.application import completion_driver
from switchboard.domain.completion.executor import CompletionEffectAdapters
from switchboard.domain.completion.state_machine import build_completion_snapshot


HEAD_810 = "88624a605727fd44df98191d5b7dd99c73b75d9c"
HEAD_812 = "25951e34" + "0" * 32
PR_810 = "https://github.com/6th-Element-Labs/projectplanner/pull/810"
PR_812 = "https://github.com/6th-Element-Labs/projectplanner/pull/812"


def managed_runner(head: str, generation: int, role: str) -> dict:
    return {
        "live": True,
        "runner_session_id": f"runner-{generation}",
        "execution_id": f"execution-{generation}",
        "execution_connection_id": f"connection-{generation}",
        "generation": generation,
        "fence_epoch": generation,
        "role": role,
        "head_sha": head,
    }


def pr810():
    return build_completion_snapshot(
        task={"task_id": "COORD-41", "status": "In Review",
              "git_state": {"head_sha": HEAD_810, "pr_number": 810,
                            "pr_url": PR_810}},
        github_pr={"number": 810, "state": "open", "draft": True,
                   "url": PR_810,
                   "mergeable": True, "mergeStateStatus": "BLOCKED",
                   "head": {"sha": HEAD_810}},
        required_status_contexts=["Switchboard CI / VM gate"],
        status_contexts=[{"name": "Switchboard CI / VM gate",
                          "conclusion": "failure",
                          "failure_attribution": "product"}],
        review={"status": "passed", "head_sha": HEAD_810, "pr_url": PR_810},
        runner=managed_runner(HEAD_810, 9, "review_merge"),
    )


def pr812():
    return build_completion_snapshot(
        task={"task_id": "ADAPTER-25", "status": "In Review",
              "git_state": {"head_sha": HEAD_812, "pr_number": 812,
                            "pr_url": PR_812}},
        github_pr={"number": 812, "state": "open", "draft": False,
                   "url": PR_812,
                   "mergeable": True, "mergeStateStatus": "BLOCKED",
                   "head": {"sha": HEAD_812}},
        required_status_contexts=["Switchboard CI / VM gate"],
        status_contexts=[{"name": "Switchboard CI / VM gate",
                          "conclusion": "success"}],
        review={
            "status": "changes_requested",
            "head_sha": HEAD_812,
            "pr_url": PR_812,
            "findings": [
                {"id": "pin", "class": "auto"},
                {"id": "credential", "class": "escalate"},
                {"id": "reconnect", "class": "auto"},
            ],
        },
        runner={"live": False},
    )


class CompletionDriver(unittest.TestCase):
    def test_public_hydrator_builds_exact_head_snapshot_without_gate_writes(self):
        class Store:
            @staticmethod
            def get_task(_task_id, project):
                return {
                    "task_id": "COORD-41", "status": "In Review",
                    "git_state": {
                        "head_sha": HEAD_810, "pr_number": 810,
                        "pr_url": PR_810,
                    },
                    "review_verdict": {
                        "current_verdict": {
                            "status": "passed", "head_sha": HEAD_810,
                            "pr_url": PR_810,
                        },
                    },
                    "session_health": {"latest_sessions": []},
                    "provenance": {},
                }

            @staticmethod
            def get_project_github_repo(_project):
                return "owner/repo"

        github_pr = {
            "number": 810, "state": "open", "draft": True,
            "mergeable": True, "head": {"sha": HEAD_810},
            "url": PR_810,
        }
        gate = {
            "task_id": "COORD-41", "pr_number": 810,
            "head_sha": HEAD_810, "findings": [],
            "required_status_contexts": ["ci"],
            "status_contexts": {"ci": {"name": "ci", "state": "success"}},
        }
        with (
            patch(
                "switchboard.storage.repositories.provenance._github_token",
                return_value="token",
            ),
            patch(
                "switchboard.storage.repositories.provenance._github_pr",
                return_value=github_pr,
            ),
            patch(
                "switchboard.application.commands.merge_gate.merge_gate",
                return_value=gate,
            ) as merge_gate,
            patch(
                "switchboard.application.queries.task_session.execute_for",
                return_value={},
            ),
        ):
            snapshot = completion_driver.hydrate_completion_snapshot(
                "COORD-41", project="switchboard", actor="owner",
                store_mod=Store,
            )
        self.assertEqual(snapshot["head_sha"], HEAD_810)
        self.assertEqual(snapshot["task_id"], "COORD-41")
        self.assertEqual(snapshot["status_contexts"]["ci"]["state"], "success")
        self.assertFalse(merge_gate.call_args.kwargs["record"])

    def test_public_tick_routes_pr810_and_executes_one_effect(self):
        calls = []
        adapters = CompletionEffectAdapters(
            start_remediation=lambda plan: calls.append(dict(plan)) or {
                "action": "started", "execution_id": "exec-remediation"},
        )
        with (
            patch(
                "switchboard.storage.repositories.completion_runs."
                "get_active_completion_run",
                return_value={"run_id": "run-810", "state_version": 2,
                              "attempt": 0},
            ),
            patch(
                "switchboard.domain.completion.executor._persist_run",
                return_value={"run_id": "run-810", "state_version": 2},
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "claim_external_effect",
                return_value={"claimed": True, "effect_key": "effect-810"},
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "verify_external_effect",
                return_value={"effect_key": "effect-810"},
            ),
            patch(
                "switchboard.application.commands.task_execution."
                "fence_task_generation",
                return_value={"fenced": True},
            ) as stop,
        ):
            result = completion_driver.run_completion_tick(
                "COORD-41", project="switchboard", actor="owner",
                agent_id="owner", store_mod=object(),
                hydrator=lambda *args, **kwargs: pr810(),
                adapters=adapters,
            )
        self.assertEqual(result["decision"]["route"], "remediation")
        self.assertEqual(result["plan"]["effect"], "start_remediation")
        self.assertEqual(len(calls), 1)
        stop.assert_called_once()

    def test_enqueue_adapter_uses_merge_queue_mutation(self):
        class Store:
            @staticmethod
            def get_project_github_repo(_project):
                return "owner/repo"

        with (
            patch(
                "switchboard.storage.repositories.provenance._github_token",
                return_value="token",
            ),
            patch(
                "switchboard.storage.repositories.provenance._github_pr",
                return_value={"node_id": "PR_node"},
            ),
            patch.object(
                completion_driver, "_github_command",
                return_value={"returncode": 0},
            ) as command,
        ):
            adapters = completion_driver.production_effect_adapters(
                project="switchboard", actor="owner", agent_id="owner",
                store_mod=Store,
            )
            result = adapters.enqueue({"pr_number": 811})
        self.assertEqual(result["returncode"], 0)
        args = command.call_args.args[0]
        self.assertEqual(args[:2], ["api", "graphql"])
        self.assertIn("enqueuePullRequest", " ".join(args))

    def test_mixed_pr812_findings_reach_remediation_port(self):
        calls = []
        adapters = CompletionEffectAdapters(
            start_remediation=lambda plan: calls.append(dict(plan)) or {
                "action": "started"},
        )
        with (
            patch(
                "switchboard.storage.repositories.completion_runs."
                "get_active_completion_run",
                return_value={"run_id": "run-812", "state_version": 1,
                              "attempt": 0},
            ),
            patch(
                "switchboard.domain.completion.executor._persist_run",
                return_value={"run_id": "run-812", "state_version": 1},
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "claim_external_effect",
                return_value={"claimed": True, "effect_key": "effect-812"},
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "verify_external_effect",
                return_value={"effect_key": "effect-812"},
            ),
        ):
            result = completion_driver.run_completion_tick(
                "ADAPTER-25", project="switchboard", actor="owner",
                agent_id="owner", store_mod=object(),
                hydrator=lambda *args, **kwargs: pr812(),
                adapters=adapters,
            )
        self.assertEqual(result["decision"]["route"], "remediation")
        self.assertEqual(
            [row["id"] for row in calls[0]["acceptance_findings"]],
            ["pin", "reconnect"],
        )
        self.assertEqual(
            [row["id"] for row in calls[0]["escalated_findings"]],
            ["credential"],
        )

    def test_transitioning_replacement_remains_unverified_for_next_tick(self):
        adapters = CompletionEffectAdapters(
            start_remediation=lambda _plan: {"action": "transitioning"},
        )
        with (
            patch(
                "switchboard.storage.repositories.completion_runs."
                "get_active_completion_run",
                return_value={"run_id": "run-810", "state_version": 2,
                              "attempt": 0},
            ),
            patch(
                "switchboard.domain.completion.executor._persist_run",
                return_value={"run_id": "run-810", "state_version": 2},
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "claim_external_effect",
                return_value={"claimed": True, "effect_key": "effect-810"},
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "mark_external_effect_issued",
                return_value={"effect_key": "effect-810"},
            ) as issued,
            patch(
                "switchboard.application.commands.task_execution."
                "fence_task_generation",
                return_value={"fenced": True},
            ),
        ):
            result = completion_driver.run_completion_tick(
                "COORD-41", project="switchboard", actor="owner",
                agent_id="owner", store_mod=object(),
                hydrator=lambda *args, **kwargs: pr810(),
                adapters=adapters,
            )
        self.assertTrue(result["execution"]["receipt"]["pending"])
        self.assertFalse(result["execution"]["receipt"]["verified"])
        issued.assert_called_once()

    def test_concurrent_claim_does_not_double_fire_effect(self):
        calls = []
        adapters = CompletionEffectAdapters(
            start_remediation=lambda plan: calls.append(dict(plan)) or {
                "action": "started"},
        )
        with (
            patch(
                "switchboard.storage.repositories.completion_runs."
                "get_active_completion_run",
                return_value={"run_id": "run-810", "state_version": 2,
                              "attempt": 0},
            ),
            patch(
                "switchboard.domain.completion.executor._persist_run",
                return_value={"run_id": "run-810", "state_version": 2},
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "claim_external_effect",
                return_value={
                    "claimed": False,
                    "effect_key": "effect-810",
                    "effect": {"status": "claimed", "updated_at": 9999999999},
                },
            ),
        ):
            result = completion_driver.run_completion_tick(
                "COORD-41", project="switchboard", actor="owner",
                agent_id="owner", store_mod=object(),
                hydrator=lambda *args, **kwargs: pr810(),
                adapters=adapters,
            )
        self.assertEqual(calls, [])
        self.assertEqual(
            result["execution"]["receipt"]["reason"],
            "effect_claim_in_flight",
        )


if __name__ == "__main__":
    unittest.main()
