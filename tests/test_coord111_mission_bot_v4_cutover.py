#!/usr/bin/env python3
"""COORD-111: one audited Mission Bot v4 production cutover switch."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT as _ROOT
from coordinator_daemon import DaemonConfig
from switchboard.application.mission_bot_v4 import (
    ReadOnlyEffectSpy,
    production_ports,
    tick_scoped_mission,
)
from switchboard.storage.migrations.runner import DDL_MIGRATIONS
from switchboard.storage.repositories.mission_journal import MissionJournalRepository


class FakeStore:
    def __init__(self):
        self.live = False

    @staticmethod
    def validate_autopilot_scope_authority(_authority, **_kwargs):
        return {"allowed": True}

    @staticmethod
    def get_task(task_id, *, project):
        return {
            "task_id": task_id,
            "project": project,
            "dependency_state": {"satisfied": True},
            "git_state": {"head_sha": "a" * 40},
        }

    def task_has_live_execution(self, _task_id, *, project):
        del project
        return self.live


class MissionBotV4CutoverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "coord111.db"

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
        self.journal.ensure_item(
            "COORD-111", project="switchboard",
            state="ACTIVE", requested_role="review_merge",
        )
        self.journal.append_event(
            "COORD-111", project="switchboard",
            event_type="github_changed", source_plane="communication",
            idempotency_key="github:status:review:comment",
        )
        self.authority = {
            "schema": "switchboard.autopilot_scope_authority.v1",
            "scope_id": "scope-111",
            "lease_id": "lease-111",
            "holder_agent_id": "coordinator-111",
            "generation": 4,
            "fence_epoch": 2,
        }
        self.store = FakeStore()

    def tearDown(self):
        self.temp.cleanup()

    def ports(self, spy=None):
        ports = production_ports(
            actor="switchboard/coordinator-autopilot",
            agent_id="coordinator-111",
            scope_project="switchboard",
            scope_authority=self.authority,
            store_mod=self.store,
            effect_spy=spy,
        )
        return replace(ports, journal=self.journal)

    def tick(self, ports):
        return tick_scoped_mission(
            "COORD-111", project="switchboard",
            scope_authority=self.authority,
            actor="switchboard/coordinator-autopilot",
            ports=ports,
        )

    def test_shadow_loaded_graph_blocks_every_start_and_preserves_cursor(self):
        spy = ReadOnlyEffectSpy()
        result = self.tick(self.ports(spy))
        item = self.journal.get_item("COORD-111", project="switchboard")
        self.assertEqual("start_not_admitted", result["reason"])
        self.assertEqual("review_merge", spy.attempts[0]["role"])
        self.assertEqual(0, item["handled_through"])

    def test_v4_loaded_graph_has_one_start_task_effect_and_advances_cursor(self):
        with patch(
            "switchboard.application.mission_bot_v4.cutover."
            "task_execution.start_task",
            return_value={"action": "started", "started": True},
        ) as start:
            result = self.tick(self.ports())
        self.assertEqual("start_task", result["action"])
        self.assertEqual(1, start.call_count)
        self.assertEqual("review_merge", start.call_args.kwargs["role"])
        item = self.journal.get_item("COORD-111", project="switchboard")
        self.assertEqual(1, item["handled_through"])

    def test_deployed_config_defaults_v4_and_retains_one_rollback_value(self):
        self.assertEqual("v4", DaemonConfig.from_env({}).mission_engine)
        self.assertEqual(
            "legacy",
            DaemonConfig.from_env(
                {"PM_COORDINATOR_MISSION_ENGINE": "legacy"}
            ).mission_engine,
        )
        with self.assertRaisesRegex(ValueError, "legacy, shadow, or v4"):
            DaemonConfig.from_env(
                {"PM_COORDINATOR_MISSION_ENGINE": "both"}
            )


if __name__ == "__main__":
    unittest.main()
