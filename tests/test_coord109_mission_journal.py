import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT as _ROOT  # noqa: F401
from switchboard.application.commands import mission_journal
from switchboard.domain.execution_liveness import TERMINAL_EXECUTION_STATES
from switchboard.storage.migrations.runner import DDL_MIGRATIONS
from switchboard.storage.repositories.mission_journal import (
    MissionJournalError,
    MissionJournalRepository,
)


MISSION_MIGRATIONS = {
    "0123_mission_items",
    "0124_mission_events",
    "0125_ix_mission_events_task_sequence",
    "0126_ix_mission_events_task_head",
}


class MissionJournalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = {
            project: Path(self.temp.name) / f"{project}.db"
            for project in ("alpha", "beta")
        }
        for path in self.paths.values():
            connection = sqlite3.connect(path)
            try:
                for name, sql in DDL_MIGRATIONS:
                    if name in MISSION_MIGRATIONS:
                        connection.execute(sql)
                connection.commit()
            finally:
                connection.close()

        @contextmanager
        def connector(project):
            connection = sqlite3.connect(
                self.paths[project], timeout=10, check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        self.connector = connector
        self.write_calls = []

        def write_through(project, operation):
            self.write_calls.append(project)
            return operation()

        self.repository = MissionJournalRepository(connector, write_through)

    def tearDown(self):
        self.temp.cleanup()

    def test_restart_recovery(self):
        mission_journal.create_mission(
            "COORD-109", project="alpha", repository=self.repository,
        )
        recovered = MissionJournalRepository(
            self.connector, lambda _project, operation: operation(),
        ).get_item("COORD-109", project="alpha")
        self.assertEqual("ACTIVE", recovered["state"])
        self.assertEqual(1, recovered["latest_sequence"])

    def test_create_is_atomic_and_idempotent(self):
        first = self.repository.create_mission("T-1", project="alpha")
        replay = self.repository.create_mission("T-1", project="alpha")
        self.assertTrue(first["event"]["created"])
        self.assertFalse(replay["event"]["created"])
        self.assertEqual(1, replay["mission"]["latest_sequence"])
        self.assertEqual(2, len(self.write_calls))

    def test_duplicate_append_is_suppressed(self):
        self.repository.create_mission("T-1", project="alpha")
        first = self.repository.append_event(
            "T-1",
            project="alpha",
            event_type="github_changed",
            source_plane="communication",
            idempotency_key="delivery-1",
            head_sha="head",
        )
        replay = self.repository.append_event(
            "T-1",
            project="alpha",
            event_type="github_changed",
            source_plane="communication",
            idempotency_key="delivery-1",
            head_sha="head",
        )
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(first["sequence"], replay["sequence"])

    def test_event_append_is_evidence_only(self):
        with self.assertRaisesRegex(MissionJournalError, "existing mission item"):
            self.repository.append_event(
                "T-1",
                project="alpha",
                event_type="github_changed",
                source_plane="communication",
                idempotency_key="before-start",
                head_sha="head",
            )
        before = self.repository.create_mission(
            "T-1", project="alpha",
        )["mission"]
        self.repository.append_event(
            "T-1",
            project="alpha",
            event_type="github_changed",
            source_plane="communication",
            idempotency_key="after-start",
            head_sha="head",
        )
        after = self.repository.get_item("T-1", project="alpha")
        self.assertEqual(before["state"], after["state"])
        self.assertEqual(before["requested_role"], after["requested_role"])
        self.assertEqual(before["handled_through"], after["handled_through"])
        self.assertEqual(before["version"], after["version"])
        self.assertEqual(2, after["latest_sequence"])

    def test_idempotency_collision_fails_closed(self):
        self.repository.create_mission("T-1", project="alpha")
        self.repository.create_mission("T-2", project="alpha")
        self.repository.append_event(
            "T-1",
            project="alpha",
            event_type="github_changed",
            source_plane="communication",
            idempotency_key="delivery-1",
            payload={"material_fingerprint": "one"},
            head_sha="one",
        )
        for task_id, payload in (
            ("T-2", {"material_fingerprint": "one"}),
            ("T-1", {"material_fingerprint": "two"}),
        ):
            with self.assertRaisesRegex(
                MissionJournalError, "different mission event",
            ):
                self.repository.append_event(
                    task_id,
                    project="alpha",
                    event_type="github_changed",
                    source_plane="communication",
                    idempotency_key="delivery-1",
                    payload=payload,
                    head_sha=str(payload["material_fingerprint"]),
                )

    def test_event_contract_rejects_untyped_payloads_and_wrong_planes(self):
        self.repository.create_mission("T-1", project="alpha")
        for payload in (
            {"retry_count": 2},
            {"retryCount": 2},
            {"ready-to-merge": True},
            {"runner": {"live": True}},
        ):
            with self.assertRaises(MissionJournalError):
                self.repository.append_event(
                    "T-1",
                    project="alpha",
                    event_type="github_changed",
                    source_plane="communication",
                    idempotency_key=f"bad-{len(str(payload))}",
                    payload=payload,
                    head_sha="head",
                )
        with self.assertRaises(MissionJournalError):
            self.repository.append_event(
                "T-1",
                project="alpha",
                event_type="made_up",
                source_plane="communication",
                idempotency_key="bad-type",
            )
        with self.assertRaisesRegex(MissionJournalError, "capacity plane"):
            self.repository.append_event(
                "T-1",
                project="alpha",
                event_type="runner_ended",
                source_plane="communication",
                idempotency_key="bad-plane",
                generation=1,
                execution_id="execution-1",
                payload={"runner_session_id": "runner-1"},
            )
        with self.assertRaisesRegex(
            MissionJournalError, "execution_id and positive generation",
        ):
            self.repository.append_event(
                "T-1",
                project="alpha",
                event_type="runner_ended",
                source_plane="capacity",
                idempotency_key="bad-runner",
                payload={"runner_session_id": "runner-1"},
            )

    def test_event_contract_rejects_invalid_typed_values(self):
        self.repository.create_mission("T-1", project="alpha")
        cases = (
            (
                "task_changed",
                "coordination",
                {"external_ref": "change-1", "payload": {"changed_fields": [["state"]]}},
                "invalid_task_change",
            ),
            (
                "runner_ended",
                "capacity",
                {
                    "execution_id": "execution-1",
                    "generation": 1,
                    "payload": {"runner_session_id": "runner-1"},
                },
                "invalid_runner_terminal_status",
            ),
            (
                "agent_yielded",
                "coordination",
                {
                    "execution_id": "execution-1",
                    "generation": 1,
                    "payload": {
                        "outcome": "done",
                        "requested_role": "implementation",
                        "observed_through": 1,
                    },
                },
                "invalid_yield_outcome",
            ),
            (
                "agent_yielded",
                "coordination",
                {
                    "execution_id": "execution-1",
                    "generation": 1,
                    "payload": {
                        "outcome": "continue",
                        "requested_role": "made_up",
                        "observed_through": 1,
                    },
                },
                "invalid_role",
            ),
            (
                "agent_yielded",
                "coordination",
                {
                    "execution_id": "execution-1",
                    "generation": 1,
                    "payload": {
                        "outcome": "waiting",
                        "requested_role": "implementation",
                        "observed_through": -1,
                    },
                },
                "invalid_event_cursor",
            ),
            (
                "observation_due",
                "coordination",
                {"payload": {"wait_started_at": 0}},
                "wait_reference_required",
            ),
            (
                "observation_due",
                "coordination",
                {"payload": {"wait_started_at": 2, "due_at": 1}},
                "invalid_observation_due",
            ),
        )
        for index, (event_type, source_plane, kwargs, code) in enumerate(cases):
            with self.subTest(event_type=event_type, code=code):
                with self.assertRaises(MissionJournalError) as raised:
                    self.repository.append_event(
                        "T-1",
                        project="alpha",
                        event_type=event_type,
                        source_plane=source_plane,
                        idempotency_key=f"invalid-value-{index}",
                        **kwargs,
                    )
                self.assertEqual(code, raised.exception.code)

    def test_runner_events_reuse_the_canonical_capacity_terminal_vocabulary(self):
        self.repository.create_mission("T-1", project="alpha")
        self.assertIn("lost", TERMINAL_EXECUTION_STATES)
        for index, status in enumerate(sorted(TERMINAL_EXECUTION_STATES)):
            event = self.repository.append_event(
                "T-1",
                project="alpha",
                event_type="runner_ended",
                source_plane="capacity",
                idempotency_key=f"runner-terminal-{index}",
                execution_id=f"execution-{index}",
                generation=1,
                payload={
                    "runner_session_id": f"runner-{index}",
                    "terminal_status": status,
                },
            )
            self.assertEqual(status, event["payload"]["terminal_status"])
        with self.assertRaises(MissionJournalError) as raised:
            self.repository.append_event(
                "T-1",
                project="alpha",
                event_type="runner_ended",
                source_plane="capacity",
                idempotency_key="runner-terminal-noncanonical",
                execution_id="execution-noncanonical",
                generation=1,
                payload={
                    "runner_session_id": "runner-noncanonical",
                    "terminal_status": "gone",
                },
            )
        self.assertEqual("invalid_runner_terminal_status", raised.exception.code)

    def test_sequences_are_monotonic_per_mission(self):
        self.repository.create_mission("T-1", project="alpha")
        values = [
            self.repository.append_event(
                "T-1",
                project="alpha",
                event_type="task_changed",
                source_plane="coordination",
                idempotency_key=f"k-{index}",
                external_ref=f"change-{index}",
            )["sequence"]
            for index in range(3)
        ]
        self.assertEqual([2, 3, 4], values)

    def test_concurrent_sequences_and_idempotency_are_atomic(self):
        concurrent = MissionJournalRepository(
            self.connector, lambda _project, operation: operation(),
        )
        concurrent.create_mission("T-1", project="alpha")
        keys = [f"delivery-{index // 2}" for index in range(24)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(
                lambda key: concurrent.append_event(
                    "T-1",
                    project="alpha",
                    event_type="github_changed",
                    source_plane="communication",
                    idempotency_key=key,
                    head_sha="head",
                ),
                keys,
            ))
        created = sorted(
            row["sequence"] for row in rows if row["created"]
        )
        self.assertEqual(list(range(2, 14)), created)
        self.assertEqual(12, len({row["event_id"] for row in rows}))

    def test_default_single_writer_keeps_each_transaction_whole(self):
        path = Path(self.temp.name) / "single-writer.db"
        connection = sqlite3.connect(path)
        try:
            for name, sql in DDL_MIGRATIONS:
                if name in MISSION_MIGRATIONS:
                    connection.execute(sql)
            connection.commit()
        finally:
            connection.close()

        resolution = {"db": str(path), "lifecycle_status": "active"}
        with (
            patch.dict(os.environ, {"PM_SQLITE_SINGLE_WRITER": "1"}),
            patch("db.connection._resolve", return_value=resolution),
        ):
            repository = MissionJournalRepository()
            repository.create_mission("T-1", project="alpha")
            with ThreadPoolExecutor(max_workers=8) as pool:
                rows = list(pool.map(
                    lambda index: repository.append_event(
                        "T-1",
                        project="alpha",
                        event_type="github_changed",
                        source_plane="communication",
                        idempotency_key=f"writer-{index}",
                        head_sha="head",
                    ),
                    range(16),
                ))
            self.assertEqual(
                list(range(2, 18)), sorted(row["sequence"] for row in rows),
            )

            def update(role):
                try:
                    return repository.update_item(
                        "T-1",
                        project="alpha",
                        state="WAITING",
                        requested_role=role,
                        expected_version=1,
                        handled_through=17,
                    )
                except MissionJournalError as exc:
                    return exc.code

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    update, ("implementation", "review_merge"),
                ))
            self.assertEqual(1, sum(isinstance(result, dict) for result in results))
            self.assertEqual(1, results.count("stale_row_version"))

    def test_project_isolation(self):
        for project in ("alpha", "beta"):
            mission_journal.create_mission(
                "T-1", project=project, repository=self.repository,
            )
        alpha = self.repository.update_item(
            "T-1",
            project="alpha",
            state="WAITING",
            requested_role="implementation",
            expected_version=1,
            handled_through=1,
        )
        self.assertEqual("WAITING", alpha["state"])
        self.assertEqual(
            "ACTIVE", self.repository.get_item("T-1", project="beta")["state"],
        )

    def test_stale_row_version_is_rejected(self):
        mission_journal.create_mission(
            "T-1", project="alpha", repository=self.repository,
        )
        self.repository.update_item(
            "T-1",
            project="alpha",
            state="WAITING",
            requested_role="implementation",
            expected_version=1,
            handled_through=1,
        )
        with self.assertRaisesRegex(MissionJournalError, "current version 2"):
            self.repository.update_item(
                "T-1",
                project="alpha",
                state="ACTIVE",
                requested_role="remediation",
                expected_version=1,
            )

    def test_create_rolls_back_if_start_event_cannot_persist(self):
        connection = sqlite3.connect(self.paths["alpha"])
        try:
            connection.execute(
                "CREATE TRIGGER reject_failed_mission BEFORE INSERT ON mission_events "
                "WHEN NEW.task_id='FAIL-1' BEGIN "
                "SELECT RAISE(ABORT, 'event rejected'); END"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(sqlite3.DatabaseError):
            self.repository.create_mission("FAIL-1", project="alpha")
        connection = sqlite3.connect(self.paths["alpha"])
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM mission_items WHERE task_id='FAIL-1'",
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(0, count)

    def test_handled_cursor_cannot_regress(self):
        self.repository.create_mission("T-1", project="alpha")
        self.repository.append_event(
            "T-1",
            project="alpha",
            event_type="task_changed",
            source_plane="coordination",
            idempotency_key="task-change",
            external_ref="change-1",
        )
        current = self.repository.update_item(
            "T-1",
            project="alpha",
            state="WAITING",
            requested_role="implementation",
            expected_version=1,
            handled_through=2,
        )
        with self.assertRaisesRegex(MissionJournalError, "cannot move backwards"):
            self.repository.update_item(
                "T-1",
                project="alpha",
                state="ACTIVE",
                requested_role="implementation",
                expected_version=current["version"],
                handled_through=1,
            )

    def test_done_requires_terminal_authority_and_is_immutable(self):
        mission_journal.create_mission(
            "T-1", project="alpha", repository=self.repository,
        )
        with self.assertRaises(MissionJournalError):
            self.repository.update_item(
                "T-1",
                project="alpha",
                state="DONE",
                requested_role="review_merge",
                expected_version=1,
                terminal_kind="github_merge",
                terminal_ref="sha",
            )
        verified_repository = MissionJournalRepository(
            self.connector,
            lambda _project, operation: operation(),
            terminal_verifier=lambda project, task, kind, ref: (
                project, task, kind, ref
            ) == ("alpha", "T-1", "github_merge", "sha"),
        )
        done = verified_repository.update_item(
            "T-1",
            project="alpha",
            state="DONE",
            requested_role="review_merge",
            expected_version=1,
            handled_through=1,
            terminal_kind="github_merge",
            terminal_ref="sha",
        )
        self.assertEqual("DONE", done["state"])
        with self.assertRaisesRegex(MissionJournalError, "cannot be rewritten"):
            verified_repository.update_item(
                "T-1",
                project="alpha",
                state="ACTIVE",
                requested_role="review_merge",
                expected_version=done["version"],
                handled_through=1,
            )

    def test_human_state_requires_exact_request_reference(self):
        self.repository.create_mission("T-1", project="alpha")
        with self.assertRaises(MissionJournalError):
            self.repository.update_item(
                "T-1",
                project="alpha",
                state="HUMAN",
                requested_role="implementation",
                expected_version=1,
                handled_through=1,
            )
        human = self.repository.update_item(
            "T-1",
            project="alpha",
            state="HUMAN",
            requested_role="implementation",
            expected_version=1,
            handled_through=1,
            human_request_id="attention-1",
        )
        self.assertEqual("attention-1", human["human_request_id"])

    def test_terminal_event_requires_verified_provenance(self):
        self.repository.create_mission("T-1", project="alpha")
        payload = {"terminal_kind": "github_merge", "terminal_ref": "sha"}
        with self.assertRaisesRegex(MissionJournalError, "already-persisted"):
            self.repository.append_event(
                "T-1",
                project="alpha",
                event_type="terminal_provenance_persisted",
                source_plane="coordination",
                idempotency_key="terminal-1",
                payload=payload,
            )
        verified = MissionJournalRepository(
            self.connector,
            lambda _project, operation: operation(),
            terminal_verifier=lambda project, task, kind, ref: (
                project, task, kind, ref
            ) == ("alpha", "T-1", "github_merge", "sha"),
        )
        event = verified.append_event(
            "T-1",
            project="alpha",
            event_type="terminal_provenance_persisted",
            source_plane="coordination",
            idempotency_key="terminal-1",
            payload=payload,
        )
        self.assertTrue(event["created"])


if __name__ == "__main__":
    unittest.main()
