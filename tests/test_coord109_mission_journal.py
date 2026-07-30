import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from path_setup import ROOT as _ROOT  # noqa: F401
from switchboard.application.commands import mission_journal
from switchboard.storage.migrations.runner import DDL_MIGRATIONS
from switchboard.storage.repositories.mission_journal import (
    MissionJournalError,
    MissionJournalRepository,
)


class MissionJournalTest(unittest.TestCase):
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

    def tearDown(self):
        self.temp.cleanup()

    def test_restart_recovery(self):
        mission_journal.create_mission("COORD-109", project="alpha", repository=self.repository)
        recovered = MissionJournalRepository(self.repository._connector).get_item(
            "COORD-109", project="alpha"
        )
        self.assertEqual("ACTIVE", recovered["state"])
        self.assertEqual(1, recovered["latest_sequence"])

    def test_duplicate_append_is_suppressed(self):
        first = self.repository.append_event(
            "T-1", project="alpha", event_type="github_changed",
            source_plane="communication", idempotency_key="delivery-1",
        )
        replay = self.repository.append_event(
            "T-1", project="alpha", event_type="github_changed",
            source_plane="communication", idempotency_key="delivery-1",
        )
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(first["sequence"], replay["sequence"])

    def test_sequences_are_monotonic_per_mission(self):
        values = [
            self.repository.append_event(
                "T-1", project="alpha", event_type="task_changed",
                source_plane="coordination", idempotency_key=f"k-{index}",
            )["sequence"]
            for index in range(3)
        ]
        self.assertEqual([1, 2, 3], values)

    def test_history_is_cursor_paginated_and_bounded(self):
        for index in range(4):
            self.repository.append_event(
                "T-1", project="alpha", event_type="task_changed",
                source_plane="coordination", idempotency_key=f"page-{index}",
                payload={"index": index},
            )
        page = self.repository.list_events(
            "T-1", project="alpha", after_sequence=1, limit=2,
        )
        self.assertEqual([2, 3], [event["sequence"] for event in page])
        self.assertEqual([1, 2], [event["payload"]["index"] for event in page])

    def test_project_isolation(self):
        for project in ("alpha", "beta"):
            mission_journal.create_mission("T-1", project=project, repository=self.repository)
        alpha = self.repository.update_item(
            "T-1", project="alpha", state="WAITING", requested_role="implementation",
            expected_version=1, handled_through=1,
        )
        self.assertEqual("WAITING", alpha["state"])
        self.assertEqual("ACTIVE", self.repository.get_item("T-1", project="beta")["state"])

    def test_stale_generation_is_rejected(self):
        mission_journal.create_mission("T-1", project="alpha", repository=self.repository)
        self.repository.update_item(
            "T-1", project="alpha", state="WAITING", requested_role="implementation",
            expected_version=1, handled_through=1,
        )
        with self.assertRaisesRegex(MissionJournalError, "current version 2"):
            self.repository.update_item(
                "T-1", project="alpha", state="ACTIVE", requested_role="remediation",
                expected_version=1,
            )

    def test_done_requires_terminal_authority(self):
        mission_journal.create_mission("T-1", project="alpha", repository=self.repository)
        with self.assertRaises(MissionJournalError):
            self.repository.update_item(
                "T-1", project="alpha", state="DONE", requested_role="review_merge",
                expected_version=1, terminal_kind="github_merge", terminal_ref="sha",
                authority="agent",
            )
        done = self.repository.update_item(
            "T-1", project="alpha", state="DONE", requested_role="review_merge",
            expected_version=1, handled_through=1, terminal_kind="github_merge",
            terminal_ref="sha", authority="canonical_provenance_projector",
        )
        self.assertEqual("DONE", done["state"])

    def test_abnormal_terminal_is_one_same_role_wake_edge(self):
        mission_journal.create_mission(
            "T-1", project="alpha", requested_role="implementation",
            repository=self.repository,
        )
        item = self.repository.get_item("T-1", project="alpha")
        self.repository.update_item(
            "T-1", project="alpha", state="ACTIVE",
            requested_role="implementation", expected_version=item["version"],
            handled_through=1,
        )
        first = self.repository.record_runner_terminal(
            "T-1", project="alpha", runner_session_id="runner-1",
            execution_id="execution-1", generation=1, status="failed",
        )
        replay = self.repository.record_runner_terminal(
            "T-1", project="alpha", runner_session_id="runner-1",
            execution_id="execution-1", generation=1, status="failed",
        )
        item = self.repository.get_item("T-1", project="alpha")
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual("ACTIVE", item["state"])
        self.assertEqual("implementation", item["requested_role"])
        self.assertEqual(1, item["handled_through"])
        self.assertEqual(2, item["latest_sequence"])

    def test_c3_terminal_switches_to_review_without_diagnosing_github(self):
        mission_journal.create_mission(
            "T-1", project="alpha", requested_role="implementation",
            repository=self.repository,
        )
        item = self.repository.get_item("T-1", project="alpha")
        self.repository.update_item(
            "T-1", project="alpha", state="ACTIVE",
            requested_role="implementation", expected_version=item["version"],
            handled_through=1,
        )
        result = self.repository.record_runner_terminal(
            "T-1", project="alpha", runner_session_id="runner-1",
            execution_id="execution-1", generation=1, status="completed",
            accepted_role="review_merge",
        )
        item = self.repository.get_item("T-1", project="alpha")
        self.assertEqual("c3_completion", result["payload"]["handoff_kind"])
        self.assertEqual("ACTIVE", item["state"])
        self.assertEqual("review_merge", item["requested_role"])
        self.assertEqual(1, item["handled_through"])
        self.assertEqual(2, item["latest_sequence"])

    def test_current_wait_yield_becomes_waiting_only_after_terminal_receipt(self):
        mission_journal.create_mission(
            "T-1", project="alpha", requested_role="review_merge",
            repository=self.repository,
        )
        yielded = self.repository.append_event(
            "T-1", project="alpha", event_type="agent_yielded",
            source_plane="coordination", idempotency_key="yield-1",
            execution_id="execution-1", generation=1,
            payload={
                "cursor_current": True,
                "outcome": "waiting",
                "requested_role": "review_merge",
            },
        )
        item = self.repository.get_item("T-1", project="alpha")
        self.repository.update_item(
            "T-1", project="alpha", state="ACTIVE",
            requested_role="review_merge", expected_version=item["version"],
            handled_through=yielded["sequence"],
        )
        result = self.repository.record_runner_terminal(
            "T-1", project="alpha", runner_session_id="runner-1",
            execution_id="execution-1", generation=1, status="completed",
        )
        item = self.repository.get_item("T-1", project="alpha")
        self.assertEqual("agent_yield", result["payload"]["handoff_kind"])
        self.assertEqual("WAITING", item["state"])
        self.assertEqual(item["latest_sequence"], item["handled_through"])

    def test_event_after_wait_yield_remains_unhandled_after_terminal_receipt(self):
        mission_journal.create_mission(
            "T-1", project="alpha", requested_role="review_merge",
            repository=self.repository,
        )
        yielded = self.repository.append_event(
            "T-1", project="alpha", event_type="agent_yielded",
            source_plane="coordination", idempotency_key="yield-1",
            execution_id="execution-1", generation=1,
            payload={
                "cursor_current": True,
                "outcome": "waiting",
                "requested_role": "review_merge",
            },
        )
        item = self.repository.get_item("T-1", project="alpha")
        self.repository.update_item(
            "T-1", project="alpha", state="ACTIVE",
            requested_role="review_merge", expected_version=item["version"],
            handled_through=yielded["sequence"],
        )
        changed = self.repository.append_event(
            "T-1", project="alpha", event_type="github_changed",
            source_plane="communication", idempotency_key="github-1",
        )

        result = self.repository.record_runner_terminal(
            "T-1", project="alpha", runner_session_id="runner-1",
            execution_id="execution-1", generation=1, status="completed",
        )

        item = self.repository.get_item("T-1", project="alpha")
        self.assertEqual("none", result["payload"]["handoff_kind"])
        self.assertEqual("ACTIVE", item["state"])
        self.assertEqual("review_merge", item["requested_role"])
        self.assertEqual(yielded["sequence"], item["handled_through"])
        self.assertGreater(item["latest_sequence"], changed["sequence"])


if __name__ == "__main__":
    unittest.main()
