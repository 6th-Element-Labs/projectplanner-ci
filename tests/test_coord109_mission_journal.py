import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from path_setup import ROOT as _ROOT
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


if __name__ == "__main__":
    unittest.main()
