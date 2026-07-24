#!/usr/bin/env python3
"""BUG-172: temporal proof for completion-effect identity and replay safety."""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.application import completion_driver
from switchboard.domain.completion import effects
from switchboard.domain.completion.executor import (
    CompletionEffectAdapters,
    execute_effect,
)
from switchboard.domain.completion.state_machine import build_completion_snapshot


HEAD = "8" * 40
PR_URL = "https://github.com/example/projectplanner/pull/810"


def failed_ci_snapshot(*, runner=None):
    return build_completion_snapshot(
        task={
            "task_id": "COORD-41",
            "status": "In Review",
            "git_state": {
                "head_sha": HEAD,
                "pr_number": 810,
                "pr_url": PR_URL,
            },
        },
        github_pr={
            "number": 810,
            "state": "open",
            "draft": False,
            "url": PR_URL,
            "mergeable": True,
            "mergeStateStatus": "BLOCKED",
            "head": {"sha": HEAD},
        },
        required_status_contexts=["required"],
        status_contexts=[{
            "name": "required",
            "conclusion": "failure",
            "failure_attribution": "product",
        }],
        review={"status": "passed", "head_sha": HEAD, "pr_url": PR_URL},
        runner=runner or {"live": False},
    )


def durable_run():
    return {
        "run_id": "completion-run-durable",
        "task_id": "COORD-41",
        "pr_number": 810,
        "head_sha": HEAD,
        "state": "blocked",
        "route": "remediation",
        "reason_code": "required_ci_failed",
        "desired_role": "remediation",
        "board_status": "Blocked",
        "attempt": 3,
        "state_version": 7,
    }


class DurablePlanIdentity(unittest.TestCase):
    def test_first_tick_persists_before_planning_and_replay_keeps_key(self):
        snapshot = failed_ci_snapshot()
        run = durable_run()
        adapter_calls = []
        order = []
        original_plan = completion_driver.plan_effect

        def persist(**_kwargs):
            order.append(("persist", run["run_id"]))
            return dict(run)

        def plan(decision, snap, persisted):
            order.append(("plan", persisted.get("run_id")))
            return original_plan(decision, snap, persisted)

        adapters = CompletionEffectAdapters(
            start_remediation=lambda value: (
                adapter_calls.append(dict(value))
                or {"action": "started", "execution_id": "exec-1"}
            ),
        )
        with (
            patch(
                "switchboard.storage.repositories.completion_runs."
                "get_active_completion_run",
                side_effect=[None, dict(run)],
            ),
            patch(
                "switchboard.domain.completion.executor._persist_run",
                side_effect=persist,
            ),
            patch.object(completion_driver, "plan_effect", side_effect=plan),
            patch(
                "switchboard.storage.repositories.external_effects."
                "claim_external_effect",
                side_effect=[
                    {"claimed": True, "effect_key": "effect-1"},
                    {
                        "claimed": False,
                        "verified": True,
                        "effect_key": "effect-1",
                        "proof": {"execution_id": "exec-1"},
                    },
                ],
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "verify_external_effect",
                return_value={"effect_key": "effect-1"},
            ),
        ):
            first = completion_driver.run_completion_tick(
                "COORD-41",
                project="switchboard",
                actor="owner",
                agent_id="owner",
                store_mod=object(),
                hydrator=lambda *_args, **_kwargs: snapshot,
                adapters=adapters,
            )
            second = completion_driver.run_completion_tick(
                "COORD-41",
                project="switchboard",
                actor="owner",
                agent_id="owner",
                store_mod=object(),
                hydrator=lambda *_args, **_kwargs: snapshot,
                adapters=adapters,
            )

        self.assertEqual(order[0], ("persist", run["run_id"]))
        self.assertEqual(order[1], ("plan", run["run_id"]))
        self.assertEqual(first["plan"]["idem_key"], second["plan"]["idem_key"])
        self.assertEqual(first["plan"]["completion_run_id"], run["run_id"])
        self.assertEqual(first["plan"]["decision_attempt"], 3)
        self.assertEqual(first["plan"]["state_version"], 7)
        self.assertEqual(len(adapter_calls), 1)
        self.assertTrue(second["execution"]["receipt"]["idempotent_replay"])


