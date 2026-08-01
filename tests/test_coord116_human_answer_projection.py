#!/usr/bin/env python3
"""COORD-116: accepted Human decisions resume one exact staged v4 mission."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from path_setup import ROOT as _ROOT  # noqa: F401

_TMP = Path(tempfile.mkdtemp(prefix="coord116-human-answer-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(_TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(_TMP / "registry.db")
(_TMP / "projects").mkdir()
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(_TMP / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.application.attention import (  # noqa: E402
    default_attention_service,
)
from switchboard.application.commands import human_blocker  # noqa: E402
from switchboard.application.mission_bot_v4.worker import (  # noqa: E402
    ScopedMissionWorkerPorts,
    tick_scoped_mission,
)
from switchboard.domain.projects.context import ProjectContext  # noqa: E402
from switchboard.storage.repositories.attention import (  # noqa: E402
    AttentionStoreError,
)
from switchboard.storage.repositories.mission_journal import (  # noqa: E402
    default_mission_journal_repository as journal,
)

PROJECT = "switchboard"
HEAD = "a" * 40
OPERATOR = ProjectContext(
    project_id=PROJECT,
    source="test",
    principal_id="principal/operator",
    effective_scopes=("write:ixp",),
)


class HumanAnswerProjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        store.init_db(PROJECT)

    def make_task_and_session(self, title: str, *, head_sha: str = HEAD) -> tuple[str, str]:
        task = store.create_task({
            "workstream_id": "COORD",
            "title": title,
            "description": "session_profile:code_strict\nCOORD-116 fixture",
            "status": "Not Started",
            "ui_impact": "no",
        }, actor="test", project=PROJECT)
        task_id = str(task["task_id"])
        with _conn(PROJECT) as connection:
            connection.execute(
                "UPDATE tasks SET status='In Progress', assignee=? WHERE task_id=?",
                ("agent/codex/coord116", task_id),
            )
        session = store.create_work_session({
            "agent_id": "agent/codex/coord116",
            "task_id": task_id,
            "repo_role": "canonical",
            "storage_mode": "worktree",
            "worktree_path": str(_TMP / f"wt-{task_id}"),
            "branch": f"codex/{task_id}-human-answer",
            "upstream": f"origin/codex/{task_id}-human-answer",
            "base_sha": head_sha,
            "head_sha": head_sha,
            "status": "active",
            "dirty_status": "clean",
            "policy_profile": "code_strict",
        }, actor="agent/codex/coord116", project=PROJECT)
        self.assertTrue(session.get("created"), session)
        return task_id, str(session["work_session"]["work_session_id"])

    def park(self, title: str, *, staged: bool = True,
             head_sha: str = HEAD) -> tuple[str, str, dict]:
        task_id, session_id = self.make_task_and_session(title, head_sha=head_sha)
        if staged:
            item = journal.ensure_item(task_id, project=PROJECT)
            journal.update_item(
                task_id,
                project=PROJECT,
                state="ACTIVE",
                requested_role="implementation",
                expected_version=int(item["version"]),
                handled_through=1,
            )
        parked = human_blocker.execute_mapping({
            "task_id": task_id,
            "work_session_id": session_id,
            "reason": "credential_missing",
            "completed_work": "Stopped before inventing missing authority.",
            "minimum_human_action": "Supply the missing authority.",
            "resume_condition": "An authenticated operator decision is recorded.",
            "next_automatic_step": "Re-assess the same task.",
            "binding": "registered_agent",
            "agent_id": "agent/codex/coord116",
            "source_tool": "agent_requires_human",
        }, actor="agent/codex/coord116", project=PROJECT)
        self.assertTrue(parked["recorded"], parked)
        with _conn(PROJECT) as connection:
            git_state = connection.execute(
                "SELECT head_sha FROM task_git_state WHERE task_id=?", (task_id,),
            ).fetchone()
        self.assertIsNone(git_state, "fixture must exercise the no-PR-head path")
        return task_id, session_id, parked

    def decide(self, parked: dict, choice_id: str, *, idem: str = "decision-1",
               context: ProjectContext = OPERATOR) -> dict:
        return default_attention_service.decide(
            context,
            str(parked["attention_request_id"]),
            {
                "expected_version": 1,
                "choice": {"id": choice_id},
                "idempotency_key": idem,
            },
            actor="operator",
        )

    def test_no_pr_resume_projects_one_answer_and_one_launch(self):
        task_id, _session_id, parked = self.park(
            "no-PR Human answer resumes staged mission",
        )
        request_id = str(parked["attention_request_id"])
        decided = self.decide(parked, "supply_credential")
        decision_id = str(decided["decision"]["decision_id"])

        self.assertEqual("resolved", decided["request"]["status"])
        self.assertEqual("resume_assessment", decided["request"]["delivery_receipt"]["effect"])
        self.assertNotIn("completion_wake", decided)
        self.assertEqual("ACTIVE", decided["mission"]["state"])
        self.assertEqual(3, decided["mission"]["answer_pointer"])
        item = journal.get_item(task_id, project=PROJECT)
        self.assertEqual(2, item["handled_through"])
        self.assertEqual(3, item["latest_sequence"])
        self.assertEqual("", item["human_request_id"])

        answered = [
            event for event in journal.list_events(
                task_id, project=PROJECT, after_sequence=0, limit=20,
            ) if event["event_type"] == "human_answered"
        ]
        self.assertEqual(1, len(answered))
        self.assertEqual("coordination", answered[0]["source_plane"])
        self.assertEqual(decision_id, answered[0]["external_ref"])
        self.assertEqual(request_id, answered[0]["payload"]["human_request_id"])
        self.assertEqual(decision_id, answered[0]["payload"]["answer_ref"])

        starts: list[dict] = []
        ports = ScopedMissionWorkerPorts(
            validate_scope=lambda _authority, **_kwargs: {"allowed": True},
            get_task=lambda _task_id, *, project: {
                "dependency_state": {"satisfied": True},
            },
            has_live_execution=lambda _task_id, *, project: False,
            start_task=lambda exact_task_id, **kwargs: starts.append({
                "task_id": exact_task_id, **kwargs,
            }) or {"action": "starting", "started": True},
            journal=journal,
        )
        first = tick_scoped_mission(
            task_id,
            project=PROJECT,
            scope_authority={"generation": 1},
            actor="test",
            ports=ports,
        )
        replay_tick = tick_scoped_mission(
            task_id,
            project=PROJECT,
            scope_authority={"generation": 1},
            actor="test",
            ports=ports,
        )
        self.assertEqual("start_task", first["action"])
        self.assertEqual(3, first["event_pointer"])
        self.assertEqual("human_answered", json.loads(starts[0]["instruction"])["event_type"])
        self.assertEqual("block_release", replay_tick["action"])
        self.assertEqual("missing_mission_event", replay_tick["reason"])
        self.assertTrue(replay_tick["release_blocked"])
        self.assertEqual(1, len(starts))

        replay = self.decide(parked, "supply_credential")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(1, len([
            event for event in journal.list_events(
                task_id, project=PROJECT, after_sequence=0, limit=20,
            ) if event["event_type"] == "human_answered"
        ]))

    def test_keep_blocked_records_decision_without_unparking_or_launching(self):
        task_id, _session_id, parked = self.park(
            "Human hold keeps staged mission parked",
        )
        decided = self.decide(parked, "hold")
        self.assertEqual("resolved", decided["request"]["status"])
        self.assertEqual("remain_blocked", decided["request"]["delivery_receipt"]["effect"])
        self.assertEqual("HUMAN", decided["mission"]["state"])
        self.assertEqual("human_hold_recorded", decided["mission"]["reason"])
        item = journal.get_item(task_id, project=PROJECT)
        self.assertEqual("HUMAN", item["state"])
        self.assertEqual([], [
            event for event in journal.list_events(
                task_id, project=PROJECT, after_sequence=0, limit=20,
            ) if event["event_type"] == "human_answered"
        ])

    def test_terminal_receipt_cleanup_does_not_invalidate_human_answer(self):
        task_id, session_id, parked = self.park(
            "terminalized Human session retains answer authority",
        )
        updated = store.update_work_session(
            session_id,
            {"status": "expired"},
            actor="host/test",
            project=PROJECT,
        )
        self.assertEqual("expired", updated["work_session"]["status"])

        decided = self.decide(parked, "supply_credential")

        self.assertEqual("resolved", decided["request"]["status"])
        self.assertEqual("ACTIVE", decided["mission"]["state"])
        self.assertEqual(
            "",
            journal.get_item(task_id, project=PROJECT)["human_request_id"],
        )

    def test_nonmission_no_head_decision_uses_request_version_without_v4_effect(self):
        task_id, _session_id, parked = self.park(
            "v1 non-code Human decision has no PR head",
            staged=False,
            head_sha="",
        )
        decided = self.decide(parked, "supply_credential")
        self.assertTrue(decided["created"])
        self.assertEqual("decision_recorded", decided["request"]["status"])
        self.assertNotIn("mission", decided)
        self.assertIsNone(journal.get_item(task_id, project=PROJECT))

    def test_current_code_head_change_still_fails_closed(self):
        task_id, _session_id, parked = self.park(
            "code Human request retains exact-head fence",
        )
        with _conn(PROJECT) as connection:
            connection.execute(
                "INSERT INTO task_git_state(task_id,head_sha,updated_at) "
                "VALUES (?,?,?)",
                (task_id, "b" * 40, 1.0),
            )
        with self.assertRaises(AttentionStoreError) as stale:
            self.decide(parked, "supply_credential")
        self.assertEqual("stale_attention_head", stale.exception.code)
        with _conn(PROJECT) as connection:
            request = connection.execute(
                "SELECT status FROM attention_requests WHERE request_id=?",
                (parked["attention_request_id"],),
            ).fetchone()
            decisions = connection.execute(
                "SELECT COUNT(*) FROM attention_decisions WHERE request_id=?",
                (parked["attention_request_id"],),
            ).fetchone()[0]
        self.assertEqual("cancelled", request["status"])
        self.assertEqual(0, decisions)
        self.assertEqual("HUMAN", journal.get_item(task_id, project=PROJECT)["state"])

    def test_stale_mission_pointer_rejects_before_recording_decision(self):
        task_id, _session_id, parked = self.park(
            "stale mission request pointer fails closed",
        )
        with _conn(PROJECT) as connection:
            connection.execute(
                "UPDATE mission_items SET human_request_id='attention-other' "
                "WHERE project_id=? AND task_id=?",
                (PROJECT, task_id),
            )
        with self.assertRaises(AttentionStoreError) as stale:
            self.decide(parked, "supply_credential")
        self.assertEqual("attention_mission_binding_stale", stale.exception.code)
        with _conn(PROJECT) as connection:
            request = connection.execute(
                "SELECT status,version FROM attention_requests WHERE request_id=?",
                (parked["attention_request_id"],),
            ).fetchone()
            decisions = connection.execute(
                "SELECT COUNT(*) FROM attention_decisions WHERE request_id=?",
                (parked["attention_request_id"],),
            ).fetchone()[0]
        self.assertEqual(("pending", 1), (request["status"], request["version"]))
        self.assertEqual(0, decisions)

    def test_projection_failure_rolls_back_decision_and_answer_event(self):
        task_id, _session_id, parked = self.park(
            "failed mission answer projection is atomic",
        )
        trigger = f"coord116_fail_{task_id.lower().replace('-', '_')}"
        quoted_task_id = task_id.replace("'", "''")
        with _conn(PROJECT) as connection:
            connection.execute(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF state ON mission_items "
                f"WHEN NEW.task_id='{quoted_task_id}' AND NEW.state='ACTIVE' "
                "BEGIN SELECT RAISE(ABORT, 'forced answer projection failure'); END",
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced answer projection failure"):
            self.decide(parked, "supply_credential")
        with _conn(PROJECT) as connection:
            request = connection.execute(
                "SELECT status,version FROM attention_requests WHERE request_id=?",
                (parked["attention_request_id"],),
            ).fetchone()
            decisions = connection.execute(
                "SELECT COUNT(*) FROM attention_decisions WHERE request_id=?",
                (parked["attention_request_id"],),
            ).fetchone()[0]
            answers = connection.execute(
                "SELECT COUNT(*) FROM mission_events WHERE project_id=? "
                "AND task_id=? AND event_type='human_answered'",
                (PROJECT, task_id),
            ).fetchone()[0]
            connection.execute(f"DROP TRIGGER {trigger}")
        self.assertEqual(("pending", 1), (request["status"], request["version"]))
        self.assertEqual(0, decisions)
        self.assertEqual(0, answers)
        self.assertEqual("HUMAN", journal.get_item(task_id, project=PROJECT)["state"])

    def test_unbound_operator_cannot_answer(self):
        task_id, _session_id, parked = self.park(
            "unauthenticated Human decision is rejected",
        )
        unbound = ProjectContext(project_id=PROJECT, source="test")
        with self.assertRaises(AttentionStoreError) as denied:
            self.decide(parked, "supply_credential", context=unbound)
        self.assertEqual("attention_principal_unbound", denied.exception.code)
        self.assertEqual("HUMAN", journal.get_item(task_id, project=PROJECT)["state"])


if __name__ == "__main__":
    unittest.main()
