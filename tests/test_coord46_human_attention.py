#!/usr/bin/env python3
"""COORD-46 attention closeout: an explicit agent request → one Needs-you item.

Factory findings never create operator attention. Once an authenticated LLM
explicitly invokes ``agent_requires_human``, automation must freeze exactly one
attention_request and resume only after an authorized decision + delivery
receipt — never via PR comments or agent_messages.
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.api.routers.attention import _provider_item
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
PR_812 = "https://github.com/6th-Element-Labs/projectplanner/pull/812"


def _pr812_snapshot(**extra):
    snap = build_completion_snapshot(
        task={
            "task_id": "COORD-20",
            "status": "In Review",
            "git_state": {
                "head_sha": HEAD, "pr_number": 812, "pr_url": PR_812,
            },
            "deliverable": {"deliverable_id": "alerts", "milestone_id": "alerts-m3-ui"},
        },
        github_pr={
            "number": 812,
            "url": PR_812,
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
        review={"status": "passed", "head_sha": HEAD, "pr_url": PR_812},
        merge_gate={
            "findings": [{
                "code": "credentialed_live_proof_unavailable",
                "failure_class": "absent_permission",
                "blocking": True,
                "message": "Eligible authenticated host/credential required for live proof",
            }],
        },
        work_session={
            "work_session_id": "worksession-812",
            "status": "blocked",
            "hygiene": {
                "blocker": {
                    "source_tool": "agent_requires_human",
                    "binding": "registered_agent",
                    "agent_id": "agent-812",
                    "reason": "credentialed_live_proof_unavailable",
                    "question": "Please provide the required credential.",
                },
            },
        },
        runner={
            "live": True,
            "runner_session_id": "runner-812",
            "execution_id": "execution-812",
            "execution_connection_id": "connection-812",
            "generation": 4,
            "fence_epoch": 7,
            "role": "review_merge",
            "head_sha": HEAD,
        },
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
        self.db.execute(
            "CREATE TABLE autopilot_scopes ("
            "scope_id TEXT PRIMARY KEY, status TEXT, lease_id TEXT, "
            "holder_agent_id TEXT, generation INTEGER, fence_epoch INTEGER, "
            "expires_at REAL, scope_type TEXT, task_project TEXT, task_id TEXT, "
            "deliverable_id TEXT)")
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

    def test_agent_request_stops_without_synthesizing_another_attention_item(self):
        snapshot = _pr812_snapshot()
        decision = classify_completion(None, snapshot)
        plan = effects.plan_effect(
            decision,
            snapshot,
            {"run_id": "completion-run-812", "state_version": 1, "attempt": 0},
        )
        self.assertEqual(decision["route"], "human")
        self.assertEqual(plan["effect"], "agent_requires_human")
        self.assertEqual(self.fenced, [])
        # The explicit MCP call owns creation of the Needs-you receipt.
        # Observing its sticky flag must not manufacture a second request.
        rows = self.db.execute(
            "SELECT COUNT(*) AS n FROM attention_requests WHERE task_id=?",
            ("COORD-20",),
        ).fetchone()["n"]
        self.assertEqual(rows, 0)

    def test_machine_gate_without_agent_receipt_boots_remediation(self):
        snapshot = _pr812_snapshot()
        snapshot["work_session"] = {
            "work_session_id": "worksession-812",
            "status": "active",
        }
        snapshot["runner"] = {"live": False}
        decision = classify_completion(None, snapshot)
        self.assertEqual(decision["route"], "remediation")
        self.assertEqual(
            decision["reason_code"],
            "credentialed_live_proof_unavailable",
        )

    def test_comments_and_agent_messages_are_not_authority(self):
        snapshot = _pr812_snapshot()
        snapshot["work_session"] = {
            "status": "blocked",
            "hygiene": {"blocker": {
                "route": "human",
                "reason": "comment_says_needs_you",
                "actor": "untrusted-prose",
            }},
        }
        snapshot["runner"] = {"live": False}
        self.assertEqual(
            classify_completion(None, snapshot)["route"],
            "remediation",
        )

    def test_machine_classification_never_writes_attention(self):
        """Only the explicit MCP command may create the Needs-you request."""
        snapshot = _pr812_snapshot()
        snapshot["work_session"] = {"status": "active"}
        snapshot["runner"] = {"live": False}
        with patch.object(
            attention_repo,
            "create_attention_request_in",
            side_effect=RuntimeError("injected attention write failure"),
        ) as create_attention:
            decision = classify_completion(None, snapshot)
            plan = effects.plan_effect(
                decision,
                snapshot,
                {"run_id": "run-812", "state_version": 1},
            )
        self.assertEqual(plan["effect"], "start_remediation")
        create_attention.assert_not_called()

    def test_noncredential_human_reasons_offer_truthful_choices(self):
        cases = {
            "wrong_target_branch": "correct_target_branch",
            "canonical_repo_missing": "configure_canonical_repo",
            "pr_closed_unmerged": "reopen_pull_request",
            "review_retry_budget_exhausted": "resolve_finding",
            "human_review_findings": "resolve_finding",
        }
        from switchboard.domain.completion.human_closeout import (
            build_human_closeout_request,
        )
        for reason, expected_choice in cases.items():
            with self.subTest(reason=reason):
                request = build_human_closeout_request(
                    plan={
                        "task_id": "COORD-20", "pr_number": 812,
                        "head_sha": HEAD, "idem_key": f"human:{reason}",
                        "reason_code": reason,
                    },
                    decision={"reason_code": reason},
                    snapshot=_pr812_snapshot(),
                    run={"run_id": "run", "state_version": 1},
                )
                self.assertEqual(request["choices"][0]["id"], expected_choice)
                if "credential" not in reason:
                    self.assertNotEqual(
                        request["choices"][0]["id"], "supply_credential")

    def test_completion_closeout_projects_as_blocking_provider_item(self):
        item = _provider_item({
            "request_id": "attention-x",
            "task_id": "COORD-20",
            "provider": "switchboard.completion",
            "prompt": "Supply credential",
            "created_at": 1.0,
            "expires_at": None,
            "context": {"reason_code": "credentialed_live_proof_unavailable"},
            "choices": [{"id": "supply_credential"}],
            "recommended_default": {"id": "supply_credential"},
            "version": 1,
        })
        self.assertEqual(item["source"], "provider")
        self.assertEqual(item["delivery_impact"], "blocking")
        self.assertEqual(item["decide"]["body"]["expected_version"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
