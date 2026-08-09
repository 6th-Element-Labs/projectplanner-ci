import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT as _ROOT  # noqa: F401
from switchboard.application.commands.github_mission_events import (
    GithubMissionProjectionError,
    append_due_observations,
    project_delivery,
)
from switchboard.storage.migrations.runner import DDL_MIGRATIONS
from switchboard.storage.repositories.mission_journal import MissionJournalRepository

import webhook_inbox


MISSION_MIGRATIONS = {
    "0123_mission_items",
    "0124_mission_events",
    "0125_ix_mission_events_task_sequence",
    "0126_ix_mission_events_task_head",
}


class GithubMissionEventsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "switchboard.db"
        connection = sqlite3.connect(self.path)
        try:
            for name, sql in DDL_MIGRATIONS:
                if name in MISSION_MIGRATIONS:
                    connection.execute(sql)
            connection.execute(
                "CREATE TABLE task_git_state("
                "task_id TEXT PRIMARY KEY, head_sha TEXT, pr_number INTEGER)"
            )
            connection.commit()
        finally:
            connection.close()

        @contextmanager
        def connector(_project):
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

        self.repository = MissionJournalRepository(
            connector, lambda _project, operation: operation(),
        )

    def tearDown(self):
        self.temp.cleanup()

    def _events(self):
        with self.repository._connection("switchboard") as connection:
            return connection.execute(
                "SELECT * FROM mission_events ORDER BY sequence"
            ).fetchall()

    @staticmethod
    def _pull_request_payload(task_id="RECON-14"):
        return {
            "action": "ready_for_review",
            "repository": {"full_name": "6th-Element-Labs/projectplanner"},
            "pull_request": {
                "number": 42,
                "title": f"{task_id} projection",
                "body": "",
                "html_url": "https://github.test/pull/42",
                "draft": False,
                "head": {"ref": f"codex/{task_id}", "sha": "a" * 40},
                "base": {"ref": "master"},
            },
        }

    def test_missing_mission_is_inert_and_does_not_create_one(self):
        result = project_delivery(
            "pull_request",
            self._pull_request_payload(),
            project="switchboard",
            repository=self.repository,
        )
        self.assertEqual("github_mission_events_inert", result["action"])
        self.assertEqual("mission_not_found", result["reason"])
        self.assertEqual(["RECON-14"], result["missing_mission_task_ids"])
        self.assertEqual([], result["events"])
        self.assertIsNone(self.repository.get_item("RECON-14", project="switchboard"))
        self.assertEqual([], self._events())

    def test_pull_request_material_value_is_deduplicated_without_state_change(self):
        before = self.repository.ensure_item("RECON-14", project="switchboard")
        first = project_delivery(
            "pull_request",
            self._pull_request_payload(),
            project="switchboard",
            repository=self.repository,
        )
        replay = project_delivery(
            "pull_request",
            self._pull_request_payload(),
            project="switchboard",
            repository=self.repository,
        )
        after = self.repository.get_item("RECON-14", project="switchboard")
        self.assertTrue(first["events"][0]["created"])
        self.assertFalse(replay["events"][0]["created"])
        self.assertEqual(2, len(self._events()))
        self.assertEqual(before["state"], after["state"])
        self.assertEqual(before["requested_role"], after["requested_role"])
        self.assertEqual(before["version"], after["version"])
        payload = json.loads(self._events()[1]["payload_json"])
        self.assertEqual("communication", self._events()[1]["source_plane"])
        self.assertEqual("pull_request", payload["object_type"])
        self.assertNotIn("body", payload)

    def test_pull_request_body_mentions_do_not_claim_other_tasks(self):
        for task_id in ("IDENTITY-2", "IDENTITY-3", "IDENTITY-4"):
            self.repository.ensure_item(task_id, project="switchboard")
        payload = self._pull_request_payload("IDENTITY-2")
        payload["pull_request"]["body"] = (
            "Runtime defaults stay with IDENTITY-3; overlay work stays with IDENTITY-4."
        )

        result = project_delivery(
            "pull_request", payload, project="switchboard", repository=self.repository,
        )

        self.assertEqual(["IDENTITY-2"], result["mapped_task_ids"])
        self.assertEqual(
            ["IDENTITY-2"],
            [event["task_id"] for event in result["events"]],
        )

    def test_issue_comment_body_mentions_use_existing_pr_owner_only(self):
        for task_id in ("IDENTITY-2", "IDENTITY-3", "IDENTITY-4"):
            self.repository.ensure_item(task_id, project="switchboard")
        with self.repository._connection("switchboard") as connection:
            connection.execute(
                "INSERT INTO task_git_state(task_id,head_sha,pr_number) VALUES (?,?,?)",
                ("IDENTITY-2", "a" * 40, 157),
            )

        result = project_delivery(
            "issue_comment",
            {
                "action": "created",
                "repository": {"full_name": "6th-Element-Labs/ActionEngine"},
                "issue": {
                    "number": 157,
                    "title": "IDENTITY-2 customer-neutral identity",
                    "body": (
                        "Runtime defaults stay with IDENTITY-3; "
                        "overlay work stays with IDENTITY-4."
                    ),
                    "pull_request": {"url": "https://github.test/pulls/157"},
                },
                "comment": {
                    "id": 99,
                    "body": "Automated review started.",
                    "html_url": "https://github.test/pull/157#issuecomment-99",
                },
            },
            project="switchboard",
            repository=self.repository,
        )

        self.assertEqual(["IDENTITY-2"], result["mapped_task_ids"])
        self.assertEqual(
            ["IDENTITY-2"],
            [event["task_id"] for event in result["events"]],
        )

    def test_invalid_material_identity_fails_early_with_typed_reason(self):
        with self.assertRaises(GithubMissionProjectionError) as raised:
            project_delivery(
                "status",
                {
                    "repository": {"full_name": "6th-Element-Labs/projectplanner"},
                    "context": "Switchboard CI / VM gate",
                    "state": "success",
                },
                project="switchboard",
                repository=self.repository,
            )
        self.assertEqual("github_material_identity_missing", raised.exception.code)
        self.assertIn("head_sha", str(raised.exception))

    def test_completed_check_without_conclusion_fails_early(self):
        with self.assertRaises(GithubMissionProjectionError) as raised:
            project_delivery(
                "check_run",
                {
                    "action": "completed",
                    "repository": {"full_name": "6th-Element-Labs/projectplanner"},
                    "check_run": {
                        "id": 7,
                        "name": "Switchboard CI / VM gate",
                        "status": "completed",
                        "head_sha": "c" * 40,
                    },
                },
                project="switchboard",
                repository=self.repository,
            )
        self.assertIn("check_run.conclusion", str(raised.exception))

    def test_status_and_verified_callback_shape_share_material_fingerprint(self):
        self.repository.ensure_item("RECON-14", project="switchboard")
        with self.repository._connection("switchboard") as connection:
            connection.execute(
                "INSERT INTO task_git_state(task_id,head_sha) VALUES (?,?)",
                ("RECON-14", "b" * 40),
            )
        payload = {
            "repository": {"full_name": "6th-Element-Labs/projectplanner"},
            "sha": "b" * 40,
            "context": "Switchboard CI / VM gate",
            "state": "success",
            "target_url": "https://github.test/actions/runs/1",
        }
        first = project_delivery(
            "status", payload, project="switchboard", repository=self.repository,
        )
        replay = project_delivery(
            "status", dict(payload), project="switchboard", repository=self.repository,
        )
        self.assertTrue(first["events"][0]["created"])
        self.assertFalse(replay["events"][0]["created"])

    def test_merge_group_queue_ref_maps_back_to_existing_pr_mission(self):
        self.repository.ensure_item("RECON-14", project="switchboard")
        with self.repository._connection("switchboard") as connection:
            connection.execute(
                "INSERT INTO task_git_state(task_id,head_sha,pr_number) VALUES (?,?,?)",
                ("RECON-14", "e" * 40, 42),
            )
        result = project_delivery(
            "merge_group",
            {
                "action": "checks_requested",
                "repository": {"full_name": "6th-Element-Labs/projectplanner"},
                "merge_group": {
                    "head_sha": "f" * 40,
                    "base_sha": "0" * 40,
                    "head_ref": (
                        "refs/heads/gh-readonly-queue/master/pr-42-deadbeef"
                    ),
                },
            },
            project="switchboard",
            repository=self.repository,
        )
        self.assertEqual(["RECON-14"], result["mapped_task_ids"])
        self.assertTrue(result["events"][0]["created"])
        event = self._events()[-1]
        self.assertEqual("f" * 40, event["head_sha"])
        self.assertEqual("merge_group", json.loads(event["payload_json"])["object_type"])

    def test_merge_group_status_maps_through_its_synthetic_head(self):
        self.repository.ensure_item("RECON-14", project="switchboard")
        with self.repository._connection("switchboard") as connection:
            connection.execute(
                "INSERT INTO task_git_state(task_id,head_sha,pr_number) VALUES (?,?,?)",
                ("RECON-14", "e" * 40, 42),
            )
        project_delivery(
            "merge_group",
            {
                "action": "checks_requested",
                "repository": {"full_name": "6th-Element-Labs/projectplanner"},
                "merge_group": {
                    "head_sha": "f" * 40,
                    "base_sha": "0" * 40,
                    "head_ref": "refs/heads/gh-readonly-queue/master/pr-42-deadbeef",
                },
            },
            project="switchboard",
            repository=self.repository,
        )
        result = project_delivery(
            "status",
            {
                "repository": {"full_name": "6th-Element-Labs/projectplanner"},
                "sha": "f" * 40,
                "context": "Switchboard CI / VM gate",
                "state": "failure",
                "target_url": "https://github.test/actions/runs/30775085751",
            },
            project="switchboard",
            repository=self.repository,
        )
        self.assertEqual(["RECON-14"], result["mapped_task_ids"])
        event = self._events()[-1]
        payload = json.loads(event["payload_json"])
        self.assertEqual("status", payload["object_type"])
        self.assertEqual("failure", payload["status_state"])
        self.assertEqual(
            "https://github.test/actions/runs/30775085751",
            payload["target_url"],
        )

    def test_review_uses_durable_pr_mapping_when_text_has_no_task_id(self):
        self.repository.ensure_item("RECON-14", project="switchboard")
        with self.repository._connection("switchboard") as connection:
            connection.execute(
                "INSERT INTO task_git_state(task_id,head_sha,pr_number) VALUES (?,?,?)",
                ("RECON-14", "1" * 40, 42),
            )
        result = project_delivery(
            "pull_request_review",
            {
                "action": "submitted",
                "repository": {"full_name": "6th-Element-Labs/projectplanner"},
                "pull_request": {
                    "number": 42,
                    "title": "Title no longer contains a task ID",
                    "body": "",
                    "head": {"ref": "feature", "sha": "1" * 40},
                },
                "review": {"id": 99, "state": "approved"},
            },
            project="switchboard",
            repository=self.repository,
        )
        self.assertEqual(["RECON-14"], result["mapped_task_ids"])
        self.assertTrue(result["events"][0]["created"])

    def test_review_and_repository_policy_events_remain_facts_only(self):
        before = self.repository.ensure_item("RECON-14", project="switchboard")
        review = project_delivery(
            "pull_request_review",
            {
                "action": "submitted",
                "repository": {"full_name": "6th-Element-Labs/projectplanner"},
                "pull_request": {
                    "number": 42,
                    "title": "RECON-14 review",
                    "head": {"ref": "codex/RECON-14", "sha": "1" * 40},
                },
                "review": {"id": 99, "state": "approved"},
            },
            project="switchboard",
            repository=self.repository,
        )
        policy = project_delivery(
            "repository_ruleset",
            {
                "action": "edited",
                "repository": {"full_name": "6th-Element-Labs/projectplanner"},
                "repository_ruleset": {"id": 7},
                "changes": {"enforcement": {"from": "disabled"}},
            },
            project="switchboard",
            repository=self.repository,
        )
        after = self.repository.get_item("RECON-14", project="switchboard")
        self.assertTrue(review["events"][0]["created"])
        self.assertTrue(policy["events"][0]["created"])
        self.assertEqual(before["state"], after["state"])
        self.assertEqual(before["requested_role"], after["requested_role"])
        self.assertEqual(before["version"], after["version"])

    def test_non_material_metadata_is_not_projected(self):
        self.repository.ensure_item("RECON-14", project="switchboard")
        result = project_delivery(
            "pull_request",
            {"action": "labeled", "pull_request": {"title": "RECON-14"}},
            project="switchboard",
            repository=self.repository,
        )
        self.assertEqual("non_material_action", result["reason"])
        self.assertEqual(1, len(self._events()))

    def test_observation_due_is_once_per_persisted_wait_timestamp(self):
        self.repository.ensure_item("RECON-14", project="switchboard", now=10.0)
        self.repository.update_item(
            "RECON-14",
            project="switchboard",
            state="WAITING",
            requested_role="implementation",
            expected_version=1,
            now=100.0,
        )
        early = append_due_observations(
            project="switchboard", now=399.0, repository=self.repository,
        )
        first = append_due_observations(
            project="switchboard", now=400.0, repository=self.repository,
        )
        replay = append_due_observations(
            project="switchboard", now=700.0, repository=self.repository,
        )
        self.assertEqual([], early["events"])
        self.assertTrue(first["events"][0]["created"])
        self.assertEqual([], replay["events"])
        event = self._events()[-1]
        self.assertEqual("observation_due", event["event_type"])
        self.assertEqual(
            {"due_at": 400.0, "wait_started_at": 100.0},
            json.loads(event["payload_json"]),
        )

    def test_later_material_event_suppresses_observation_backstop(self):
        self.repository.ensure_item("RECON-14", project="switchboard", now=10.0)
        self.repository.update_item(
            "RECON-14",
            project="switchboard",
            state="WAITING",
            requested_role="implementation",
            expected_version=1,
            now=100.0,
        )
        self.repository.append_event(
            "RECON-14",
            project="switchboard",
            event_type="github_changed",
            source_plane="communication",
            idempotency_key="later-material-event",
            occurred_at=200.0,
            head_sha="d" * 40,
        )
        result = append_due_observations(
            project="switchboard", now=400.0, repository=self.repository,
        )
        self.assertEqual([], result["events"])

    def test_webhook_keeps_v1_result_and_adds_only_projection_receipt(self):
        row = {
            "event": "pull_request",
            "payload": json.dumps(self._pull_request_payload()),
        }
        with (
            patch.object(
                webhook_inbox.github_sync,
                "handle_pr",
                return_value={"action": "v1_pr_applied", "status": "In Review"},
            ) as v1_handler,
            patch.object(
                webhook_inbox.github_mission_events,
                "project_delivery",
                return_value={
                    "action": "github_mission_events_projected",
                    "events": [{"task_id": "RECON-14", "created": True}],
                },
            ) as projection,
        ):
            result = webhook_inbox._apply_row(row, "switchboard")
        v1_handler.assert_called_once()
        projection.assert_called_once()
        self.assertEqual("v1_pr_applied", result["action"])
        self.assertIn("mission_events", result)

    def test_projection_only_webhook_is_recorded_as_applied_not_ignored(self):
        row = {
            "event": "pull_request_review",
            "payload": json.dumps({"action": "submitted"}),
        }
        with patch.object(
            webhook_inbox.github_mission_events,
            "project_delivery",
            return_value={
                "action": "github_mission_events_projected",
                "events": [{"task_id": "RECON-14", "created": True}],
            },
        ):
            result = webhook_inbox._apply_row(row, "switchboard")
        self.assertEqual("github_mission_events_projected", result["action"])
        self.assertEqual("ignored", result["base_action"])

    def test_invalid_projection_fails_delivery_for_durable_retry(self):
        row = {
            "event": "pull_request",
            "payload": json.dumps(self._pull_request_payload()),
        }
        with (
            patch.object(
                webhook_inbox.github_sync,
                "handle_pr",
                return_value={"action": "v1_pr_applied"},
            ) as v1_handler,
            patch.object(
                webhook_inbox.github_mission_events,
                "project_delivery",
                side_effect=GithubMissionProjectionError(
                    "github_material_identity_missing", "head_sha is missing",
                ),
            ),
        ):
            with self.assertRaises(GithubMissionProjectionError):
                webhook_inbox._apply_row(row, "switchboard")
        v1_handler.assert_called_once()


if __name__ == "__main__":
    unittest.main()
