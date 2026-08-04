#!/usr/bin/env python3
"""COORD-126: a remediation escalation is terminal to the pager."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT as _ROOT  # noqa: F401


_TMP = Path(tempfile.mkdtemp(prefix="coord126-human-escalation-"))
os.environ["PM_DB_PATH"] = str(_TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(_TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(_TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(_TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(_TMP / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_REVIEW_REMEDIATION_MAX_ROUNDS"] = "1"

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.application.commands import mission_journal as mission_journal_commands  # noqa: E402
from switchboard.application.commands import review_verdicts  # noqa: E402
from switchboard.application.mission_bot_v4.worker import (  # noqa: E402
    ScopedMissionWorkerPorts,
    tick_scoped_mission,
)
from switchboard.storage.repositories.mission_journal import (  # noqa: E402
    default_mission_journal_repository as journal,
)


PROJECT = "switchboard"
WORKER = "codex/coord126-worker"
REVIEWER = "codex/coord126-reviewer"
PR_URL = "https://github.test/6th-Element-Labs/projectplanner/pull/126"
HEAD_1 = "1" * 40
HEAD_2 = "2" * 40


def _finding(finding_id: str) -> dict[str, str]:
    return {
        "id": finding_id,
        "location": "src/switchboard/domain/example.py:42",
        "category": "evidence_integrity",
        "severity": "high",
        "invariant_violated": "The reviewed boundary remains fail closed.",
        "repair_requirement": "Apply the exact bounded repair and test it.",
        "class": "auto",
        "state": "open",
    }


def _verdict(task_id: str, head_sha: str, finding_id: str) -> dict[str, object]:
    return {
        "task_id": task_id,
        "pr_url": PR_URL,
        "head_sha": head_sha,
        "reviewer_principal": REVIEWER,
        "review_mode": "standard",
        "status": "changes_requested",
        "findings": [_finding(finding_id)],
    }


class HumanEscalationStopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        store.init_db(PROJECT)

    def _reviewable_task(self) -> str:
        task = store.create_task({
            "workstream_id": "COORD",
            "title": "COORD-126 bounded remediation fixture",
            "status": "Not Started",
            "ui_impact": "no",
        }, actor="coord126-test", project=PROJECT)
        task_id = str(task["task_id"])
        store.register_agent(
            WORKER, "codex", lane="COORD", task_id=task_id, project=PROJECT)
        store.register_agent(
            REVIEWER, "codex", lane="COORD", task_id=task_id, project=PROJECT)
        claim = store.claim_task(
            task_id, WORKER, principal_id="principal-coord126-worker",
            actor="coord126-test", project=PROJECT)
        with _conn(PROJECT) as connection:
            connection.execute(
                "UPDATE task_claims SET status='completed', completed_at=1 WHERE id=?",
                (claim["claim_id"],),
            )
            connection.execute(
                "UPDATE tasks SET status='In Review' WHERE task_id=?", (task_id,))
        store.mark_task_pr_opened(
            task_id, 126, PR_URL, branch=f"codex/{task_id}-fixture",
            head_sha=HEAD_1, actor="coord126-test", project=PROJECT)
        journal.create_mission(task_id, project=PROJECT, requested_role="review_merge")
        return task_id

    @staticmethod
    def _move_to_new_head(task_id: str) -> None:
        store.mark_task_pr_opened(
            task_id, 127, PR_URL, branch=f"codex/{task_id}-fixture",
            head_sha=HEAD_2, actor="coord126-test", project=PROJECT)
        with _conn(PROJECT) as connection:
            connection.execute(
                "UPDATE tasks SET status='In Review', assignee=NULL WHERE task_id=?",
                (task_id,),
            )

    @staticmethod
    def _register_review_runner(task_id: str) -> None:
        now = time.time()
        with _conn(PROJECT) as connection:
            connection.execute(
                "INSERT INTO runner_sessions("
                "runner_session_id,host_id,agent_id,runtime,task_id,claim_id,pid,"
                "status,cwd,control_json,metadata_json,last_snapshot_json,principal_id,"
                "started_at,heartbeat_at,heartbeat_ttl_s,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "run-coord126-review", "host-coord126", REVIEWER, "codex",
                    task_id, "claim-coord126-review", 1, "running", "", "{}",
                    json.dumps({
                        "execution_id": "exec-coord126-review",
                        "execution_generation": 2,
                        "execution_connection_id": "connection-coord126",
                        "execution_role": "review_merge",
                        "execution_head_sha": HEAD_2,
                    }),
                    "{}", "principal-coord126-review", now, now, 180, now,
                ),
            )
            connection.execute(
                "INSERT INTO resource_leases("
                "id,agent_id,principal_id,task_id,resource_type,names,claimed_at,"
                "ttl_seconds,released_at,execution_role,execution_generation,"
                "fence_epoch,lease_state,head_sha,wake_id"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "exec-coord126-review", REVIEWER, "principal-coord126-review",
                    task_id, "execution", task_id, now, 180, None, "review_merge", 2,
                    1, "active", HEAD_2, "wake-coord126-review",
                ),
            )

    def test_escalation_then_yield_can_only_surrender(self) -> None:
        task_id = self._reviewable_task()
        first = review_verdicts.execute_mapping(
            _verdict(task_id, HEAD_1, "COORD126-1"), actor=REVIEWER,
            principal_id="principal-coord126-review", project=PROJECT)
        self.assertEqual("queued", first["auto_remediation"]["status"])

        self._move_to_new_head(task_id)
        escalated = review_verdicts.execute_mapping(
            _verdict(task_id, HEAD_2, "COORD126-2"), actor=REVIEWER,
            principal_id="principal-coord126-review", project=PROJECT)
        remediation = escalated["auto_remediation"]
        self.assertEqual("escalated", remediation["status"])
        self.assertEqual("HUMAN", remediation["mission_human_hold"]["state"])
        events = journal.list_events(task_id, project=PROJECT, after_sequence=0)
        human_events = [event for event in events if event["event_type"] == "human_requested"]
        self.assertEqual(1, len(human_events))
        self.assertEqual(
            f"review-remediation:{remediation['remediation_id']}",
            human_events[0]["external_ref"],
        )

        self._register_review_runner(task_id)
        before = journal.get_item(task_id, project=PROJECT)
        with patch.object(
                mission_journal_commands.runner_repository,
                "make_runner_lease_due",
                return_value={"updated": True, "idempotent": False},
        ) as surrender:
            yielded = mission_journal_commands.yield_mission(
                task_id, project=PROJECT, execution_id="exec-coord126-review",
                generation=2, observed_through=int(before["latest_sequence"]),
                outcome="continue", requested_role="remediation", actor=REVIEWER,
                head_sha=HEAD_2)
        surrender.assert_called_once()
        self.assertEqual("completion_owner", surrender.call_args.kwargs["authority"])
        self.assertTrue(yielded["surrender_requested"])
        after = journal.get_item(task_id, project=PROJECT)
        self.assertTrue(yielded["created"])
        self.assertEqual("HUMAN", yielded["state"])
        self.assertEqual("HUMAN", after["state"])
        self.assertEqual(after["latest_sequence"], after["handled_through"])

        starts: list[dict[str, object]] = []
        tick = tick_scoped_mission(
            task_id, project=PROJECT, scope_authority={"generation": 1},
            actor="coord126-test",
            ports=ScopedMissionWorkerPorts(
                validate_scope=lambda _scope, **_kwargs: {"allowed": True},
                get_task=lambda _task_id, **_kwargs: {
                    "dependency_state": {"satisfied": True},
                },
                has_live_execution=lambda _task_id, **_kwargs: False,
                start_task=lambda _task_id, **kwargs: starts.append(kwargs) or {
                    "started": True,
                },
                journal=journal,
            ),
        )
        self.assertEqual("wait", tick["action"])
        self.assertEqual("authenticated_agent_request", tick["reason"])
        self.assertEqual([], starts)

        replay = journal.yield_execution(
            task_id, project=PROJECT, execution_id="exec-coord126-review",
            generation=2, observed_through=int(before["latest_sequence"]),
            outcome="continue", requested_role="remediation", actor=REVIEWER,
            head_sha=HEAD_2)
        self.assertFalse(replay["created"])
        self.assertEqual("HUMAN", replay["state"])


if __name__ == "__main__":
    unittest.main()
