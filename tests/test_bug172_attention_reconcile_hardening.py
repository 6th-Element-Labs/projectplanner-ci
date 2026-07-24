#!/usr/bin/env python3
"""BUG-172 adversarial proofs for replay, provider trust, and repair sweeps."""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.api.routers.attention import _provider_item
from switchboard.application.attention import AttentionService
from switchboard.domain.projects.context import ProjectContext
from switchboard.storage.migrations.attention import upgrade_attention_schema
from switchboard.storage.repositories import attention as attention_repo
from switchboard.storage.repositories import review_remediations


PROJECT = "switchboard"
HEAD = "a" * 40


class DecisionReplayAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE task_git_state ("
            "task_id TEXT PRIMARY KEY, head_sha TEXT, pr_number INTEGER)"
        )
        self.db.execute(
            "INSERT INTO task_git_state(task_id,head_sha,pr_number) "
            "VALUES ('COORD-46', ?, 825)",
            (HEAD,),
        )
        upgrade_attention_schema(self.db)

    def tearDown(self) -> None:
        self.db.close()

    def test_committed_decision_replay_precedes_expiry_and_mutable_bindings(self):
        request = attention_repo.create_attention_request_in(
            self.db,
            {
                "provider": "provider-a",
                "provider_request_id": "question-1",
                "schema_version": "provider.question.v1",
                "prompt": "Continue?",
                "choices": [{"id": "continue"}, {"id": "hold"}],
                "idempotency_key": "request-1",
                "task_id": "COORD-46",
                "context": {"head_sha": HEAD, "pr_number": 825},
                "expires_at": 200.0,
            },
            project=PROJECT,
            actor="provider-a",
            now=100.0,
        )["request"]
        decision = {
            "expected_version": 1,
            "choice": {"id": "continue"},
            "idempotency_key": "decision-1",
        }
        first = attention_repo.record_attention_decision_in(
            self.db,
            request["request_id"],
            decision,
            actor="operator",
            actor_principal_id="principal/operator",
            project=PROJECT,
            now=150.0,
        )
        self.db.execute(
            "UPDATE task_git_state SET head_sha=?, pr_number=826 "
            "WHERE task_id='COORD-46'",
            ("b" * 40,),
        )

        replay = attention_repo.record_attention_decision_in(
            self.db,
            request["request_id"],
            decision,
            actor="operator",
            actor_principal_id="principal/operator",
            project=PROJECT,
            now=300.0,
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(
            replay["decision"]["decision_id"],
            first["decision"]["decision_id"],
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM attention_decisions WHERE request_id=?",
                (request["request_id"],),
            ).fetchone()[0],
            1,
        )

        changed = {**decision, "choice": {"id": "hold"}}
        with self.assertRaises(attention_repo.AttentionStoreError) as conflict:
            attention_repo.record_attention_decision_in(
                self.db,
                request["request_id"],
                changed,
                actor="operator",
                actor_principal_id="principal/operator",
                project=PROJECT,
                now=300.0,
            )
        self.assertEqual(
            conflict.exception.code,
            "attention_decision_idempotency_conflict",
        )


class CompletionProviderTrustTest(unittest.TestCase):
    def test_completion_namespace_is_reserved_at_external_ingress(self):
        class Repository:
            def create_request(self, *_args, **_kwargs):
                raise AssertionError("reserved provider reached persistence")

        service = AttentionService(repository=Repository())
        context = ProjectContext(
            project_id=PROJECT,
            source="test",
            principal_id="principal/host",
        )
        for provider in (
            attention_repo.COMPLETION_PROVIDER,
            f"{attention_repo.COMPLETION_PROVIDER}.fake",
        ):
            with self.subTest(provider=provider):
                with self.assertRaises(
                    attention_repo.AttentionStoreError
                ) as rejected:
                    service.upsert_request(
                        context,
                        {"provider": provider},
                        actor="agent-host",
                    )
                self.assertEqual(
                    rejected.exception.code,
                    "attention_completion_owner_required",
                )

    def test_ui_trusts_only_exact_completion_provider_and_schema(self):
        base = {
            "request_id": "attention-1",
            "provider": attention_repo.COMPLETION_PROVIDER,
            "schema_version": attention_repo.COMPLETION_CLOSEOUT_SCHEMA,
            "prompt": "Resolve blocker",
            "context": {},
            "version": 1,
        }
        self.assertEqual(_provider_item(base)["kind"], "completion_human")
        self.assertEqual(
            _provider_item({
                **base,
                "provider": f"{attention_repo.COMPLETION_PROVIDER}.fake",
            })["kind"],
            "provider_request",
        )
        self.assertEqual(
            _provider_item({
                **base,
                "schema_version": "provider.question.v1",
            })["kind"],
            "provider_request",
        )


class RepairReconcileMalformedStateTest(unittest.TestCase):
    def test_malformed_agent_state_cannot_abort_or_enter_the_sweep(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                agent_state TEXT,
                updated_at REAL NOT NULL
            );
            CREATE TABLE activity (
                task_id TEXT,
                actor TEXT,
                kind TEXT,
                payload TEXT,
                created_at REAL
            );
            """
        )
        db.executemany(
            "INSERT INTO tasks(task_id,agent_state,updated_at) VALUES (?,?,?)",
            [
                ("MALFORMED-1", "{not-json", 1.0),
                (
                    "READY-1",
                    '{"review_repair":{"status":"linked"}}',
                    2.0,
                ),
            ],
        )
        repository = review_remediations.ReviewRemediationRepository()
        with (
            patch.object(review_remediations, "_conn", return_value=db),
            patch.object(
                review_remediations,
                "_write_through",
                side_effect=lambda _project, fn: fn(),
            ),
            patch.object(
                repository,
                "resolve_cross_task_repair",
                return_value={
                    "repair_task_id": "READY-1",
                    "status": "blocked",
                    "reason": "fixture",
                },
            ) as resolve,
        ):
            result = repository.reconcile_cross_task_repairs(
                project=PROJECT,
                actor="reconcile-test",
            )

        self.assertEqual(result["checked"], 1)
        resolve.assert_called_once_with(
            "READY-1",
            actor="reconcile-test",
            project=PROJECT,
        )
        self.assertEqual(
            db.execute(
                "SELECT agent_state FROM tasks WHERE task_id='MALFORMED-1'"
            ).fetchone()[0],
            "{not-json",
        )
        db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
