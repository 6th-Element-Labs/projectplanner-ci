#!/usr/bin/env python3
"""BUG-164: public production completion driver and effect ports."""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.application import completion_driver
from switchboard.domain.completion.executor import CompletionEffectAdapters
from switchboard.domain.completion.normalization_law import LAW_ROWS
from switchboard.domain.completion.state_machine import build_completion_snapshot


HEAD_810 = "88624a605727fd44df98191d5b7dd99c73b75d9c"
HEAD_812 = "25951e34" + "0" * 32
PR_810 = "https://github.com/6th-Element-Labs/projectplanner/pull/810"
PR_812 = "https://github.com/6th-Element-Labs/projectplanner/pull/812"

# These are unit proofs of the driver's ports and routing; they hold no database.
# run_completion_tick also appends to the COORD-50 decision corpus, whose persistence
# is proven in tests/test_coord50_decision_corpus.py.
_CORPUS_PATCH = patch(
    "switchboard.storage.repositories.decision_records.record_decision_episode",
    return_value={},
)


def setUpModule():
    _CORPUS_PATCH.start()


def tearDownModule():
    _CORPUS_PATCH.stop()


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


def fresh_snapshot(snapshot: dict) -> dict:
    observed_at = time.time()
    snapshot.update({
        "observed_at": observed_at,
        "hydration_started_at": observed_at,
        "source_observed_at": {
            source: observed_at
            for row in LAW_ROWS
            for source in row.authoritative_sources
        },
    })
    return snapshot


def pr810():
    return fresh_snapshot(build_completion_snapshot(
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
        runner={"live": False},
    ))


def pr812():
    return fresh_snapshot(build_completion_snapshot(
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
    ))


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
            patch(
                "switchboard.storage.repositories.autopilot_scopes."
                "list_autopilot_scopes",
                return_value=[],
            ),
        ):
            snapshot = completion_driver.hydrate_completion_snapshot(
                "COORD-41", project="switchboard", actor="owner",
                store_mod=Store,
            )
        self.assertEqual(snapshot["head_sha"], HEAD_810)
        self.assertEqual(snapshot["task_id"], "COORD-41")
        self.assertEqual(snapshot["status_contexts"]["ci"]["state"], "success")
        self.assertGreater(len(set(snapshot["source_observed_at"].values())), 1)
        self.assertNotEqual(
            set(snapshot["source_observed_at"].values()),
            {snapshot["observed_at"]},
        )
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
        stop.assert_not_called()

    def test_enqueue_adapter_arms_squash_auto_merge(self):
        class Store:
            @staticmethod
            def get_project_github_repo(_project):
                return "owner/repo"

        with (
            patch(
                "switchboard.storage.repositories.provenance._github_token",
                return_value="token",
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
            result = adapters.enqueue({
                "pr_number": 811,
                "head_sha": "a" * 40,
            })
        self.assertEqual(result["returncode"], 0)
        args = command.call_args.args[0]
        self.assertEqual(args[:3], ["pr", "merge", "811"])
        self.assertIn("--auto", args)
        self.assertIn("--squash", args)
        self.assertEqual(
            args[args.index("--match-head-commit") + 1],
            "a" * 40,
        )

    def test_update_branch_adapter_is_retired_from_production(self):
        class Store:
            @staticmethod
            def get_project_github_repo(_project):
                return "owner/repo"

        with (
            patch(
                "switchboard.storage.repositories.provenance._github_token",
                return_value="token",
            ),
        ):
            adapters = completion_driver.production_effect_adapters(
                project="switchboard", actor="owner", agent_id="owner",
                store_mod=Store,
            )
        self.assertIsNone(adapters.update_branch)

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
        # Mission Bot boots remediation with the full dossier. Credential /
        # escalate-class findings are not split to humans by the controller —
        # only a server-stamped agent_requires_human receipt may stop for a human.
        self.assertEqual(
            [row["id"] for row in calls[0]["acceptance_findings"]],
            ["pin", "credential", "reconnect"],
        )
        self.assertFalse(calls[0].get("escalated_findings"))

    def test_transitioning_task_execution_remains_unverified_for_next_tick(self):
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
        # START receipts belong to Task Execution, not the external-effect
        # ledger used for GitHub mutations.
        issued.assert_not_called()

    def test_parallel_effect_claim_cannot_veto_task_execution_start(self):
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
        self.assertEqual(len(calls), 1)
        self.assertFalse(result["execution"]["receipt"]["pending"])
        self.assertTrue(result["execution"]["receipt"]["verified"])
        self.assertFalse(result["execution"]["receipt"]["idempotent_replay"])

    def test_empty_queue_hydrates_prior_enqueue_from_verified_ledger(self):
        with patch(
            "switchboard.storage.repositories.external_effects."
            "list_external_effects",
            return_value=[{
                "effect_key": "effect-enqueue-1",
                "resource": "enqueue",
                "updated_at": 100.0,
                "payload": {"head_sha": HEAD_810, "effect": "enqueue"},
                "readback": {},
            }],
        ):
            queue = completion_driver._merge_queue_snapshot(
                {},
                task_id="COORD-41",
                head_sha=HEAD_810,
                project="switchboard",
            )
        self.assertTrue(queue["prior_enqueue_verified"])
        self.assertEqual(queue["prior_enqueue_effect_key"], "effect-enqueue-1")
        self.assertEqual(queue["verified_queue_effect_count"], 1)

    def test_ledger_enqueue_without_head_sha_is_not_tip_provenance(self):
        with patch(
            "switchboard.storage.repositories.external_effects."
            "list_external_effects",
            return_value=[{
                "effect_key": "effect-enqueue-old",
                "resource": "enqueue",
                "updated_at": 100.0,
                "payload": {"effect": "enqueue"},
                "readback": {},
            }],
        ):
            queue = completion_driver._merge_queue_snapshot(
                {},
                task_id="COORD-41",
                head_sha=HEAD_810,
                project="switchboard",
            )
        self.assertEqual(queue, {})


if __name__ == "__main__":
    unittest.main()
