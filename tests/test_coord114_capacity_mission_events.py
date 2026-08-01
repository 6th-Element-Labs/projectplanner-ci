#!/usr/bin/env python3
"""COORD-114: restore the missing Capacity facts before v4 cutover."""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT as _ROOT  # noqa: F401
from switchboard.application.commands import capacity_mission_events
from switchboard.application.mission_bot_v4 import run_scoped_mission_tick
from switchboard.storage.migrations.runner import DDL_MIGRATIONS
from switchboard.storage.repositories.mission_journal import (
    MissionJournalError,
    MissionJournalRepository,
)

PROJECT = "switchboard"
TASK = "COORD-114"


def failed_wake(
    wake_id: str = "wake-failed",
    *,
    status: str = "failed",
    requested_at: float | None = None,
    runner_session_id: str = "",
    reason: str = "stale_execution_context",
    execution_id: str = "execlease-failed",
    generation: int = 2,
) -> dict:
    return {
        "wake_id": wake_id,
        "task_id": TASK,
        "status": status,
        "requested_at": time.time() + 60 if requested_at is None else requested_at,
        "completed_at": time.time() + 61,
        "runner_session_id": runner_session_id or None,
        "result": {"reason": reason} if reason else {},
        "policy": {
            "execution_assignment": {
                "execution_id": execution_id,
                "generation": generation,
            },
            "lifecycle": {
                "execution_id": execution_id,
                "generation": generation,
            },
            "execution_context": {"base_sha": "stale-head-must-not-replay"},
        },
    }


def terminal_runner(
    runner_id: str = "run-killed",
    *,
    status: str = "killed",
    started_at: float | None = None,
    execution_id: str = "execlease-runner",
    generation: int = 3,
) -> dict:
    return {
        "runner_session_id": runner_id,
        "task_id": TASK,
        "status": status,
        "started_at": time.time() + 60 if started_at is None else started_at,
        "updated_at": time.time() + 61,
        "metadata": {
            "execution_id": execution_id,
            "execution_generation": generation,
            "execution_head_sha": "current-head",
        },
        "execution": {
            "execution_id": execution_id,
            "generation": generation,
            "head_sha": "current-head",
        },
    }


class CapacityMissionEventsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "journal.db"

        @contextmanager
        def connector(_project):
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            for name, sql in DDL_MIGRATIONS:
                if name in {
                    "0123_mission_items",
                    "0124_mission_events",
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
        created = self.journal.create_mission(
            TASK, project=PROJECT, requested_role="implementation",
        )
        item = created["mission"]
        self.journal.update_item(
            TASK,
            project=PROJECT,
            state="ACTIVE",
            requested_role="implementation",
            expected_version=int(item["version"]),
            handled_through=1,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def wakes(self, rows, runner_rows=()):
        return capacity_mission_events.append_terminal_wake_events(
            project=PROJECT,
            task_id=TASK,
            repository=self.journal,
            list_wakes=lambda **_kwargs: list(rows),
            list_runners=lambda **_kwargs: list(runner_rows),
        )

    def runners(self, rows):
        return capacity_mission_events.append_terminal_runner_events(
            project=PROJECT,
            task_id=TASK,
            repository=self.journal,
            list_runners=lambda **_kwargs: list(rows),
        )

    def test_failed_pre_runner_wake_is_exact_idempotent_and_headless(self):
        first = self.wakes([failed_wake()])
        replay = self.wakes([failed_wake()])
        self.assertTrue(first["events"][0]["created"])
        self.assertFalse(replay["events"][0]["created"])
        events = self.journal.list_events(
            TASK, project=PROJECT, after_sequence=1, limit=10,
        )
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual("execution_ended", event["event_type"])
        self.assertEqual("capacity", event["source_plane"])
        self.assertEqual("execlease-failed", event["execution_id"])
        self.assertEqual(2, event["generation"])
        self.assertIsNone(event["head_sha"])
        self.assertEqual(
            {
                "wake_id": "wake-failed",
                "terminal_status": "failed",
                "reason_code": "stale_execution_context",
                "receipt_ref": "wake:wake-failed",
            },
            event["payload"],
        )

    def test_cancelled_wake_projects_but_other_eras_and_runner_wakes_do_not(self):
        mission = self.journal.get_item(TASK, project=PROJECT)
        old = float(mission["created_at"]) - 1
        receipt = self.wakes(
            [
                failed_wake("wake-old", requested_at=old),
                failed_wake("wake-runner", runner_session_id="run-existing"),
                failed_wake(
                    "wake-cancelled", status="cancelled", reason="operator_cancel",
                ),
            ],
            runner_rows=[{
                "runner_session_id": "run-existing",
                "task_id": TASK,
            }],
        )
        self.assertEqual(["wake-cancelled"], [row["wake_id"] for row in receipt["events"]])

    def test_allocated_runner_id_is_not_registration_or_liveness(self):
        receipt = self.wakes([
            failed_wake(
                "wake-allocated-only",
                runner_session_id="run-allocated-but-never-registered",
            ),
        ])
        self.assertEqual(
            ["wake-allocated-only"],
            [row["wake_id"] for row in receipt["events"]],
        )
        event = self.journal.list_events(
            TASK, project=PROJECT, after_sequence=1, limit=10,
        )[0]
        self.assertEqual("execution_ended", event["event_type"])
        self.assertEqual("capacity", event["source_plane"])

    def test_capacity_read_and_missing_identity_fail_loudly(self):
        with self.assertRaisesRegex(
            capacity_mission_events.CapacityMissionProjectionError,
            "sqlite_busy",
        ) as unavailable:
            self.wakes([{
                "error": "control_plane_unavailable",
                "reason": "sqlite_busy",
            }])
        self.assertEqual("capacity_read_unavailable", unavailable.exception.code)

        with self.assertRaises(capacity_mission_events.CapacityMissionProjectionError) as missing:
            self.wakes([failed_wake(execution_id="", generation=0)])
        self.assertEqual("execution_identity_missing", missing.exception.code)

        with self.assertRaises(capacity_mission_events.CapacityMissionProjectionError) as cause:
            self.wakes([failed_wake(reason="")])
        self.assertEqual("capacity_failure_cause_missing", cause.exception.code)

    def test_completed_wake_without_runner_is_an_invariant_failure(self):
        with self.assertRaises(capacity_mission_events.CapacityMissionProjectionError) as result:
            self.wakes([failed_wake(status="completed")])
        self.assertEqual("completed_wake_without_runner", result.exception.code)

        mission = self.journal.get_item(TASK, project=PROJECT)
        old = failed_wake(
            "wake-old-completed",
            status="completed",
            requested_at=float(mission["created_at"]) - 1,
        )
        self.assertEqual([], self.wakes([old])["events"])

    def test_capacity_reader_exception_preserves_class_and_message(self):
        def broken_reader(**_kwargs):
            raise sqlite3.OperationalError("database is locked")

        with self.assertRaises(capacity_mission_events.CapacityMissionProjectionError) as result:
            capacity_mission_events.append_terminal_wake_events(
                project=PROJECT,
                task_id=TASK,
                repository=self.journal,
                list_wakes=broken_reader,
            )
        self.assertEqual("capacity_read_failed", result.exception.code)
        self.assertIn("OperationalError: database is locked", str(result.exception))

    def test_all_terminal_runner_outcomes_project_exactly_once(self):
        rows = [
            terminal_runner("run-killed", status="killed"),
            terminal_runner("run-completed", status="completed", generation=4),
            terminal_runner("run-live", status="running", generation=5),
        ]
        first = self.runners(rows)
        replay = self.runners(rows)
        self.assertEqual(2, len(first["events"]))
        self.assertTrue(all(event["created"] for event in first["events"]))
        self.assertTrue(all(not event["created"] for event in replay["events"]))
        events = self.journal.list_events(
            TASK, project=PROJECT, after_sequence=1, limit=10,
        )
        self.assertEqual(
            ["killed", "completed"],
            [event["payload"]["terminal_status"] for event in events],
        )
        self.assertTrue(all(event["source_plane"] == "capacity" for event in events))

    def test_valid_continue_yield_consumes_observed_history_and_suppresses_exit_page(self):
        self.journal.append_event(
            TASK,
            project=PROJECT,
            event_type="agent_yielded",
            source_plane="coordination",
            idempotency_key="yield:review-to-remediation",
            execution_id="execlease-review",
            generation=2,
            head_sha="current-head",
            payload={
                "outcome": "continue",
                "requested_role": "remediation",
                "observed_through": 1,
                "latest_sequence_at_yield": 1,
                "cursor_current": True,
            },
        )
        receipt = self.runners([
            terminal_runner(
                "run-review",
                status="stopped",
                execution_id="execlease-review",
                generation=2,
            ),
        ])
        mission = self.journal.get_item(TASK, project=PROJECT)
        version = int(mission["version"])
        replay = self.runners([
            terminal_runner(
                "run-review",
                status="stopped",
                execution_id="execlease-review",
                generation=2,
            ),
        ])
        replay_mission = self.journal.get_item(TASK, project=PROJECT)
        events = self.journal.list_events(
            TASK, project=PROJECT, after_sequence=1, limit=10,
        )
        self.assertEqual([], receipt["events"])
        self.assertEqual(1, len(receipt["finalized_handoffs"]))
        self.assertEqual("agent_yield_handoff", receipt[
            "finalized_handoffs"][0]["reason"])
        self.assertEqual(["agent_yielded"], [row["event_type"] for row in events])
        self.assertEqual("ACTIVE", mission["state"])
        self.assertEqual("remediation", mission["requested_role"])
        self.assertEqual(1, mission["handled_through"])
        self.assertTrue(replay["finalized_handoffs"][0]["already_finalized"])
        self.assertEqual(version, int(replay_mission["version"]))

    def test_valid_wait_yield_parks_only_after_terminal_ack(self):
        yielded = self.journal.append_event(
            TASK,
            project=PROJECT,
            event_type="agent_yielded",
            source_plane="coordination",
            idempotency_key="yield:review-wait",
            execution_id="execlease-wait",
            generation=2,
            head_sha="current-head",
            payload={
                "outcome": "waiting",
                "requested_role": "review_merge",
                "observed_through": 1,
                "latest_sequence_at_yield": 1,
                "cursor_current": True,
            },
        )
        receipt = self.runners([
            terminal_runner(
                "run-wait",
                status="stopped",
                execution_id="execlease-wait",
                generation=2,
            ),
        ])
        mission = self.journal.get_item(TASK, project=PROJECT)
        self.assertEqual([], receipt["events"])
        self.assertEqual("WAITING", mission["state"])
        self.assertEqual(int(yielded["sequence"]), mission["handled_through"])

    def test_c3_handoff_suppresses_generic_runner_exit_and_leaves_review_actionable(self):
        handoff = self.journal.append_event(
            TASK,
            project=PROJECT,
            event_type="task_changed",
            source_plane="coordination",
            idempotency_key="c3:implementation-review",
            generation=3,
            execution_id="run-implementation",
            head_sha="new-head",
            payload={
                "change_ref": "completion-r114",
                "changed_fields": ["status", "git_state"],
                "command_ref": "complete_claim_terminal_ack",
            },
        )
        receipt = self.runners([
            terminal_runner(
                "run-implementation",
                status="completed",
                execution_id="execlease-implementation",
                generation=3,
            ),
        ])
        mission = self.journal.get_item(TASK, project=PROJECT)
        self.assertEqual([], receipt["events"])
        self.assertEqual("c3_review_handoff", receipt[
            "finalized_handoffs"][0]["reason"])
        self.assertEqual("review_merge", mission["requested_role"])
        self.assertEqual(int(handoff["sequence"]) - 1, mission["handled_through"])

    def test_terminal_runner_missing_identity_fails_instead_of_disappearing(self):
        with self.assertRaises(capacity_mission_events.CapacityMissionProjectionError) as result:
            self.runners([terminal_runner(execution_id="", generation=0)])
        self.assertEqual("execution_identity_missing", result.exception.code)

    def test_journal_rejects_wrong_plane_stale_head_and_missing_cause(self):
        base = {
            "task_id": TASK,
            "project": PROJECT,
            "event_type": "execution_ended",
            "idempotency_key": "execution_ended:wake-invalid",
            "execution_id": "execlease-invalid",
            "generation": 7,
            "payload": {
                "wake_id": "wake-invalid",
                "terminal_status": "failed",
                "reason_code": "no_host",
                "receipt_ref": "wake:wake-invalid",
            },
        }
        with self.assertRaises(MissionJournalError) as plane:
            self.journal.append_event(source_plane="coordination", **base)
        self.assertEqual("invalid_source_plane", plane.exception.code)

        with self.assertRaises(MissionJournalError) as head:
            self.journal.append_event(
                source_plane="capacity", head_sha="stale-head", **base,
            )
        self.assertEqual("stale_execution_head_forbidden", head.exception.code)

        no_cause = dict(base)
        no_cause["idempotency_key"] = "execution_ended:wake-no-cause"
        no_cause["payload"] = {
            "wake_id": "wake-no-cause",
            "terminal_status": "failed",
            "receipt_ref": "wake:wake-no-cause",
        }
        with self.assertRaises(MissionJournalError) as cause:
            self.journal.append_event(source_plane="capacity", **no_cause)
        self.assertEqual("capacity_failure_reference_required", cause.exception.code)

    def test_scoped_runtime_projects_then_starts_once_against_current_task_head(self):
        wake = failed_wake()
        starts: list[dict] = []
        runner_live = False

        class Store:
            @staticmethod
            def validate_autopilot_scope_authority(_authority, **_kwargs):
                return {"allowed": True}

            @staticmethod
            def get_task(_task_id, **_kwargs):
                return {
                    "dependency_state": {"satisfied": True},
                    "git_state": {"head_sha": "current-task-head"},
                }

            @staticmethod
            def task_has_live_execution(_task_id, **_kwargs):
                return runner_live

            @staticmethod
            def list_wake_intents(**_kwargs):
                return [wake]

            @staticmethod
            def list_runner_sessions(**_kwargs):
                return []

        def start(task_id, **kwargs):
            starts.append({"task_id": task_id, **kwargs})
            return {"action": "starting", "starting": True}

        with patch(
            "switchboard.application.mission_bot_v4.runtime."
            "task_execution.start_task",
            side_effect=start,
        ):
            result = run_scoped_mission_tick(
                TASK,
                project=PROJECT,
                scope_project=PROJECT,
                scope_authority={"generation": 1},
                actor="coordinator-test",
                agent_id="coordinator-test",
                journal=self.journal,
                store_mod=Store,
            )
            replay = run_scoped_mission_tick(
                TASK,
                project=PROJECT,
                scope_project=PROJECT,
                scope_authority={"generation": 1},
                actor="coordinator-test",
                agent_id="coordinator-test",
                journal=self.journal,
                store_mod=Store,
            )
        self.assertEqual("start_task", result["action"])
        self.assertEqual("terminal_wake_events_projected", result[
            "capacity_projection"
        ]["wake_projection"]["action"])
        self.assertEqual(1, len(starts))
        self.assertEqual("current-task-head", starts[0]["source_sha"])
        self.assertEqual("block_release", replay["action"])
        self.assertEqual("missing_mission_event", replay["reason"])
        self.assertTrue(replay["release_blocked"])

    def test_scoped_runtime_names_projection_failure_and_does_not_start(self):
        starts: list[dict] = []

        class Store:
            @staticmethod
            def validate_autopilot_scope_authority(_authority, **_kwargs):
                return {"allowed": True}

            @staticmethod
            def get_task(_task_id, **_kwargs):
                return {"dependency_state": {"satisfied": True}}

            @staticmethod
            def task_has_live_execution(_task_id, **_kwargs):
                return False

            @staticmethod
            def list_wake_intents(**_kwargs):
                return [{
                    "error": "control_plane_unavailable",
                    "reason": "database is locked",
                }]

            @staticmethod
            def list_runner_sessions(**_kwargs):
                return []

        with patch(
            "switchboard.application.mission_bot_v4.runtime."
            "task_execution.start_task",
            side_effect=lambda *args, **kwargs: starts.append((args, kwargs)),
        ):
            result = run_scoped_mission_tick(
                TASK,
                project=PROJECT,
                scope_project=PROJECT,
                scope_authority={"generation": 1},
                actor="coordinator-test",
                agent_id="coordinator-test",
                journal=self.journal,
                store_mod=Store,
            )
        self.assertEqual("capacity_projection_failed", result["reason"])
        self.assertEqual("capacity_read_unavailable", result["error"])
        self.assertEqual("missing_data", result["failure_class"])
        self.assertIn("database is locked", result["message"])
        self.assertEqual([], starts)

    def test_scoped_runtime_reports_a_partial_projection_truthfully(self):
        class Store:
            @staticmethod
            def validate_autopilot_scope_authority(_authority, **_kwargs):
                return {"allowed": True}

            @staticmethod
            def get_task(_task_id, **_kwargs):
                return {"dependency_state": {"satisfied": True}}

            @staticmethod
            def task_has_live_execution(_task_id, **_kwargs):
                return False

            @staticmethod
            def list_wake_intents(**_kwargs):
                return [failed_wake()]

            @staticmethod
            def list_runner_sessions(**_kwargs):
                return [{
                    "error": "control_plane_unavailable",
                    "reason": "runner registry locked",
                }]

        result = run_scoped_mission_tick(
            TASK,
            project=PROJECT,
            scope_project=PROJECT,
            scope_authority={"generation": 1},
            actor="coordinator-test",
            agent_id="coordinator-test",
            journal=self.journal,
            store_mod=Store,
        )
        self.assertEqual("capacity_projection_failed", result["reason"])
        self.assertEqual(1, result["mutations"])
        self.assertTrue(result["partial_projection"])
        self.assertEqual(
            2,
            self.journal.get_item(TASK, project=PROJECT)["latest_sequence"],
        )


if __name__ == "__main__":
    unittest.main()
