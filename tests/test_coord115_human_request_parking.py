#!/usr/bin/env python3
"""COORD-115: the real Human closeout parks a staged v4 mission atomically."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from path_setup import ROOT as _ROOT  # noqa: F401

_TMP = Path(tempfile.mkdtemp(prefix="coord115-human-request-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(_TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(_TMP / "registry.db")
(_TMP / "projects").mkdir()
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(_TMP / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.application.commands import human_blocker  # noqa: E402
from switchboard.application.mission_bot_v4.worker import (  # noqa: E402
    ScopedMissionWorkerPorts,
    tick_scoped_mission,
)
from switchboard.storage.repositories.mission_journal import (  # noqa: E402
    MissionJournalError,
    default_mission_journal_repository as journal,
)

PROJECT = "switchboard"


class HumanRequestParkingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        store.init_db(PROJECT)

    def make_task_and_session(self, title: str) -> tuple[str, str]:
        task = store.create_task({
            "workstream_id": "COORD",
            "title": title,
            "description": "session_profile:code_strict\nCOORD-115 fixture",
            "status": "Not Started",
            "ui_impact": "no",
        }, actor="test", project=PROJECT)
        task_id = str(task["task_id"])
        with _conn(PROJECT) as connection:
            connection.execute(
                "UPDATE tasks SET status='In Progress', assignee=? WHERE task_id=?",
                ("agent/codex/coord115", task_id),
            )
        session = store.create_work_session({
            "agent_id": "agent/codex/coord115",
            "task_id": task_id,
            "repo_role": "canonical",
            "storage_mode": "worktree",
            "worktree_path": str(_TMP / f"wt-{task_id}"),
            "branch": f"codex/{task_id}-human-request",
            "upstream": f"origin/codex/{task_id}-human-request",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "status": "active",
            "dirty_status": "clean",
            "policy_profile": "code_strict",
        }, actor="agent/codex/coord115", project=PROJECT)
        self.assertTrue(session.get("created"), session)
        return task_id, str(session["work_session"]["work_session_id"])

    def request(self, task_id: str, work_session_id: str, *, reason: str) -> dict:
        return human_blocker.execute_mapping({
            "task_id": task_id,
            "work_session_id": work_session_id,
            "reason": reason,
            "completed_work": "Stopped before inventing missing authority.",
            "minimum_human_action": "Supply the missing authority.",
            "resume_condition": "An authenticated operator decision is recorded.",
            "next_automatic_step": "Re-assess the same task.",
            "binding": "registered_agent",
            "agent_id": "agent/codex/coord115",
            "source_tool": "agent_requires_human",
        }, actor="agent/codex/coord115", project=PROJECT)

    def test_real_closeout_parks_and_replays_without_relaunch(self):
        task_id, session_id = self.make_task_and_session(
            "real Human closeout parks staged mission",
        )
        journal.ensure_item(task_id, project=PROJECT)

        first = self.request(
            task_id, session_id, reason="provider_acceptance_capacity_missing",
        )
        self.assertTrue(first["recorded"])
        request_id = str(first["attention_request_id"])
        self.assertEqual({
            "recorded": True,
            "event_created": True,
            "event_id": first["mission"]["event_id"],
            "state": "HUMAN",
            "human_request_id": request_id,
            "task_id": task_id,
        }, first["mission"])
        events = journal.list_events(
            task_id, project=PROJECT, after_sequence=0, limit=20,
        )
        requested = [event for event in events if event["event_type"] == "human_requested"]
        self.assertEqual(1, len(requested))
        self.assertEqual("coordination", requested[0]["source_plane"])
        self.assertEqual(request_id, requested[0]["external_ref"])
        self.assertEqual(request_id, requested[0]["payload"]["human_request_id"])

        starts: list[dict] = []
        ports = ScopedMissionWorkerPorts(
            validate_scope=lambda _authority, **_kwargs: {"allowed": True},
            get_task=lambda _task_id, *, project: {
                "dependency_state": {"satisfied": True},
            },
            has_live_execution=lambda _task_id, *, project: False,
            start_task=lambda task_id, **kwargs: starts.append({
                "task_id": task_id, **kwargs,
            }) or {"action": "starting", "started": True},
            journal=journal,
        )
        tick = tick_scoped_mission(
            task_id,
            project=PROJECT,
            scope_authority={"generation": 1},
            actor="test",
            ports=ports,
        )
        self.assertEqual("wait", tick["action"])
        self.assertEqual([], starts)

        replay = self.request(
            task_id, session_id, reason="provider_acceptance_capacity_missing",
        )
        self.assertEqual(request_id, replay["attention_request_id"])
        self.assertFalse(replay["mission"]["event_created"])
        events = journal.list_events(
            task_id, project=PROJECT, after_sequence=0, limit=20,
        )
        self.assertEqual(
            1,
            len([event for event in events if event["event_type"] == "human_requested"]),
        )

    def test_non_v4_task_keeps_attention_but_has_no_mission_side_effect(self):
        task_id, session_id = self.make_task_and_session(
            "v1 task remains outside dormant mission journal",
        )
        result = self.request(task_id, session_id, reason="credential_missing")
        self.assertTrue(result["recorded"])
        self.assertEqual("mission_not_found", result["mission"]["reason"])
        self.assertIsNone(journal.get_item(task_id, project=PROJECT))

    def test_terminal_mission_is_not_reopened(self):
        task_id, session_id = self.make_task_and_session(
            "terminal mission remains immutable",
        )
        journal.ensure_item(task_id, project=PROJECT)
        with _conn(PROJECT) as connection:
            connection.execute(
                "UPDATE mission_items SET state='DONE' WHERE project_id=? AND task_id=?",
                (PROJECT, task_id),
            )
        result = self.request(task_id, session_id, reason="credential_missing")
        self.assertTrue(result["recorded"])
        self.assertEqual("mission_terminal", result["mission"]["reason"])
        self.assertEqual("DONE", journal.get_item(task_id, project=PROJECT)["state"])

    def test_attention_event_and_human_state_share_one_transaction(self):
        task_id, session_id = self.make_task_and_session(
            "failed HUMAN state write rolls back request and event",
        )
        journal.ensure_item(task_id, project=PROJECT)
        trigger = f"coord115_fail_{task_id.lower().replace('-', '_')}"
        quoted_task_id = task_id.replace("'", "''")
        with _conn(PROJECT) as connection:
            connection.execute(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OF state ON mission_items "
                f"WHEN NEW.task_id='{quoted_task_id}' AND NEW.state='HUMAN' "
                "BEGIN SELECT RAISE(ABORT, 'forced HUMAN write failure'); END",
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced HUMAN write failure"):
            self.request(task_id, session_id, reason="credential_missing")
        with _conn(PROJECT) as connection:
            attention_count = int(connection.execute(
                "SELECT COUNT(*) FROM attention_requests WHERE task_id=?",
                (task_id,),
            ).fetchone()[0])
            event_count = int(connection.execute(
                "SELECT COUNT(*) FROM mission_events "
                "WHERE project_id=? AND task_id=? AND event_type='human_requested'",
                (PROJECT, task_id),
            ).fetchone()[0])
            connection.execute(f"DROP TRIGGER {trigger}")
        self.assertEqual(0, attention_count)
        self.assertEqual(0, event_count)
        self.assertEqual("ACTIVE", journal.get_item(task_id, project=PROJECT)["state"])

    def test_event_contract_rejects_wrong_plane_and_request_reference(self):
        task_id, _session_id = self.make_task_and_session(
            "Human request event contract",
        )
        journal.ensure_item(task_id, project=PROJECT)
        payload = {
            "human_request_id": "attention-contract",
            "reason_code": "credential_missing",
        }
        with self.assertRaises(MissionJournalError) as plane:
            journal.append_event(
                task_id,
                project=PROJECT,
                event_type="human_requested",
                source_plane="communication",
                idempotency_key="human_requested:wrong-plane",
                external_ref="attention-contract",
                payload=payload,
            )
        self.assertEqual("invalid_source_plane", plane.exception.code)
        with self.assertRaises(MissionJournalError) as identity:
            journal.append_event(
                task_id,
                project=PROJECT,
                event_type="human_requested",
                source_plane="coordination",
                idempotency_key="human_requested:wrong-ref",
                external_ref="attention-other",
                payload=payload,
            )
        self.assertEqual("human_request_reference_required", identity.exception.code)


if __name__ == "__main__":
    unittest.main()
