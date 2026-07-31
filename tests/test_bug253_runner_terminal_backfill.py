#!/usr/bin/env python3
"""BUG-253: an operator-killed runner must still reach the v4 mission journal.

The kill path in ``complete_runner_control`` writes the terminal status onto
the runner session row with raw SQL, so ``upsert_runner_session`` never fires
``record_runner_terminal``: no ``runner_ended`` event lands, ``handled_through``
stays equal to ``latest_sequence``, and the mission believes its dead runner is
still working — forever (QA-46 in the 3-2-3 drain canary).  The terminal-runner
sweep copies the already-persisted terminal fact into the inbox exactly once so
the unchanged reducer restarts the persisted role.
"""
from __future__ import annotations

import sqlite3
import time
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from path_setup import ROOT as _ROOT  # noqa: F401
from switchboard.application.commands import capacity_mission_events, mission_journal
from switchboard.application.mission_bot_v4.worker import (
    ScopedMissionWorkerPorts,
    tick_scoped_mission,
)
from switchboard.storage.migrations.runner import DDL_MIGRATIONS
from switchboard.storage.repositories.mission_journal import MissionJournalRepository


def _terminal_session(runner_session_id: str = "run-1", *,
                      status: str = "killed",
                      started_at: float | None = None,
                      execution_id: str = "execlease-1",
                      generation: int = 2) -> dict:
    return {
        "runner_session_id": runner_session_id,
        "task_id": "QA-46",
        "status": status,
        "started_at": time.time() + 60.0 if started_at is None else started_at,
        "metadata": {
            "execution_id": execution_id,
            "execution_generation": generation,
            "wake_id": "wake-1",
        },
    }


class RunnerTerminalBackfillTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = {}

        @contextmanager
        def connector(project):
            path = self.paths.setdefault(project, Path(self.temp.name) / f"{project}.db")
            c = sqlite3.connect(path)
            c.row_factory = sqlite3.Row
            for name, sql in DDL_MIGRATIONS:
                if name in {
                    "0123_mission_items", "0124_mission_events",
                    "0125_ix_mission_events_task_sequence",
                    "0126_ix_mission_events_task_head",
                }:
                    c.execute(sql)
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

        self.repository = MissionJournalRepository(connector)
        mission_journal.create_mission(
            "QA-46", project="switchboard", requested_role="implementation",
            repository=self.repository,
        )
        # The stuck production shape: the mission handled its own start (the
        # killed generation-2 runner "handled" the last event by starting).
        item = self.repository.get_item("QA-46", project="switchboard")
        self.repository.update_item(
            "QA-46", project="switchboard", state="ACTIVE",
            requested_role="implementation",
            expected_version=int(item["version"]), handled_through=1,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _sweep(self, sessions):
        return capacity_mission_events.append_terminal_runner_events(
            project="switchboard", task_id="QA-46",
            repository=self.repository, list_runners=lambda **kwargs: list(sessions),
        )

    def _ports(self, starts):
        def start_task(task_id, **kwargs):
            starts.append({"task_id": task_id, **kwargs})
            return {"started": True, "action": "started"}

        return ScopedMissionWorkerPorts(
            validate_scope=lambda authority, **kwargs: {"allowed": True},
            get_task=lambda task_id, *, project: {
                "dependency_state": {"satisfied": True},
            },
            has_live_execution=lambda task_id, *, project: False,
            start_task=start_task,
            journal=self.repository,
        )

    def test_killed_runner_projects_exactly_one_runner_ended(self):
        receipt = self._sweep([_terminal_session()])
        self.assertEqual("terminal_runner_events_projected", receipt["action"])
        self.assertEqual(1, len(receipt["events"]))
        events = self.repository.list_events(
            "QA-46", project="switchboard", after_sequence=1, limit=10)
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual("runner_ended", event["event_type"])
        self.assertEqual("capacity", event["source_plane"])
        self.assertEqual("runner_ended:run-1", event["idempotency_key"])
        item = self.repository.get_item("QA-46", project="switchboard")
        # The receipt is the wake edge: it must stay unhandled so the fenced
        # worker restarts the persisted role.
        self.assertEqual("ACTIVE", item["state"])
        self.assertEqual(1, int(item["handled_through"]))

    def test_replay_is_idempotent(self):
        self._sweep([_terminal_session()])
        receipt = self._sweep([_terminal_session()])
        self.assertEqual(0, len(receipt["events"]))
        events = self.repository.list_events(
            "QA-46", project="switchboard", after_sequence=1, limit=10)
        self.assertEqual(1, len(events))

    def test_live_and_completed_runners_are_skipped(self):
        receipt = self._sweep([
            _terminal_session("run-live", status="running"),
            _terminal_session("run-done", status="completed"),
        ])
        self.assertEqual(0, len(receipt["events"]))
        self.assertEqual([], self.repository.list_events(
            "QA-46", project="switchboard", after_sequence=1, limit=10))

    def test_previous_mission_era_receipt_is_skipped(self):
        receipt = self._sweep([_terminal_session(started_at=1.0)])
        self.assertEqual(0, len(receipt["events"]))

    def test_incomplete_execution_identity_is_skipped_not_fatal(self):
        receipt = self._sweep([
            _terminal_session("run-noid", execution_id="", generation=0),
            _terminal_session("run-good"),
        ])
        self.assertEqual(1, len(receipt["events"]))
        self.assertEqual("run-good", receipt["events"][0]["runner_session_id"])

    def test_missing_mission_is_ignored(self):
        receipt = capacity_mission_events.append_terminal_runner_events(
            project="switchboard", task_id="QA-999",
            repository=self.repository,
            list_runners=lambda **kwargs: [_terminal_session()],
        )
        self.assertEqual("ignored", receipt["action"])
        self.assertEqual("mission_not_found", receipt["reason"])

    def test_backfilled_receipt_restarts_the_mission(self):
        self._sweep([_terminal_session()])
        starts: list = []
        result = tick_scoped_mission(
            "QA-46", project="switchboard", ports=self._ports(starts),
            scope_authority={"scope_id": "test"}, actor="test",
        )
        self.assertEqual(1, len(starts), result)
        self.assertEqual("QA-46", starts[0]["task_id"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