class IssuedEffectReplay(unittest.TestCase):
    def test_issued_effect_waits_for_readback_without_adapter_or_refence(self):
        snapshot = failed_ci_snapshot(runner={
            "live": True,
            "role": "review_merge",
            "head_sha": HEAD,
            "generation": 11,
        })
        run = durable_run()
        adapter_calls = []
        adapters = CompletionEffectAdapters(
            start_remediation=lambda value: (
                adapter_calls.append(dict(value)) or {"action": "started"}
            ),
        )
        with (
            patch(
                "switchboard.storage.repositories.completion_runs."
                "get_active_completion_run",
                return_value=dict(run),
            ),
            patch(
                "switchboard.domain.completion.executor._persist_run",
                return_value=dict(run),
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "claim_external_effect",
                return_value={
                    "claimed": False,
                    "effect_key": "effect-issued",
                    "effect": {
                        "status": "issued",
                        "readback": {"action": "transitioning"},
                    },
                },
            ),
            patch(
                "switchboard.application.commands.task_execution.stop_task",
                return_value={"stopped": True},
            ) as stop,
        ):
            result = completion_driver.run_completion_tick(
                "COORD-41",
                project="switchboard",
                actor="owner",
                agent_id="owner",
                store_mod=object(),
                hydrator=lambda *_args, **_kwargs: snapshot,
                adapters=adapters,
            )

        self.assertEqual(adapter_calls, [])
        stop.assert_not_called()
        self.assertFalse(result["execution"]["receipt"]["verified"])
        self.assertTrue(result["execution"]["receipt"]["pending"])
        self.assertEqual(
            result["execution"]["receipt"]["reason"],
            "effect_issued_awaiting_readback",
        )


class FailedEffectReplay(unittest.TestCase):
    def test_failed_effect_obeys_backoff_without_adapter_or_refence(self):
        adapter_calls = []
        adapters = CompletionEffectAdapters(
            start_remediation=lambda value: adapter_calls.append(dict(value)),
        )
        plan = effects.plan_effect(
            {
                "state": "blocked",
                "route": "remediation",
                "reason_code": "required_ci_failed",
                "desired_role": "remediation",
                "board_projection": "Blocked",
            },
            failed_ci_snapshot(),
            durable_run(),
        )
        with (
            patch(
                "switchboard.domain.completion.executor._persist_run",
                return_value=durable_run(),
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "claim_external_effect",
                return_value={
                    "claimed": False,
                    "effect_key": "effect-failed",
                    "effect": {
                        "status": "failed",
                        "retry_count": 1,
                        "updated_at": time.time(),
                        "last_error": "runner unavailable",
                    },
                },
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "retry_external_effect",
            ) as retry,
        ):
            result = execute_effect(
                plan,
                decision={
                    "state": "blocked",
                    "route": "remediation",
                    "reason_code": "required_ci_failed",
                },
                snapshot=failed_ci_snapshot(),
                run=durable_run(),
                project="switchboard",
                actor="owner",
                adapters=adapters,
            )
        self.assertEqual(adapter_calls, [])
        retry.assert_not_called()
        self.assertTrue(result["receipt"]["pending"])
        self.assertEqual(result["receipt"]["reason"], "effect_retry_backoff")

    def test_expired_failed_effect_requires_atomic_reclaim_before_reissue(self):
        adapter_calls = []
        adapters = CompletionEffectAdapters(
            start_remediation=lambda value: (
                adapter_calls.append(dict(value))
                or {"action": "started", "execution_id": "exec-retry"}
            ),
        )
        decision = {
            "state": "blocked",
            "route": "remediation",
            "reason_code": "required_ci_failed",
            "desired_role": "remediation",
            "board_projection": "Blocked",
        }
        snapshot = failed_ci_snapshot()
        plan = effects.plan_effect(decision, snapshot, durable_run())
        with (
            patch(
                "switchboard.domain.completion.executor._persist_run",
                return_value=durable_run(),
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "claim_external_effect",
                return_value={
                    "claimed": False,
                    "effect_key": "effect-failed",
                    "effect": {
                        "status": "failed",
                        "retry_count": 2,
                        "updated_at": time.time() - 120,
                    },
                },
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "retry_external_effect",
                return_value={
                    "claimed": True,
                    "effect_key": "effect-failed",
                    "retry": True,
                },
            ) as retry,
            patch(
                "switchboard.storage.repositories.external_effects."
                "verify_external_effect",
                return_value={"effect_key": "effect-failed"},
            ),
        ):
            result = execute_effect(
                plan,
                decision=decision,
                snapshot=snapshot,
                run=durable_run(),
                project="switchboard",
                actor="owner",
                adapters=adapters,
            )
        retry.assert_called_once_with(
            "effect-failed",
            expected_retry_count=2,
            actor="owner",
            project="switchboard",
        )
        self.assertEqual(len(adapter_calls), 1)
        self.assertTrue(result["receipt"]["verified"])


