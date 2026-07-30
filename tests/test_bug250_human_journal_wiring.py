#!/usr/bin/env python3
"""BUG-250: authenticated Human requests must reach the v4 mission journal.

agent_requires_human fenced the runner and parked the board, but the mission
item stayed ACTIVE, so the fenced runner's terminal receipt read as runner
loss and the pager relaunched over the open Human request. The request side
now parks the mission on HUMAN; the answer side is pulled from the durable
attention request by run_v4_tick, so a dropped delivery cannot strand it.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT as _ROOT  # noqa: F401
from switchboard.application.commands import human_mission_events, mission_journal
from switchboard.application.mission_bot_v4 import ReadOnlyEffectSpy, run_v4_tick
from switchboard.application.mission_bot_v4.worker import (
    ScopedMissionWorkerPorts,
    tick_scoped_mission,
)
from switchboard.storage.migrations.runner import DDL_MIGRATIONS
from switchboard.storage.repositories.mission_journal import MissionJournalRepository

TASK = "QA-33"
REQUEST = "attn-bug250"


class HumanJournalWiringTest(unittest.TestCase):
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
            TASK, project="switchboard", requested_role="implementation",
            repository=self.repository)
        # The runner asked for Human and was fenced; its terminal receipt is
        # already in the inbox — exactly the state that used to relaunch.
        item = self.repository.get_item(TASK, project="switchboard")
        self.repository.update_item(
            TASK, project="switchboard", state="ACTIVE",
            requested_role="implementation",
            expected_version=int(item["version"]), handled_through=1)
        self.repository.append_event(
            TASK, project="switchboard", event_type="runner_ended",
            source_plane="capacity", idempotency_key="runner_ended:run-bug250",
            payload={"status": "exited"})

    def tearDown(self):
        self.temp.cleanup()

    def _record(self):
        return human_mission_events.record_human_requested(
            project="switchboard", task_id=TASK, request_id=REQUEST,
            reason="needs a human decision", repository=self.repository)

    def _reconcile(self, status):
        return human_mission_events.reconcile_human_answer(
            project="switchboard", task_id=TASK, repository=self.repository,
            get_request=lambda request_id, *, project: {
                "request_id": request_id, "status": status})

    def _ports(self, starts):
        def start_task(task_id, **kwargs):
            starts.append({"task_id": task_id, **kwargs})
            return {"started": True, "action": "started"}

        return ScopedMissionWorkerPorts(
            validate_scope=lambda authority, **kwargs: {"allowed": True},
            get_task=lambda task_id, *, project: {
                "dependency_state": {"satisfied": True}},
            has_live_execution=lambda task_id, *, project: False,
            start_task=start_task,
            journal=self.repository,
        )

    def _tick(self, starts):
        return tick_scoped_mission(
            TASK, project="switchboard", scope_authority={"generation": 1},
            actor="test", ports=self._ports(starts))

    def test_request_parks_the_mission_on_human(self):
        receipt = self._record()
        self.assertTrue(receipt["recorded"])
        self.assertEqual("HUMAN", receipt["state"])
        self.assertEqual(REQUEST, receipt["human_request_id"])
        events = self.repository.list_events(
            TASK, project="switchboard", after_sequence=2, limit=5)
        self.assertEqual("human_requested", events[0]["event_type"])
        self.assertEqual("coordination", events[0]["source_plane"])
        replay = self._record()
        self.assertFalse(replay["event_created"])
        self.assertEqual("HUMAN", replay["state"])

    def test_human_mission_does_not_relaunch_over_unhandled_events(self):
        self._record()
        starts = []
        result = self._tick(starts)
        self.assertEqual("wait", result["action"])
        self.assertEqual([], starts)

    def test_pending_request_keeps_waiting(self):
        self._record()
        receipt = self._reconcile("pending")
        self.assertEqual("waiting", receipt["action"])
        item = self.repository.get_item(TASK, project="switchboard")
        self.assertEqual("HUMAN", item["state"])

    def test_unanswered_terminal_request_stays_human(self):
        self._record()
        for status in ("failed", "expired", "cancelled", "orphaned"):
            receipt = self._reconcile(status)
            self.assertEqual("waiting", receipt["action"], status)
        self.assertEqual(
            "HUMAN", self.repository.get_item(TASK, project="switchboard")["state"])

    def test_answered_request_resumes_exactly_one_generation(self):
        self._record()
        receipt = self._reconcile("decision_recorded")
        self.assertEqual("human_answered", receipt["action"])
        self.assertEqual("ACTIVE", receipt["state"])
        item = self.repository.get_item(TASK, project="switchboard")
        self.assertEqual("", item["human_request_id"])
        starts = []
        first = self._tick(starts)
        self.assertEqual("start_task", first["action"])
        self.assertEqual("implementation", starts[0]["role"])
        replay = self._reconcile("decision_recorded")
        self.assertEqual("ignored", replay["action"])  # no longer HUMAN
        events = self.repository.list_events(
            TASK, project="switchboard", after_sequence=0, limit=20)
        answered = [e for e in events if e["event_type"] == "human_answered"]
        self.assertEqual(1, len(answered))

    def test_missing_mission_is_untouched(self):
        receipt = human_mission_events.record_human_requested(
            project="switchboard", task_id="QA-99", request_id=REQUEST,
            repository=self.repository)
        self.assertFalse(receipt["recorded"])
        self.assertEqual("mission_not_found", receipt["reason"])

    def test_run_v4_tick_invokes_the_human_reconciler(self):
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

            @staticmethod
            def list_wake_intents(**kwargs):
                return []

        with patch(
            "switchboard.application.commands.human_mission_events."
            "reconcile_human_answer",
            return_value={"action": "ignored", "reason": "not_human"},
        ) as reconciler:
            run_v4_tick(
                TASK, project="switchboard", scope_project="switchboard",
                scope_authority={"generation": 1}, actor="test", agent_id="agent",
                store_mod=_Store, effect_spy=ReadOnlyEffectSpy(),
                journal=self.repository)
        self.assertEqual(1, reconciler.call_count)
        kwargs = reconciler.call_args.kwargs
        self.assertEqual(TASK, kwargs["task_id"])
        self.assertIs(self.repository, kwargs["repository"])


if __name__ == "__main__":
    unittest.main()
