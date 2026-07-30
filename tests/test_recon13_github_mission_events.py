import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from path_setup import ROOT as _ROOT
from switchboard.application.commands.github_mission_events import (
    append_due_observations,
    project_delivery,
)
from switchboard.storage.migrations.runner import DDL_MIGRATIONS
from switchboard.storage.repositories.mission_journal import MissionJournalRepository


class GithubMissionEventsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "switchboard.db"

        @contextmanager
        def connector(project):
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            for name, sql in DDL_MIGRATIONS:
                if name.startswith(("0123_", "0124_", "0125_", "0126_")):
                    connection.execute(sql)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS task_git_state("
                "task_id TEXT PRIMARY KEY, head_sha TEXT)"
            )
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        self.repository = MissionJournalRepository(connector)
        self.repository.ensure_item("RECON-13", project="switchboard")

    def tearDown(self):
        self.temp.cleanup()

    def _events(self):
        with self.repository._connection("switchboard") as connection:
            return connection.execute(
                "SELECT * FROM mission_events ORDER BY sequence"
            ).fetchall()

    def test_pull_request_material_value_is_deduplicated_across_deliveries(self):
        payload = {
            "action": "ready_for_review",
            "repository": {"full_name": "6th-Element-Labs/projectplanner"},
            "pull_request": {
                "number": 42,
                "title": "RECON-13 projection",
                "body": "",
                "html_url": "https://github.test/pull/42",
                "draft": False,
                "head": {"ref": "agent/RECON-13", "sha": "a" * 40},
                "base": {"ref": "master"},
            },
        }
        first = project_delivery(
            "pull_request", payload, project="switchboard", repository=self.repository
        )
        second = project_delivery(
            "pull_request", payload, project="switchboard", repository=self.repository
        )
        self.assertTrue(first["events"][0]["created"])
        self.assertFalse(second["events"][0]["created"])
        self.assertEqual(1, len(self._events()))

    def test_status_and_verified_callback_shape_share_material_fingerprint(self):
        with self.repository._connection("switchboard") as connection:
            connection.execute(
                "INSERT INTO task_git_state(task_id,head_sha) VALUES (?,?)",
                ("RECON-13", "b" * 40),
            )
        payload = {
            "repository": {"full_name": "6th-Element-Labs/projectplanner"},
            "sha": "b" * 40,
            "context": "Switchboard CI / VM gate",
            "state": "success",
            "target_url": "https://github.test/actions/runs/1",
        }
        first = project_delivery(
            "status", payload, project="switchboard", repository=self.repository
        )
        replay = project_delivery(
            "status", dict(payload), project="switchboard", repository=self.repository
        )
        self.assertTrue(first["events"][0]["created"])
        self.assertFalse(replay["events"][0]["created"])

    def test_non_material_pr_metadata_is_audited_but_not_projected(self):
        result = project_delivery(
            "pull_request",
            {"action": "labeled", "pull_request": {"title": "RECON-13"}},
            project="switchboard", repository=self.repository,
        )
        self.assertEqual("non_material_action", result["reason"])
        self.assertEqual([], self._events())

    def test_observation_due_is_once_per_persisted_wait_timestamp(self):
        self.repository.update_item(
            "RECON-13", project="switchboard", state="WAITING",
            requested_role="implementation", expected_version=1, now=100.0,
        )
        early = append_due_observations(
            project="switchboard", now=399.0, repository=self.repository
        )
        first = append_due_observations(
            project="switchboard", now=400.0, repository=self.repository
        )
        replay = append_due_observations(
            project="switchboard", now=700.0, repository=self.repository
        )
        self.assertEqual([], early["events"])
        self.assertTrue(first["events"][0]["created"])
        self.assertFalse(replay["events"][0]["created"])
        self.assertEqual("observation_due", self._events()[0]["event_type"])


if __name__ == "__main__":
    unittest.main()
