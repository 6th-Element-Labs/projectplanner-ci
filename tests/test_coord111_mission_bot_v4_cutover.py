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

from path_setup import ROOT as _ROOT  # noqa: F401
from coordinator_daemon import DaemonConfig
from switchboard.application.mission_bot_v4 import (
    ReadOnlyEffectSpy,
    production_ports,
    run_v4_tick,
    tick_scoped_mission,
)
from switchboard.application.commands.autopilot import control_autopilot
from switchboard.application.commands.task_execution import _arm_task_scope
from switchboard.storage.migrations.runner import DDL_MIGRATIONS
from switchboard.storage.repositories.mission_journal import MissionJournalRepository


class FakeStore:
    def __init__(self):
        self.live = False
        self.scope_allowed = True
        self.task = {
            "dependency_state": {"satisfied": True},
            "git_state": {"head_sha": "a" * 40},
        }

    def validate_autopilot_scope_authority(self, _authority, **_kwargs):
        return (
            {"allowed": True}
            if self.scope_allowed
            else {"allowed": False, "error": "scope_authority_denied"}
        )

    def get_task(self, task_id, *, project):
        return {
            **self.task,
            "task_id": task_id,
            "project": project,
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

    def test_operator_start_journals_mission_before_scope_is_visible(self):
        calls = []
        with (
            patch(
                "switchboard.application.commands.mission_journal.create_mission",
                side_effect=lambda *_a, **_k: calls.append("mission"),
            ),
            patch(
                "switchboard.storage.repositories.autopilot_scopes."
                "list_autopilot_scopes",
                side_effect=lambda **_k: calls.append("list") or [],
            ),
            patch(
                "switchboard.storage.repositories.autopilot_scopes."
                "start_autopilot_scope",
                side_effect=lambda **_k: calls.append("scope") or {
                    "scope_id": "scope-111", "scope_type": "task",
                },
            ),
        ):
            result = _arm_task_scope(
                "COORD-111", project="switchboard",
                role="implementation", runtime="codex", actor="operator",
            )
        self.assertEqual(["mission", "list", "scope"], calls)
        self.assertEqual("scope-111", result["scope_id"])

    def test_control_start_creates_review_mission_before_scope(self):
        calls = []
        with (
            patch(
                "switchboard.storage.repositories.autopilot_scopes."
                "validate_autopilot_target",
                return_value=None,
            ),
            patch(
                "switchboard.storage.repositories.tasks.get_task",
                return_value={
                    "task_id": "COORD-111",
                    "git_state": {"pr_number": 1118, "pr_url": "https://example/pr/1118"},
                },
            ),
            patch(
                "switchboard.application.commands.mission_journal.create_mission",
                side_effect=lambda *_a, **kwargs: (
                    calls.append(("mission", kwargs["requested_role"])) or {}
                ),
            ),
            patch(
                "switchboard.storage.repositories.autopilot_scopes."
                "start_autopilot_scope",
                side_effect=lambda **_kwargs: (
                    calls.append(("scope", None))
                    or {"scope_id": "scope-111", "scope_type": "task"}
                ),
            ),
        ):
            result = control_autopilot(
                "",
                project="switchboard",
                action="start",
                scope_type="task",
                task_project="switchboard",
                task_id="COORD-111",
            )
        self.assertEqual(
            [("mission", "review_merge"), ("scope", None)],
            calls,
        )
        self.assertEqual("scope-111", result["scope"]["scope_id"])

    def test_active_pre_cutover_scope_backfills_from_current_pr_then_starts(self):
        self.store.task["git_state"] = {
            "head_sha": "b" * 40,
            "pr_number": 1118,
            "pr_url": "https://example/pr/1118",
        }
        with patch(
            "switchboard.application.mission_bot_v4.cutover."
            "task_execution.start_task",
            return_value={"action": "started", "started": True},
        ) as start:
            result = run_v4_tick(
                "BUG-244",
                project="switchboard",
                scope_project="switchboard",
                scope_authority=self.authority,
                actor="switchboard/coordinator-autopilot",
                agent_id="coordinator-111",
                store_mod=self.store,
                journal=self.journal,
            )
        item = self.journal.get_item("BUG-244", project="switchboard")
        self.assertEqual("active_scope_backfill", result["mission_initialization"]["source"])
        self.assertEqual("review_merge", item["requested_role"])
        self.assertEqual("review_merge", start.call_args.kwargs["role"])
        self.assertEqual("b" * 40, start.call_args.kwargs["source_sha"])

    def test_denied_scope_cannot_backfill_a_missing_mission(self):
        self.store.scope_allowed = False
        result = run_v4_tick(
            "BUG-244",
            project="switchboard",
            scope_project="switchboard",
            scope_authority=self.authority,
            actor="switchboard/coordinator-autopilot",
            agent_id="coordinator-111",
            store_mod=self.store,
            journal=self.journal,
        )
        self.assertEqual("scope_authority_denied", result["reason"])
        self.assertIsNone(
            self.journal.get_item("BUG-244", project="switchboard")
        )

    def test_waiting_mission_gets_one_scoped_observation_backstop(self):
        item = self.journal.get_item("COORD-111", project="switchboard")
        self.journal.update_item(
            "COORD-111",
            project="switchboard",
            state="WAITING",
            requested_role="review_merge",
            expected_version=item["version"],
            handled_through=1,
            now=1.0,
        )
        with patch(
            "switchboard.application.mission_bot_v4.cutover."
            "task_execution.start_task",
            return_value={"action": "started", "started": True},
        ) as start:
            first = run_v4_tick(
                "COORD-111",
                project="switchboard",
                scope_project="switchboard",
                scope_authority=self.authority,
                actor="switchboard/coordinator-autopilot",
                agent_id="coordinator-111",
                store_mod=self.store,
                journal=self.journal,
            )
            replay = run_v4_tick(
                "COORD-111",
                project="switchboard",
                scope_project="switchboard",
                scope_authority=self.authority,
                actor="switchboard/coordinator-autopilot",
                agent_id="coordinator-111",
                store_mod=self.store,
                journal=self.journal,
            )
        events = self.journal.list_events(
            "COORD-111", project="switchboard", limit=20)
        due = [event for event in events if event["event_type"] == "observation_due"]
        self.assertEqual("start_task", first["action"])
        self.assertEqual("no_unhandled_event", replay["reason"])
        self.assertEqual(1, start.call_count)
        self.assertEqual(1, len(due))

    def test_deployed_config_defaults_v4_and_rejects_retired_engines(self):
        self.assertEqual("v4", DaemonConfig.from_env({}).mission_engine)
        for retired in ("legacy", "shadow", "both"):
            with self.subTest(retired=retired), self.assertRaisesRegex(
                    ValueError, "must be v4"):
                DaemonConfig.from_env(
                    {"PM_COORDINATOR_MISSION_ENGINE": retired})


if __name__ == "__main__":
    unittest.main()
