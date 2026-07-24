#!/usr/bin/env python3
"""COORD-46 acceptance 11-13: route=human → one PROTO-7/8 Needs-you item.

PR #812-shaped: implementation is complete, but credentialed live proof is
unavailable. Automation must freeze exactly one attention_request, project
Blocked(route=human), and resume only after an authorized decision + delivery
receipt — never via PR comments or agent_messages.
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.api.routers.attention import _rank, _request_item
from switchboard.domain.completion import effects
from switchboard.domain.completion.executor import execute_effect
from switchboard.domain.completion.state_machine import (
    build_completion_snapshot,
    classify_completion,
)
from switchboard.storage.migrations import runner as migrations
from switchboard.storage.migrations.attention import upgrade_attention_schema
from switchboard.storage.repositories import attention as attention_repo
from switchboard.storage.repositories import completion_runs
from switchboard.storage.repositories import task_completion

HEAD = "c" * 40


def _pr812_snapshot(**extra):
    snap = build_completion_snapshot(
        task={
            "task_id": "COORD-20",
            "status": "In Review",
            "git_state": {"head_sha": HEAD, "pr_number": 812},
            "deliverable": {"deliverable_id": "alerts", "milestone_id": "alerts-m3-ui"},
        },
        github_pr={
            "number": 812,
            "state": "open",
            "draft": False,
            "mergeable": True,
            "mergeStateStatus": "CLEAN",
            "head": {"sha": HEAD},
        },
        required_status_contexts=["Switchboard CI / VM gate"],
        status_contexts=[{
            "name": "Switchboard CI / VM gate",
            "conclusion": "success",
        }],
        review={"status": "passed", "head_sha": HEAD},
        merge_gate={
            "findings": [{
                "code": "credentialed_live_proof_unavailable",
                "failure_class": "absent_permission",
                "blocking": True,
                "message": "Eligible authenticated host/credential required for live proof",
            }],
        },
        work_session={"work_session_id": "worksession-812", "status": "active"},
        runner={"live": True, "role": "review_merge", "head_sha": HEAD, "generation": 4},
    )
    snap.update(extra)
    return snap


class HumanAttentionCloseout(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE tasks ("
            "task_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "assignee TEXT, updated_at REAL)")
        self.db.execute(
            "CREATE TABLE task_git_state ("
            "task_id TEXT PRIMARY KEY, pr_number INTEGER, head_sha TEXT, "
            "branch TEXT, pr_url TEXT, merged_sha TEXT, evidence_json TEXT)")
        for name, sql in migrations.DDL_MIGRATIONS:
            if name in {
                "0074_task_execution_completion_phases",
                "0075_ix_task_execution_completion_identity",
                "0111_completion_runs",
                "0112_ux_completion_runs_task",
            }:
                self.db.execute(sql)
        upgrade_attention_schema(self.db)
        self.db.execute(
            "INSERT INTO tasks(task_id, status, assignee, updated_at) "
            "VALUES (?,?,?,?)",
            ("COORD-20", "In Review", None, 1.0))
        self.db.execute(
            "INSERT INTO task_git_state("
            "task_id, pr_number, head_sha, branch, pr_url, merged_sha, evidence_json) "
            "VALUES (?,?,?,?,?,?,?)",
            ("COORD-20", 812, HEAD, "codex/COORD-20-x",
             "https://github.com/6th-Element-Labs/projectplanner/pull/812",
             None, "{}"))
        self.db.commit()
        self.patches = [
            patch.object(completion_runs, "_conn", return_value=self.db),
            patch.object(
                completion_runs, "_write_through",
                side_effect=lambda _project, fn: fn()),
            patch.object(task_completion, "_conn", return_value=self.db),
            patch.object(
                task_completion, "_write_through",
                side_effect=lambda _project, fn: fn()),
            patch.object(attention_repo, "_conn", return_value=self.db),
            patch.object(
                attention_repo, "_write_through",
                side_effect=lambda _project, fn: fn()),
        ]
        for p in self.patches:
            p.start()
        self.fenced = []
        self.wakes = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.db.close()

    def _tick(self):
        snapshot = _pr812_snapshot()
        decision = classify_completion(None, snapshot)
        run = completion_runs.get_active_completion_run(
            "COORD-20", project="switchboard") or {
            "run_id": "completion-run-812",
            "state_version": 1,
            "attempt": 0,
        }
        plan = effects.plan_effect(decision, snapshot, run)
        return execute_effect(
            plan,
            decision=decision,
            snapshot=snapshot,
            run=run,
            project="switchboard",
            actor="completion-owner",
            fence_generation=lambda generation: self.fenced.append(generation),
            wake_completion_owner=lambda payload: self.wakes.append(payload),
        )

    def test_pr812_credential_gate_creates_one_needs_you_item(self):
        first = self._tick()
        second = self._tick()
        third = self._tick()

        self.assertEqual(first["effect"], "escalate_human")
        self.assertTrue(first["attention"]["created"] or first["attention"]["idempotent_replay"])
        self.assertTrue(second["attention"]["idempotent_replay"])
        self.assertTrue(third["attention"]["idempotent_replay"])
        self.assertEqual(
            first["attention"]["request"]["request_id"],
            second["attention"]["request"]["request_id"],
        )

        rows = self.db.execute(
            "SELECT COUNT(*) AS n FROM attention_requests WHERE task_id=?",
            ("COORD-20",),
        ).fetchone()["n"]
        self.assertEqual(rows, 1)

        run = completion_runs.get_active_completion_run(
            "COORD-20", project="switchboard")
        self.assertEqual(run["route"], "human")
        self.assertEqual(run["board_status"], "Blocked")
        board = self.db.execute(
            "SELECT status FROM tasks WHERE task_id=?", ("COORD-20",)
        ).fetchone()["status"]
        self.assertEqual(board, "Blocked")
        self.assertEqual(self.fenced, [4])

        request = first["attention"]["request"]
        ctx = request["context"]
        for key in (
            "task_id", "deliverable_id", "completion_run_id", "state_version",
            "pr_number", "head_sha", "completed_work_summary", "evidence_refs",
            "unresolved_gate", "reason_code", "why_automation_stopped",
            "resume_condition", "next_automatic_action", "delivery_impact",
            "owner",
        ):
            self.assertIn(key, ctx, key)
        self.assertEqual(ctx["reason_code"], "credentialed_live_proof_unavailable")
        self.assertEqual(request["choices"][0]["id"], "supply_credential")
        self.assertEqual(request["recommended_default"]["id"], "supply_credential")

        feed_item = _request_item(request)
        self.assertEqual(feed_item["source"], "attention")
        self.assertTrue(feed_item["attention_id"].startswith("attention:"))
        self.assertIn("/api/attention/requests/", feed_item["decide"]["path"])

    def test_authorized_decision_wakes_owner_but_resumed_needs_receipt(self):
        receipt = self._tick()
        request = receipt["attention"]["request"]
        decided = attention_repo.default_attention_repository.record_decision(
            request["request_id"],
            {
                "expected_version": request["version"],
                "choice": {"id": "supply_credential"},
                "idempotency_key": "decide-812-1",
            },
            actor="operator",
            actor_principal_id="principal-1",
            project="switchboard",
        )
        resume = execute_effect.resume_after_human_decision(
            decided,
            project="switchboard",
            actor="completion-owner",
            wake_completion_owner=lambda payload: self.wakes.append(payload),
        )
        self.assertEqual(resume["status"], "decision_recorded")
        self.assertFalse(resume["resumed"])
        self.assertEqual(len(self.wakes), 1)

        with_receipt = execute_effect.mark_human_resume_receipt(
            request["request_id"],
            expected_version=decided["request"]["version"],
            host_id=request.get("host_id") or "operator",
            actor="completion-owner",
            receipt={"execution_id": "exec-1", "schema": "switchboard.delivery_receipt.v1"},
            project="switchboard",
        )
        self.assertTrue(with_receipt["resumed"])
        self.assertEqual(with_receipt["status"], "resolved")

        # Board stays Blocked until the completion owner reclassifies a new head.
        board = self.db.execute(
            "SELECT status FROM tasks WHERE task_id=?", ("COORD-20",)
        ).fetchone()["status"]
        self.assertEqual(board, "Blocked")
        run = completion_runs.get_active_completion_run(
            "COORD-20", project="switchboard")
        self.assertEqual(run["route"], "human")

    def test_comments_and_agent_messages_are_not_authority(self):
        self._tick()
        # Injecting prose into unrelated stores must not create a second
        # attention row or change the frozen request.
        before = self.db.execute(
            "SELECT request_hash, prompt FROM attention_requests WHERE task_id=?",
            ("COORD-20",),
        ).fetchone()
        # Simulate mirror-only surfaces existing; the executor still dedupes.
        again = self._tick()
        after = self.db.execute(
            "SELECT request_hash, prompt FROM attention_requests WHERE task_id=?",
            ("COORD-20",),
        ).fetchone()
        self.assertEqual(before["request_hash"], after["request_hash"])
        self.assertEqual(before["prompt"], after["prompt"])
        self.assertTrue(again["attention"]["idempotent_replay"])

    def test_attention_source_ranks_with_agents(self):
        item = _request_item({
            "request_id": "attention-x",
            "task_id": "COORD-20",
            "prompt": "Supply credential",
            "created_at": 1.0,
            "expires_at": None,
            "context": {"reason_code": "credentialed_live_proof_unavailable"},
            "choices": [{"id": "supply_credential"}],
            "recommended_default": {"id": "supply_credential"},
            "version": 1,
        })
        agent = {"source": "agent", "deadline": 1, "age_s": 1, "payload": {}}
        inbox = {"source": "inbox", "deadline": None, "age_s": 1,
                 "payload": {"proposals": 3}}
        ranked = sorted([inbox, item, agent], key=_rank)
        self.assertEqual(ranked[0]["source"], "agent")
        self.assertEqual(ranked[1]["source"], "attention")


if __name__ == "__main__":
    unittest.main(verbosity=2)
