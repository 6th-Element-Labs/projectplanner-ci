#!/usr/bin/env python3
"""COORD-46 + Mission Bot: humans only from stamped agent_requires_human.

Pre-cutover, a credential merge-gate finding invented ``escalate_human`` and a
Needs-you item. Mission Bot forbids that: machine red boots remediation (or
waits on a live runner). Only a server-stamped ``agent_requires_human`` /
``record_human_blocker`` receipt may project ``route=human`` / Blocked.

Needs-you attention is authored by the agent MCP write path, not by the
Mission Bot inventing a closeout from classifier-era finding codes.
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
from switchboard.domain.mission_bot import reduce_mission
from switchboard.domain.mission_bot.outputs import MissionOutput
from switchboard.storage.migrations import runner as migrations
from switchboard.storage.migrations.attention import upgrade_attention_schema
from switchboard.storage.repositories import attention as attention_repo
from switchboard.storage.repositories import completion_runs
from switchboard.storage.repositories import task_completion

HEAD = "c" * 40
PR_812 = "https://github.com/6th-Element-Labs/projectplanner/pull/812"


def _agent_blocker(**extra):
    return {
        "route": "agent_requires_human",
        "reason": "credentialed_live_proof_unavailable",
        "source_tool": "agent_requires_human",
        "binding": "registered_agent",
        "provenance_stamp": "switchboard.resolve_write_actor.v1",
        "agent_id": "agent-812",
        "actor": "agent-812",
        "execution_id": "execution-812",
        "execution_generation": 4,
        **extra,
    }


def _pr812_snapshot(*, live_runner: bool = False, stamped_human: bool = False,
                    credential_finding: bool = True):
    findings = []
    if credential_finding and not stamped_human:
        findings.append({
            "code": "credentialed_live_proof_unavailable",
            "failure_class": "absent_permission",
            "blocking": True,
            "message": "Eligible authenticated host/credential required for live proof",
        })
    blocker = _agent_blocker() if stamped_human else None
    task = {
        "task_id": "COORD-20",
        "status": "In Review",
        "git_state": {
            "head_sha": HEAD, "pr_number": 812, "pr_url": PR_812,
        },
        "deliverable": {"deliverable_id": "alerts", "milestone_id": "alerts-m3-ui"},
    }
    if blocker:
        task["human_blocker"] = blocker
    work_session = {"work_session_id": "worksession-812", "status": "active"}
    if blocker:
        work_session = {
            "work_session_id": "worksession-812",
            "status": "blocked",
            "hygiene": {"blocker": blocker},
        }
    return build_completion_snapshot(
        task=task,
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
        merge_gate={"findings": findings},
        work_session=work_session,
        runner={
            "live": live_runner,
            "runner_session_id": "runner-812",
            "execution_id": "execution-812",
            "execution_connection_id": "connection-812",
            "generation": 4,
            "fence_epoch": 7,
            "role": "review_merge",
            "head_sha": HEAD,
        },
    )


class MissionBotHumanAttention(unittest.TestCase):
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

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.db.close()

    def _legacy_tick(self, snapshot):
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
        )

    def test_credential_finding_with_live_runner_waits(self):
        snap = _pr812_snapshot(live_runner=True, credential_finding=True)
        cmd = reduce_mission(snap)
        self.assertEqual(cmd["output"], MissionOutput.WAIT.value)
        self.assertEqual(cmd["reason_code"], "live_runner_in_progress")
        decision = classify_completion(None, snap)
        self.assertEqual(decision["route"], "wait")
        self.assertNotEqual(decision["route"], "human")

    def test_credential_finding_without_live_runner_remediates(self):
        """Machine red never invents escalate_human / Needs-you."""
        snap = _pr812_snapshot(live_runner=False, credential_finding=True)
        cmd = reduce_mission(snap)
        self.assertEqual(cmd["output"], MissionOutput.START_REMEDIATION.value)
        self.assertEqual(
            cmd["reason_code"], "credentialed_live_proof_unavailable")
        decision = classify_completion(None, snap)
        plan = effects.plan_effect(
            decision, snap,
            {"run_id": "completion-run-812", "state_version": 1, "attempt": 0},
        )
        self.assertEqual(plan["effect"], "start_remediation")
        self.assertNotEqual(plan["effect"], "escalate_human")
        self.assertNotEqual(plan["route"], "human")
        rows = self.db.execute(
            "SELECT COUNT(*) AS n FROM attention_requests WHERE task_id=?",
            ("COORD-20",),
        ).fetchone()["n"]
        self.assertEqual(rows, 0)

    def test_stamped_agent_requires_human_projects_blocked_human_route(self):
        snap = _pr812_snapshot(
            live_runner=False, stamped_human=True, credential_finding=False)
        cmd = reduce_mission(snap)
        self.assertEqual(cmd["output"], MissionOutput.AGENT_REQUIRES_HUMAN.value)
        self.assertEqual(
            cmd["reason_code"], "credentialed_live_proof_unavailable")
        decision = classify_completion(None, snap)
        self.assertEqual(decision["route"], "human")
        self.assertEqual(decision["board_projection"], "Blocked")
        self.assertEqual(decision["effect"], "agent_requires_human")
        plan = effects.plan_effect(
            decision, snap,
            {"run_id": "completion-run-812", "state_version": 1, "attempt": 0},
        )
        # Planner keeps the agent-authored sticky name; it does not mint
        # escalate_human from machine finding codes.
        self.assertEqual(plan["effect"], "agent_requires_human")
        self.assertEqual(plan["route"], "human")

    def test_forged_human_route_without_binding_does_not_stop(self):
        snap = _pr812_snapshot(live_runner=False, credential_finding=True)
        snap["work_session"] = {
            "status": "blocked",
            "hygiene": {
                "blocker": {
                    "route": "human",
                    "reason": "credentialed_live_proof_unavailable",
                    "actor": "not-an-agent",
                }
            },
        }
        cmd = reduce_mission(snap)
        self.assertEqual(cmd["output"], MissionOutput.START_REMEDIATION.value)
        self.assertNotEqual(cmd["output"], MissionOutput.AGENT_REQUIRES_HUMAN.value)

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
