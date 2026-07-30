#!/usr/bin/env python3
"""COORD-110: fenced, replay-safe Mission Bot v4 pager."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from path_setup import ROOT as _ROOT
from switchboard.application.mission_bot_v4 import (
    ScopedMissionWorkerPorts,
    tick_scoped_mission,
)
from switchboard.storage.migrations.runner import DDL_MIGRATIONS
from switchboard.storage.repositories.mission_journal import MissionJournalRepository


class ScopedMissionWorkerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "worker.db"

        @contextmanager
        def connector(_project):
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            for name, sql in DDL_MIGRATIONS:
                if name in {
                    "0123_mission_items", "0124_mission_events",
                    "0125_ix_mission_events_task_sequence",
                    "0126_ix_mission_events_task_head",
                }:
                    connection.execute(sql)
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        self.journal = MissionJournalRepository(connector)
        self.task = {
            "task_id": "COORD-110",
            "status": "Blocked",  # Deliberately not an admission signal.
            "dependency_state": {"satisfied": True},
        }
        self.starts = []
        self.authority = {
            "schema": "switchboard.autopilot_scope_authority.v1",
            "scope_id": "scope-110",
            "lease_id": "lease-110",
            "holder_agent_id": "coordinator-110",
            "generation": 7,
            "fence_epoch": 3,
        }
        self.scope_allowed = True
        self.runner_live = False

    def tearDown(self):
        self.temp.cleanup()

    def ports(self):
        def start(task_id, **kwargs):
            self.starts.append({"task_id": task_id, **kwargs})
            self.runner_live = True
            return {"action": "started", "started": True}

        return ScopedMissionWorkerPorts(
            validate_scope=lambda *_args, **_kwargs: {
                "allowed": self.scope_allowed,
                **({} if self.scope_allowed else {"error": "scope_authority_denied"}),
            },
            get_task=lambda *_args, **_kwargs: dict(self.task),
            has_live_execution=lambda *_args, **_kwargs: self.runner_live,
            start_task=start,
            journal=self.journal,
        )

    def create(self, *, state="ACTIVE", role="implementation"):
        self.journal.ensure_item(
            "COORD-110", project="switchboard", state=state, requested_role=role,
        )
        return self.journal.append_event(
            "COORD-110", project="switchboard", event_type="mission_started",
            source_plane="coordination", idempotency_key="start-110",
        )

    def tick(self):
        return tick_scoped_mission(
            "COORD-110", project="switchboard",
            scope_authority=self.authority, actor="coordinator-110",
            ports=self.ports(),
        )

    def test_oldest_event_starts_persisted_role_once_and_replay_waits(self):
        self.create(role="review_merge")
        self.journal.append_event(
            "COORD-110", project="switchboard", event_type="github_changed",
            source_plane="communication", idempotency_key="github-110",
        )
        first = self.tick()
        replay = self.tick()
        self.assertEqual("start_task", first["action"])
        self.assertEqual(1, first["event_pointer"])
        self.assertEqual("review_merge", self.starts[0]["role"])
        self.assertEqual("wait", replay["action"])
        self.assertEqual(1, len(self.starts))

    def test_inactive_scope_terminal_dependency_human_and_runner_wait(self):
        self.create()
        self.scope_allowed = False
        self.assertEqual("scope_authority_denied", self.tick()["reason"])
        self.scope_allowed = True
        self.task["dependency_state"]["satisfied"] = False
        self.assertEqual("dependencies_unmet", self.tick()["reason"])
        self.task["dependency_state"]["satisfied"] = True
        item = self.journal.get_item("COORD-110", project="switchboard")
        self.journal.update_item(
            "COORD-110", project="switchboard", state="HUMAN",
            requested_role="implementation", expected_version=item["version"],
        )
        self.assertEqual("authenticated_agent_request", self.tick()["reason"])
        item = self.journal.get_item("COORD-110", project="switchboard")
        self.journal.update_item(
            "COORD-110", project="switchboard", state="ACTIVE",
            requested_role="implementation", expected_version=item["version"],
        )
        self.runner_live = True
        self.assertEqual("runner_live", self.tick()["reason"])
        self.assertEqual([], self.starts)

    def test_verified_done_is_observed_and_board_blocked_does_not_deadlock(self):
        self.create(role="review_merge")
        item = self.journal.get_item("COORD-110", project="switchboard")
        self.journal.update_item(
            "COORD-110", project="switchboard", state="DONE",
            requested_role="review_merge", expected_version=item["version"],
            handled_through=1, terminal_kind="github_merge", terminal_ref="abc123",
            authority="canonical_provenance_projector",
        )
        self.assertEqual("terminal_provenance", self.tick()["reason"])
        self.assertEqual([], self.starts)

    def test_failed_admission_leaves_event_unhandled_for_same_key_retry(self):
        self.create(role="remediation")
        calls = []
        ports = self.ports()
        failing = ScopedMissionWorkerPorts(
            validate_scope=ports.validate_scope,
            get_task=ports.get_task,
            has_live_execution=ports.has_live_execution,
            start_task=lambda task_id, **kwargs: (
                calls.append({"task_id": task_id, **kwargs}) or {"error": "no host"}
            ),
            journal=self.journal,
        )
        first = tick_scoped_mission(
            "COORD-110", project="switchboard",
            scope_authority=self.authority, actor="coordinator-110", ports=failing,
        )
        second = tick_scoped_mission(
            "COORD-110", project="switchboard",
            scope_authority=self.authority, actor="coordinator-110", ports=failing,
        )
        self.assertEqual("start_not_admitted", first["reason"])
        self.assertEqual(calls[0]["mission_key"], calls[1]["mission_key"])
        item = self.journal.get_item("COORD-110", project="switchboard")
        self.assertEqual(0, item["handled_through"])

    def test_transitioning_receipt_is_not_admission_and_keeps_cursor(self):
        self.create(role="review_merge")
        ports = self.ports()
        transitioning = ScopedMissionWorkerPorts(
            validate_scope=ports.validate_scope,
            get_task=ports.get_task,
            has_live_execution=ports.has_live_execution,
            start_task=lambda *_args, **_kwargs: {
                "action": "transitioning", "started": False, "attached": False,
            },
            journal=self.journal,
        )
        result = tick_scoped_mission(
            "COORD-110", project="switchboard",
            scope_authority=self.authority, actor="coordinator-110",
            ports=transitioning,
        )
        self.assertEqual("start_not_admitted", result["reason"])
        item = self.journal.get_item("COORD-110", project="switchboard")
        self.assertEqual(0, item["handled_through"])

    def test_abnormal_terminal_keeps_role_and_late_receipt_cannot_restore_old_role(self):
        self.create(role="implementation")
        abnormal = self.tick()
        self.assertEqual("implementation", abnormal["requested_role"])

        # Model the accepted C3 finalizer: it advances the durable role before
        # Capacity's generic terminal observation can be considered.
        self.runner_live = False
        item = self.journal.get_item("COORD-110", project="switchboard")
        self.journal.update_item(
            "COORD-110", project="switchboard", state="ACTIVE",
            requested_role="review_merge", expected_version=item["version"],
            handled_through=item["handled_through"],
        )
        self.journal.append_event(
            "COORD-110", project="switchboard", event_type="runner_ended",
            source_plane="capacity", idempotency_key="late-runner-ended",
            execution_id="old-implementation",
        )
        late = self.tick()
        self.assertEqual("review_merge", late["requested_role"])
        self.assertNotEqual("implementation", late["requested_role"])

    def test_scope_is_revalidated_at_write_boundary(self):
        self.create()
        validations = []

        def validate(*_args, **_kwargs):
            validations.append(True)
            return (
                {"allowed": True}
                if len(validations) == 1
                else {"allowed": False, "error": "scope_authority_denied"}
            )

        ports = self.ports()
        result = tick_scoped_mission(
            "COORD-110", project="switchboard",
            scope_authority=self.authority, actor="coordinator-110",
            ports=ScopedMissionWorkerPorts(
                validate_scope=validate,
                get_task=ports.get_task,
                has_live_execution=ports.has_live_execution,
                start_task=ports.start_task,
                journal=self.journal,
            ),
        )
        self.assertEqual("scope_authority_denied", result["reason"])
        self.assertEqual([], self.starts)


if __name__ == "__main__":
    unittest.main()
