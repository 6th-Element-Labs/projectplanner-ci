#!/usr/bin/env python3
"""Mission Bot v5 is the small ADR-0008 pager and production selection."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT

from coordinator_daemon import DaemonConfig, summarize_scope_result
from scoped_completion_coordinator import ScopedCompletionCoordinator
from switchboard.application.mission_bot_v5 import (
    ScopedMissionWorkerPorts,
    production_ports,
    project_ci_remediation,
    project_terminal_provenance,
    tick_scoped_mission,
)
from switchboard.application.mission_bot_v5.coordinator import (
    V5ScopedCompletionCoordinator,
)
from switchboard.connect.execution_assignment import build_execution_assignment
from switchboard.domain.mission_bot_v5 import decide_mission_transition
from switchboard.storage.migrations.runner import DDL_MIGRATIONS
from switchboard.storage.repositories.mission_journal import MissionJournalRepository
from switchboard.storage.repositories.mission_launch_attempts import (
    MissionLaunchAttemptRepository,
)


class ControllerTest(unittest.TestCase):
    def context(self, **changes):
        value = {
            "scope_active": True,
            "terminal_provenance": False,
            "dependencies_satisfied": True,
            "mission_state": "ACTIVE",
            "runner_live": False,
            "requested_role": "implementation",
            "handled_through": 0,
            "latest_sequence": 1,
        }
        value.update(changes)
        return value

    def test_complete_rule_and_precedence(self):
        self.assertEqual(
            "scope_inactive",
            decide_mission_transition(self.context(scope_active=False))["reason"],
        )
        self.assertEqual(
            "terminal_provenance",
            decide_mission_transition(self.context(terminal_provenance=True))["reason"],
        )
        self.assertEqual(
            "dependencies_unmet",
            decide_mission_transition(self.context(dependencies_satisfied=False))["reason"],
        )
        self.assertEqual(
            "human_requested",
            decide_mission_transition(self.context(mission_state="HUMAN"))["reason"],
        )
        self.assertEqual(
            "runner_live",
            decide_mission_transition(self.context(runner_live=True))["reason"],
        )
        started = decide_mission_transition(self.context(requested_role="remediation"))
        self.assertEqual("start_task", started["action"])
        self.assertEqual("remediation", started["requested_role"])
        self.assertEqual(1, started["event_pointer"])
        self.assertEqual(
            "no_unhandled_event",
            decide_mission_transition(self.context(handled_through=1))["reason"],
        )

    def test_non_authoritative_noise_is_ignored(self):
        decision = decide_mission_transition(self.context(
            board_status="Blocked",
            claim_live=True,
            work_session_live=True,
            host_trusted=False,
            authorization_required=True,
            wake_pending=True,
            message_timed_out=True,
            ci_state="failure",
        ))
        self.assertEqual("start_task", decision["action"])


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "mission-v5.db"

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
        self.journal.create_mission(
            "BOT-5", project="switchboard", requested_role="implementation",
        )
        self.starts = []
        self.live = False

    def tearDown(self):
        self.temp.cleanup()

    def ports(self):
        def start(task_id, **kwargs):
            self.starts.append({"task_id": task_id, **kwargs})
            self.live = True
            return {"action": "starting", "starting": True}

        return ScopedMissionWorkerPorts(
            validate_scope=lambda *_args, **_kwargs: {"allowed": True},
            get_task=lambda *_args, **_kwargs: {
                "task_id": "BOT-5",
                "status": "Blocked",
                "dependency_state": {"satisfied": True},
                "git_state": {},
            },
            has_live_execution=lambda *_args, **_kwargs: self.live,
            start_task=start,
            journal=self.journal,
        )

    def test_launch_is_identity_and_cursor_only(self):
        result = tick_scoped_mission(
            "BOT-5",
            project="switchboard",
            scope_authority={"generation": 3, "fence_epoch": 2},
            actor="coordinator",
            ports=self.ports(),
        )
        self.assertEqual("start_task", result["action"])
        self.assertTrue(result["mission_key"].startswith("v5:"))
        pointer = json.loads(self.starts[0]["instruction"])
        self.assertEqual({
            "schema": "switchboard.mission_pointer.v5",
            "project": "switchboard",
            "task_id": "BOT-5",
            "event_sequence": 1,
        }, pointer)
        self.assertNotIn("mission_launch_pointer", self.starts[0])
        self.assertNotIn("findings", self.starts[0])
        self.assertNotIn("dossier", self.starts[0])

    def test_runner_sessions_liveness_prevents_start(self):
        self.live = True
        result = tick_scoped_mission(
            "BOT-5",
            project="switchboard",
            scope_authority={"generation": 3, "fence_epoch": 2},
            actor="coordinator",
            ports=self.ports(),
        )
        self.assertEqual("runner_live", result["reason"])
        self.assertEqual([], self.starts)

    def test_launch_failures_back_off_and_exhaust_durably(self):
        @contextmanager
        def connector(_project):
            connection = sqlite3.connect(Path(self.temp.name) / "attempts.db")
            connection.row_factory = sqlite3.Row
            connection.execute(next(
                sql for name, sql in DDL_MIGRATIONS
                if name == "0137_mission_launch_attempts"
            ))
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()

        attempts = MissionLaunchAttemptRepository(
            connector, write_through=lambda _project, write: write(),
        )
        now = [100.0]

        def refused(_task_id, **_kwargs):
            return {"error": "no_capacity", "start_error": "cli_busy"}

        ports = self.ports()
        ports = ScopedMissionWorkerPorts(
            validate_scope=ports.validate_scope,
            get_task=ports.get_task,
            has_live_execution=lambda *_args, **_kwargs: False,
            start_task=refused,
            journal=self.journal,
            launch_attempts=attempts,
            clock=lambda: now[0],
            max_launch_attempts=2,
            retry_base_seconds=10,
        )
        first = tick_scoped_mission(
            "BOT-5", project="switchboard",
            scope_authority={"generation": 3}, actor="coordinator", ports=ports,
        )
        self.assertEqual("start_not_admitted", first["reason"])
        self.assertEqual(1, first["retry_count"])
        self.assertEqual(110.0, first["next_retry_at"])
        backed_off = tick_scoped_mission(
            "BOT-5", project="switchboard",
            scope_authority={"generation": 3}, actor="coordinator", ports=ports,
        )
        self.assertEqual("launch_retry_backoff", backed_off["reason"])
        now[0] = 110.0
        exhausted = tick_scoped_mission(
            "BOT-5", project="switchboard",
            scope_authority={"generation": 3}, actor="coordinator", ports=ports,
        )
        self.assertEqual("launch_retry_exhausted", exhausted["reason"])
        self.assertEqual(2, exhausted["retry_count"])
        self.assertEqual("cli_busy", exhausted["start_error"])


class CiRemediationTest(unittest.TestCase):
    def test_two_exact_head_failures_route_then_third_parks(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ci-v5.db"

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
                finally:
                    connection.close()

            journal = MissionJournalRepository(
                connector, write_through=lambda _project, write: write(),
            )
            journal.create_mission("CI-1", project="switchboard")
            for index, head in enumerate(("a" * 40, "b" * 40, "c" * 40), 1):
                journal.append_event(
                    "CI-1", project="switchboard", event_type="github_changed",
                    source_plane="communication", idempotency_key=f"ci-fail-{index}",
                    head_sha=head, external_ref=f"check-{index}",
                    payload={
                        "repository": "org/repo", "object_type": "check_run",
                        "object_id": index, "material_fingerprint": str(index),
                        "status_context": "test", "status_state": "failure",
                    },
                )
                result = project_ci_remediation(
                    "CI-1", project="switchboard",
                    task={"git_state": {"head_sha": head}}, journal=journal,
                    max_attempts=2,
                )
                if index < 3:
                    self.assertEqual(
                        "ci_failure_routed_to_remediation", result["reason"],
                    )
                    if index == 1:
                        journal.append_event(
                            "CI-1", project="switchboard",
                            event_type="github_changed",
                            source_plane="communication",
                            idempotency_key="ci-pass-1", head_sha=head,
                            external_ref="check-pass-1",
                            payload={
                                "repository": "org/repo",
                                "object_type": "check_run", "object_id": 10,
                                "material_fingerprint": "pass-1",
                                "status_context": "test", "status_state": "success",
                            },
                        )
                        superseded = project_ci_remediation(
                            "CI-1", project="switchboard",
                            task={"git_state": {"head_sha": head}}, journal=journal,
                            max_attempts=2,
                        )
                        self.assertEqual(
                            "current_head_ci_failure_missing", superseded["reason"],
                        )
                else:
                    self.assertEqual("ci_remediation_exhausted", result["reason"])
            item = journal.get_item("CI-1", project="switchboard")
            self.assertEqual("WAITING", item["state"])
            self.assertEqual("remediation", item["requested_role"])


class TerminalProjectionTest(unittest.TestCase):
    def test_verified_offline_done_closes_mission(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "terminal-v5.db"

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
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, status TEXT)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS task_git_state ("
                    "task_id TEXT PRIMARY KEY, merged_sha TEXT, "
                    "in_main_content INTEGER NOT NULL DEFAULT 0, "
                    "evidence_json TEXT NOT NULL DEFAULT '{}')"
                )
                try:
                    yield connection
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()

            journal = MissionJournalRepository(
                connector,
                write_through=lambda _project, write: write(),
            )
            journal.create_mission(
                "CANARY-1", project="v5canary", requested_role="implementation",
            )
            evidence_hash = "a" * 64
            evidence = {
                "offline_evidence": {
                    "evidence_hash": evidence_hash,
                    "verifier": "switchboard/v5-canary",
                    "reviewed_at": 1.0,
                }
            }
            with connector("v5canary") as connection:
                connection.execute(
                    "INSERT INTO tasks(task_id,status) VALUES (?,?)",
                    ("CANARY-1", "Done"),
                )
                connection.execute(
                    "INSERT INTO task_git_state(task_id,evidence_json) VALUES (?,?)",
                    ("CANARY-1", json.dumps(evidence)),
                )

            result = project_terminal_provenance(
                "CANARY-1",
                project="v5canary",
                actor="switchboard/v5-canary",
                journal=journal,
                task_reader=lambda *_args, **_kwargs: {
                    "task_id": "CANARY-1",
                    "status": "Done",
                    "git_state": {"evidence": evidence},
                },
            )
            item = journal.get_item("CANARY-1", project="v5canary")
            self.assertTrue(result["projected"])
            self.assertEqual("offline", result["terminal_kind"])
            self.assertEqual(evidence_hash, result["terminal_ref"])
            self.assertEqual("DONE", item["state"])
            self.assertEqual("offline", item["terminal_kind"])
            self.assertEqual(evidence_hash, item["terminal_ref"])


class ProductionPortTest(unittest.TestCase):
    def test_fenced_scope_does_not_require_coordinator_agent_registration(self):
        class Store:
            @staticmethod
            def validate_autopilot_scope_authority(*_args, **_kwargs):
                return {"allowed": True}

        ports = production_ports(
            actor="switchboard/mission-bot-v5",
            agent_id="switchboard/mission-bot-v5/v5/coordinator",
            scope_project="v5canary",
            store_mod=Store(),
        )
        with patch(
            "switchboard.application.mission_bot_v5.runtime.task_execution.start_task",
            return_value={"action": "starting", "starting": True},
        ) as start:
            receipt = ports.start_task(
                "WAVE-1",
                project="v5canary",
                role="implementation",
                source_sha="",
                instruction="{}",
                mission_key="v5:1:WAVE-1:1:implementation",
                scope_authority={"generation": 1, "fence_epoch": 1},
            )

        self.assertTrue(receipt["starting"])
        self.assertTrue(start.call_args.kwargs["operator_launch_authorized"])
        self.assertTrue(start.call_args.kwargs["scope_launch_authorized"])


class ProductionSelectionTest(unittest.TestCase):
    def test_daemon_selects_v5_and_v5_has_no_v4_dependency(self):
        daemon = (ROOT / "coordinator_daemon.py").read_text()
        coordinator = (
            ROOT / "src/switchboard/application/mission_bot_v5/coordinator.py"
        ).read_text()
        self.assertIn("V5ScopedCompletionCoordinator", daemon)
        self.assertNotIn("V4ScopedCompletionCoordinator", daemon)
        self.assertNotIn("mission_bot_v4", coordinator)

    def test_v5_assignment_exposes_context_and_yield_tools(self):
        contract = build_execution_assignment(
            task_id="BOT-5",
            assignment={"assignment_id": "assignment-v5"},
            lifecycle={
                "role": "implementation",
                "execution_id": "exec-v5",
                "generation": 1,
                "head_sha": "",
                "mission_key": "v5:1:BOT-5:1:implementation",
            },
        )
        self.assertEqual(
            "get_mission_context", contract["typed_tools"]["mission_context"],
        )
        self.assertEqual("yield_mission", contract["typed_tools"]["mission_yield"])
        self.assertNotIn("stale_assignment", contract["typed_tools"])

    def test_v5_concurrency_uses_live_runner_sessions(self):
        class Store:
            @staticmethod
            def list_runner_sessions(**_kwargs):
                return [
                    {
                        "runner_session_id": f"run-{index}", "status": "running",
                        "heartbeat_at": 100.0, "heartbeat_ttl_s": 60,
                    }
                    for index in range(2)
                ]

        coordinator = V5ScopedCompletionCoordinator(
            DaemonConfig(mission_bot_v5_max_concurrency=3),
            store_mod=Store(), agent_id="v5", clock=lambda: 100.0,
        )
        candidates = [
            {"task_id": f"TASK-{index}", "task_project": "switchboard"}
            for index in range(4)
        ]
        with patch.object(
            ScopedCompletionCoordinator, "_scope_candidates",
            return_value=candidates,
        ):
            selected = coordinator._scope_candidates({}, {})
        self.assertEqual(["TASK-0"], [row["task_id"] for row in selected])

    def test_v5_scope_lease_needs_no_agent_registration(self):
        calls = []

        class Store:
            @staticmethod
            def acquire_autopilot_scope_lease(scope_id, **kwargs):
                calls.append({"scope_id": scope_id, **kwargs})
                return {"scope_id": scope_id, "generation": 1, "fence_epoch": 1}

        coordinator = V5ScopedCompletionCoordinator(
            DaemonConfig(), store_mod=Store(), agent_id="v5", clock=lambda: 100.0,
        )
        receipt = coordinator._acquire_scope_authority(
            "switchboard", {"scope_id": "scope-v5"},
        )
        self.assertEqual("scope-v5", receipt["scope_id"])
        self.assertFalse(calls[0]["registration_required"])

    def test_scope_summary_keeps_v5_launch_failure(self):
        summary = summarize_scope_result({
            "status": "running",
            "receipts": [{
                "task_id": "BOT-5", "status": "completion_tick",
                "completion": {
                    "schema": "switchboard.mission_worker_tick.v5",
                    "task_id": "BOT-5", "action": "wait",
                    "reason": "start_not_admitted", "retry_count": 1,
                    "next_retry_at": 123.0, "start_error": "cli_busy",
                },
            }],
        })
        receipt = summary["receipts"][0]
        self.assertEqual("start_not_admitted", receipt["tick_reason"])
        self.assertEqual(1, receipt["retry_count"])
        self.assertEqual("cli_busy", receipt["start_error"])


if __name__ == "__main__":
    unittest.main()
