#!/usr/bin/env python3
"""COORD-113: restore and harden the fenced COORD-110 pager."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT as _ROOT  # noqa: F401
from switchboard.application.commands.task_execution import TaskExecutionError
from switchboard.application.mission_bot_v4 import (
    ScopedMissionWorkerPorts,
    production_ports,
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

        self.journal = MissionJournalRepository(
            connector,
            terminal_verifier=lambda *_args: True,
        )
        self.task = {
            "task_id": "COORD-113",
            "status": "Blocked",  # Deliberately not an admission signal.
            "dependency_state": {"satisfied": True},
            "git_state": {"head_sha": "abc123"},
        }
        self.starts: list[dict] = []
        self.authority = {
            "schema": "switchboard.autopilot_scope_authority.v1",
            "scope_id": "scope-113",
            "lease_id": "lease-113",
            "holder_agent_id": "coordinator-113",
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
            return {"action": "starting", "started": False, "starting": True}

        return ScopedMissionWorkerPorts(
            validate_scope=lambda *_args, **_kwargs: {
                "allowed": self.scope_allowed,
                **({} if self.scope_allowed else {
                    "error": "scope_authority_denied",
                    "reason_codes": ["fence_epoch"],
                }),
            },
            get_task=lambda *_args, **_kwargs: dict(self.task),
            has_live_execution=lambda *_args, **_kwargs: self.runner_live,
            start_task=start,
            journal=self.journal,
        )

    def create(self, *, role="implementation"):
        return self.journal.create_mission(
            "COORD-113", project="switchboard", requested_role=role,
        )

    def tick(self, ports=None):
        return tick_scoped_mission(
            "COORD-113",
            project="switchboard",
            scope_authority=self.authority,
            actor="coordinator-113",
            ports=ports or self.ports(),
        )

    def test_oldest_event_starts_persisted_role_once_and_replay_waits(self):
        self.create(role="review_merge")
        self.journal.append_event(
            "COORD-113",
            project="switchboard",
            event_type="github_changed",
            source_plane="communication",
            idempotency_key="github-113",
            head_sha="abc123",
        )
        first = self.tick()
        replay = self.tick()
        self.assertEqual("start_task", first["action"])
        self.assertEqual(1, first["event_pointer"])
        self.assertEqual("review_merge", self.starts[0]["role"])
        self.assertEqual("runner_live", replay["reason"])
        self.assertEqual(1, len(self.starts))

    def test_yield_launch_carries_the_exact_observed_trigger_event(self):
        self.create(role="review_merge")
        self.task["git_state"]["head_sha"] = "f" * 40
        failure = self.journal.append_event(
            "COORD-113",
            project="switchboard",
            event_type="github_changed",
            source_plane="communication",
            idempotency_key="merge-group-failure-113",
            head_sha="f" * 40,
            external_ref="https://github.test/actions/runs/7",
            payload={
                "object_type": "status",
                "status_context": "Switchboard CI / VM gate",
                "status_state": "failure",
                "target_url": "https://github.test/actions/runs/7",
            },
        )
        yielded = self.journal.append_event(
            "COORD-113",
            project="switchboard",
            event_type="agent_yielded",
            source_plane="coordination",
            idempotency_key="yield-113",
            generation=2,
            execution_id="exec-113",
            payload={
                "outcome": "continue",
                "requested_role": "remediation",
                "observed_through": failure["sequence"],
                "latest_sequence_at_yield": failure["sequence"],
                "cursor_current": True,
            },
        )
        self.journal.update_item(
            "COORD-113",
            project="switchboard",
            state="ACTIVE",
            requested_role="remediation",
            expected_version=self.journal.get_item(
                "COORD-113", project="switchboard",
            )["version"],
            handled_through=yielded["sequence"] - 1,
        )

        result = self.tick()

        self.assertEqual("start_task", result["action"])
        pointer = json.loads(self.starts[0]["instruction"])
        self.assertEqual("agent_yielded", pointer["event_type"])
        self.assertEqual(failure["sequence"], pointer["trigger_event"]["event_sequence"])
        self.assertEqual("failure", pointer["trigger_event"]["payload"]["status_state"])
        self.assertEqual(
            "https://github.test/actions/runs/7",
            pointer["trigger_event"]["external_ref"],
        )
        self.assertEqual(
            {
                "schema": "switchboard.mission_launch_pointer.v4",
                "event_id": failure["event_id"],
                "event_sequence": failure["sequence"],
                "ci_context": "Switchboard CI / VM gate",
                "failure_state": "failure",
                "evidence_url": "https://github.test/actions/runs/7",
                "exact_head_sha": "f" * 40,
            },
            self.starts[0]["mission_launch_pointer"],
        )

    def test_review_yield_launch_carries_verdict_findings_and_exact_head(self):
        self.create(role="review_merge")
        self.task["git_state"].update({
            "head_sha": "e" * 40,
            "pr_url": "https://github.test/pull/1303",
        })
        self.task["review_remediation"] = {"current": {
            "verdict_id": "reviewverdict-dogfood32",
            "remediation_id": "reviewremediation-dogfood32-round-3",
            "source_head_sha": "e" * 40,
            "source_pr_url": "https://github.test/pull/1303",
            "acceptance_criteria": [
                {"id": "dogfood32-block-control-only-advance"},
            ],
        }}
        yielded = self.journal.append_event(
            "COORD-113",
            project="switchboard",
            event_type="agent_yielded",
            source_plane="coordination",
            idempotency_key="review-yield-113",
            generation=6,
            execution_id="exec-review-113",
            payload={
                "outcome": "continue",
                "requested_role": "remediation",
                "observed_through": 0,
                "latest_sequence_at_yield": 0,
                "cursor_current": True,
            },
        )
        self.journal.update_item(
            "COORD-113",
            project="switchboard",
            state="ACTIVE",
            requested_role="remediation",
            expected_version=self.journal.get_item(
                "COORD-113", project="switchboard",
            )["version"],
            handled_through=yielded["sequence"] - 1,
        )

        result = self.tick()

        self.assertEqual("start_task", result["action"])
        self.assertEqual(
            {
                "schema": "switchboard.review_remediation_launch_pointer.v4",
                "event_id": yielded["event_id"],
                "event_sequence": yielded["sequence"],
                "verdict_id": "reviewverdict-dogfood32",
                "remediation_id": "reviewremediation-dogfood32-round-3",
                "finding_ids": ["dogfood32-block-control-only-advance"],
                "evidence_url": "https://github.test/pull/1303",
                "exact_head_sha": "e" * 40,
            },
            self.starts[0]["mission_launch_pointer"],
        )

    def test_scope_dependencies_human_and_runner_each_wait_without_start(self):
        self.create()
        self.scope_allowed = False
        denied = self.tick()
        self.assertEqual("scope_authority_denied", denied["reason"])
        self.assertEqual(["fence_epoch"], denied["reason_codes"])

        self.scope_allowed = True
        self.task["dependency_state"]["satisfied"] = False
        self.assertEqual("dependencies_unmet", self.tick()["reason"])

        self.task["dependency_state"]["satisfied"] = True
        item = self.journal.get_item("COORD-113", project="switchboard")
        self.journal.update_item(
            "COORD-113",
            project="switchboard",
            state="HUMAN",
            requested_role="implementation",
            expected_version=item["version"],
            human_request_id="human-113",
        )
        self.assertEqual("authenticated_agent_request", self.tick()["reason"])

        item = self.journal.get_item("COORD-113", project="switchboard")
        self.journal.update_item(
            "COORD-113",
            project="switchboard",
            state="ACTIVE",
            requested_role="implementation",
            expected_version=item["version"],
        )
        self.runner_live = True
        self.assertEqual("runner_live", self.tick()["reason"])
        self.assertEqual([], self.starts)

    def test_verified_done_is_observed_and_board_blocked_is_ignored(self):
        self.create(role="review_merge")
        item = self.journal.get_item("COORD-113", project="switchboard")
        self.journal.update_item(
            "COORD-113",
            project="switchboard",
            state="DONE",
            requested_role="review_merge",
            expected_version=item["version"],
            handled_through=1,
            terminal_kind="github_merge",
            terminal_ref="abc123",
        )
        self.assertEqual("terminal_provenance", self.tick()["reason"])
        self.assertEqual([], self.starts)

    def test_failed_admission_keeps_cursor_and_reuses_mission_key(self):
        self.create(role="remediation")
        calls: list[dict] = []
        base = self.ports()
        failing = ScopedMissionWorkerPorts(
            validate_scope=base.validate_scope,
            get_task=base.get_task,
            has_live_execution=base.has_live_execution,
            start_task=lambda task_id, **kwargs: (
                calls.append({"task_id": task_id, **kwargs})
                or {"error": "no host", "failure_class": "unreachable_agent"}
            ),
            journal=self.journal,
        )
        first = self.tick(failing)
        second = self.tick(failing)
        self.assertEqual("start_not_admitted", first["reason"])
        self.assertEqual(calls[0]["mission_key"], calls[1]["mission_key"])
        item = self.journal.get_item("COORD-113", project="switchboard")
        self.assertEqual(0, item["handled_through"])

    def test_transitioning_receipt_is_not_admission(self):
        self.create(role="review_merge")
        base = self.ports()
        transitioning = ScopedMissionWorkerPorts(
            validate_scope=base.validate_scope,
            get_task=base.get_task,
            has_live_execution=base.has_live_execution,
            start_task=lambda *_args, **_kwargs: {
                "action": "transitioning",
                "started": False,
                "attached": False,
            },
            journal=self.journal,
        )
        self.assertEqual("start_not_admitted", self.tick(transitioning)["reason"])
        item = self.journal.get_item("COORD-113", project="switchboard")
        self.assertEqual(0, item["handled_through"])

    def test_scope_is_revalidated_at_write_boundary(self):
        self.create()
        validations: list[bool] = []

        def validate(*_args, **_kwargs):
            validations.append(True)
            return (
                {"allowed": True}
                if len(validations) == 1
                else {"allowed": False, "error": "scope_authority_denied"}
            )

        base = self.ports()
        result = self.tick(ScopedMissionWorkerPorts(
            validate_scope=validate,
            get_task=base.get_task,
            has_live_execution=base.has_live_execution,
            start_task=base.start_task,
            journal=self.journal,
        ))
        self.assertEqual("scope_authority_denied", result["reason"])
        self.assertEqual(2, len(validations))
        self.assertEqual([], self.starts)

    def test_missing_task_is_named_and_does_not_start(self):
        self.create()
        base = self.ports()
        result = self.tick(ScopedMissionWorkerPorts(
            validate_scope=base.validate_scope,
            get_task=lambda *_args, **_kwargs: None,
            has_live_execution=base.has_live_execution,
            start_task=base.start_task,
            journal=self.journal,
        ))
        self.assertEqual("task_not_found", result["reason"])
        self.assertEqual([], self.starts)


class ProductionPortsTest(unittest.TestCase):
    def test_ports_use_scope_start_task_and_runner_registry_only(self):
        calls: list[tuple[str, dict]] = []

        class Store:
            @staticmethod
            def validate_autopilot_scope_authority(authority, **kwargs):
                calls.append(("scope", {"authority": authority, **kwargs}))
                return {"allowed": True}

            @staticmethod
            def get_task(task_id, **kwargs):
                calls.append(("task", {"task_id": task_id, **kwargs}))
                return {"task_id": task_id}

            @staticmethod
            def task_has_live_execution(task_id, **kwargs):
                calls.append(("runner_sessions", {"task_id": task_id, **kwargs}))
                return False

        ports = production_ports(
            actor="coordinator-113",
            agent_id="coordinator-113",
            scope_project="switchboard",
            store_mod=Store,
        )
        authority = {"schema": "switchboard.autopilot_scope_authority.v1"}
        self.assertFalse(ports.has_live_execution("QA-1", project="switchboard"))
        with patch(
            "switchboard.application.mission_bot_v4.runtime.task_execution.start_task",
            side_effect=lambda task_id, **kwargs: (
                calls.append(("start_task", {"task_id": task_id, **kwargs}))
                or {"action": "starting", "starting": True}
            ),
        ):
            receipt = ports.start_task(
                "QA-1",
                project="switchboard",
                role="implementation",
                source_sha="abc",
                instruction="{}",
                mission_key="mission-1",
                mission_launch_pointer={"event_id": "missionevent-113"},
                scope_authority=authority,
            )
        self.assertEqual("starting", receipt["action"])
        self.assertEqual(
            ["runner_sessions", "scope", "start_task"],
            [name for name, _detail in calls],
        )
        self.assertEqual(
            {"event_id": "missionevent-113"},
            calls[-1][1]["mission_launch_pointer"],
        )

    def test_scope_refusal_prevents_start_and_preserves_reason_codes(self):
        class Store:
            @staticmethod
            def validate_autopilot_scope_authority(_authority, **_kwargs):
                return {
                    "allowed": False,
                    "error": "scope_authority_denied",
                    "reason_codes": ["expired"],
                }

        ports = production_ports(
            actor="coordinator-113",
            agent_id="coordinator-113",
            scope_project="switchboard",
            store_mod=Store,
        )
        with patch(
            "switchboard.application.mission_bot_v4.runtime.task_execution.start_task",
            side_effect=AssertionError("start_task must not be called"),
        ):
            receipt = ports.start_task(
                "QA-1",
                project="switchboard",
                role="implementation",
                source_sha="",
                instruction="{}",
                mission_key="mission-1",
                scope_authority={
                    "schema": "switchboard.autopilot_scope_authority.v1",
                },
            )
        self.assertTrue(receipt["refused"])
        self.assertEqual("absent_permission", receipt["failure_class"])
        self.assertEqual(["expired"], receipt["reason_codes"])

    def test_typed_task_execution_refusal_is_not_hidden(self):
        class Store:
            @staticmethod
            def validate_autopilot_scope_authority(_authority, **_kwargs):
                return {"allowed": True}

        ports = production_ports(
            actor="coordinator-113",
            agent_id="coordinator-113",
            scope_project="switchboard",
            store_mod=Store,
        )
        with patch(
            "switchboard.application.mission_bot_v4.runtime.task_execution.start_task",
            side_effect=TaskExecutionError(
                "start_refused", "No eligible host.", start_error="no_host",
            ),
        ):
            receipt = ports.start_task(
                "QA-1",
                project="switchboard",
                role="implementation",
                source_sha="",
                instruction="{}",
                mission_key="mission-1",
                scope_authority={
                    "schema": "switchboard.autopilot_scope_authority.v1",
                },
            )
        self.assertTrue(receipt["refused"])
        self.assertEqual("failed_gate", receipt["failure_class"])
        self.assertEqual("No eligible host.", receipt["message"])
        self.assertEqual("no_host", receipt["start_error"])


if __name__ == "__main__":
    unittest.main()