class CanonicalRepairContract(unittest.TestCase):
    def test_plan_and_ledger_payload_canonicalize_finding_order(self):
        first = {"id": "a", "summary": "first"}
        second = {"id": "b", "summary": "second"}
        escalated = {"id": "human", "class": "escalate"}
        decision = {
            "state": "blocked",
            "route": "remediation",
            "reason_code": "changes_requested",
            "desired_role": "remediation",
            "board_projection": "Blocked",
            "acceptance_findings": [second, first],
            "escalated_findings": [escalated],
        }
        snapshot = {
            "task_id": "COORD-41",
            "pr_number": 810,
            "head_sha": HEAD,
            "runner": {"live": False},
        }
        run = durable_run()
        plan = effects.plan_effect(decision, snapshot, run)
        self.assertEqual(
            [row["id"] for row in plan["acceptance_findings"]],
            ["a", "b"],
        )

        # Dirty caller order must not alter the ledger payload hash either.
        dirty_plan = {
            **plan,
            "acceptance_findings": [second, first],
        }
        adapters = CompletionEffectAdapters(
            start_remediation=lambda _value: {"action": "started"},
        )
        with (
            patch(
                "switchboard.domain.completion.executor._persist_run",
                return_value=dict(run),
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "claim_external_effect",
                return_value={"claimed": True, "effect_key": "effect-canonical"},
            ) as claim,
            patch(
                "switchboard.storage.repositories.external_effects."
                "verify_external_effect",
                return_value={"effect_key": "effect-canonical"},
            ),
        ):
            execute_effect(
                dirty_plan,
                decision=decision,
                snapshot=snapshot,
                run=run,
                project="switchboard",
                actor="owner",
                adapters=adapters,
            )
        payload = claim.call_args.args[3]
        self.assertEqual(
            [row["id"] for row in payload["acceptance_findings"]],
            ["a", "b"],
        )


class ProductionStartIdentity(unittest.TestCase):
    def test_start_adapter_forwards_completion_round_identity(self):
        class Store:
            @staticmethod
            def get_project_github_repo(_project):
                return "example/projectplanner"

        with (
            patch(
                "switchboard.storage.repositories.provenance._github_token",
                return_value="token",
            ),
            patch(
                "switchboard.application.commands.task_execution.start_task",
                return_value={"action": "starting"},
            ) as start,
        ):
            adapters = completion_driver.production_effect_adapters(
                project="switchboard",
                actor="owner",
                agent_id="owner",
                store_mod=Store,
            )
            result = adapters.start_remediation({
                "task_id": "COORD-41",
                "role": "remediation",
                "head_sha": HEAD,
                "reason_code": "required_ci_failed",
                "route": "remediation",
                "acceptance_findings": [{"id": "ci"}],
                "decision_attempt": 4,
                "state_version": 9,
            })

        self.assertEqual(result["action"], "starting")
        self.assertEqual(start.call_args.kwargs["decision_attempt"], 4)
        self.assertEqual(start.call_args.kwargs["state_version"], 9)


if __name__ == "__main__":
    unittest.main()
