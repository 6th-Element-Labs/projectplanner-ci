#!/usr/bin/env python3
"""BUG-248: failed pre-runner wakes must reach the v4 mission journal.

A wake that dies before Capacity creates a runner (e.g. stale_execution_context
after master advances under a pending wake) previously left no journal event,
so ``handled_through == latest_sequence`` and the mission waited forever.  The
capacity projector copies that durable failure into the inbox exactly once and
the unchanged reducer wakes the persisted role against current master.
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT as _ROOT  # noqa: F401
from switchboard.application.commands import capacity_mission_events, mission_journal
from switchboard.application.mission_bot_v4 import run_v4_tick
from switchboard.application.mission_bot_v4.worker import (
    ScopedMissionWorkerPorts,
    tick_scoped_mission,
)
from switchboard.storage.migrations.runner import DDL_MIGRATIONS
from switchboard.storage.repositories.mission_journal import MissionJournalRepository


def _failed_wake(wake_id: str = "wake-1", *, requested_at: float | None = None,
                 runner_session_id: str = "", reason: str = "stale_execution_context",
                 generation: int = 2) -> dict:
    return {
        "wake_id": wake_id,
        "task_id": "QA-28",
        "status": "failed",
        "requested_at": time.time() + 60.0 if requested_at is None else requested_at,
        "runner_session_id": runner_session_id or None,
        "result": {"reason": reason},
        "policy": {
            "execution_assignment": {
                "execution_id": f"execlease-{wake_id}",
                "generation": generation,
            },
            "execution_context": {"base_sha": "f" * 40},
        },
    }


class CapacityWakeProjectionTest(unittest.TestCase):
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
            "QA-28", project="switchboard", requested_role="implementation",
            repository=self.repository,
        )
        # The mission has handled its own start: cursor caught up to the
        # mission_started event, exactly the stuck production state.
        item = self.repository.get_item("QA-28", project="switchboard")
        self.repository.update_item(
            "QA-28", project="switchboard", state="ACTIVE",
            requested_role="implementation",
            expected_version=int(item["version"]), handled_through=1,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _project(self, wakes):
        return capacity_mission_events.append_failed_wake_events(
            project="switchboard", task_id="QA-28",
            repository=self.repository, list_wakes=lambda **kwargs: list(wakes),
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

    def test_failed_pre_runner_wake_projects_exactly_one_execution_ended(self):
        receipt = self._project([_failed_wake()])
        self.assertEqual("capacity_mission_events_projected", receipt["action"])
        self.assertEqual(1, len(receipt["events"]))
        self.assertTrue(receipt["events"][0]["created"])
        events = self.repository.list_events(
            "QA-28", project="switchboard", after_sequence=1, limit=10)
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual("execution_ended", event["event_type"])
        self.assertEqual("capacity", event["source_plane"])
        self.assertEqual("execution_ended:wake-1", event["idempotency_key"])
        self.assertEqual("execlease-wake-1", event["execution_id"])
        self.assertEqual(2, event["generation"])
        self.assertEqual(
            {"wake_id": "wake-1", "status": "failed",
             "reason": "stale_execution_context"},
            event["payload"],
        )
        # No stale base: the replacement must resolve the current default branch.
        self.assertIsNone(event["head_sha"])

    def test_next_tick_starts_exactly_one_fresh_generation(self):
        self._project([_failed_wake()])
        starts = []
        result = tick_scoped_mission(
            "QA-28", project="switchboard", scope_authority={"generation": 1},
            actor="test", ports=self._ports(starts),
        )
        self.assertEqual("start_task", result["action"])
        self.assertEqual(1, len(starts))
        self.assertEqual("implementation", starts[0]["role"])
        # source_sha falls through to empty, never the fenced stale base.
        self.assertEqual("", starts[0]["source_sha"])
        self.assertEqual(2, result["handled_through"])

    def test_replay_creates_no_duplicate_event_or_runner(self):
        wake = _failed_wake()
        first = self._project([wake])
        replay = self._project([wake])
        self.assertTrue(first["events"][0]["created"])
        self.assertFalse(replay["events"][0]["created"])
        item = self.repository.get_item("QA-28", project="switchboard")
        self.assertEqual(2, item["latest_sequence"])
        starts = []
        ports = self._ports(starts)
        tick_scoped_mission(
            "QA-28", project="switchboard", scope_authority={"generation": 1},
            actor="test", ports=ports,
        )
        again = tick_scoped_mission(
            "QA-28", project="switchboard", scope_authority={"generation": 1},
            actor="test", ports=ports,
        )
        self.assertEqual(1, len(starts))
        self.assertEqual("wait", again["action"])
        self.assertEqual("no_unhandled_event", again["reason"])

    def test_wake_with_runner_is_owned_by_runner_projection(self):
        receipt = self._project([_failed_wake(runner_session_id="run-1")])
        self.assertEqual([], receipt["events"])
        item = self.repository.get_item("QA-28", project="switchboard")
        self.assertEqual(1, item["latest_sequence"])

    def test_pre_mission_and_sentinel_rows_are_not_projected(self):
        stale = _failed_wake("wake-old", requested_at=time.time() - 3600.0)
        sentinel = {"error": "control_plane_unavailable", "reason": "sqlite_busy"}
        receipt = self._project([stale, sentinel])
        self.assertEqual([], receipt["events"])
        missing = capacity_mission_events.append_failed_wake_events(
            project="switchboard", task_id="QA-99",
            repository=self.repository, list_wakes=lambda **kwargs: [],
        )
        self.assertEqual("ignored", missing["action"])
        self.assertEqual("mission_not_found", missing["reason"])

    def test_run_v4_tick_invokes_capacity_projector(self):
        class _Store:
            @staticmethod
            def get_task(task_id, *, project):
                return {"dependency_state": {"satisfied": True}}

            @staticmethod
            def task_has_live_execution(task_id, *, project):
                return False

            @staticmethod
            def validate_autopilot_scope_authority(authority, **kwargs):
                return {"allowed": True}

        with patch(
            "switchboard.application.commands.capacity_mission_events."
            "append_failed_wake_events",
            return_value={"action": "capacity_mission_events_projected", "events": []},
        ) as projector:
            run_v4_tick(
                "QA-28", project="switchboard", scope_project="switchboard",
                scope_authority={"generation": 1}, actor="test", agent_id="agent",
                store_mod=_Store,
                journal=self.repository,
            )
        self.assertEqual(1, projector.call_count)
        kwargs = projector.call_args.kwargs
        self.assertEqual("QA-28", kwargs["task_id"])
        self.assertEqual("switchboard", kwargs["project"])
        self.assertIs(self.repository, kwargs["repository"])


if __name__ == "__main__":
    unittest.main()
