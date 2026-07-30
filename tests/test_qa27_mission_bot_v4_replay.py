#!/usr/bin/env python3
"""QA-27: replay Mission Bot v4 incidents and the GitHub/CI state matrix.

The harness deliberately feeds raw provider facts to the durable journal and
the production scoped worker.  Assertions are about coordination invariants,
not about a factory interpretation of GitHub state.
"""
from __future__ import annotations

import inspect
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
from switchboard.application.mission_bot_v4 import (
    ScopedMissionWorkerPorts,
    tick_scoped_mission,
)
from switchboard.application.mission_bot_v4 import worker as worker_module
from switchboard.storage.migrations.runner import DDL_MIGRATIONS
from switchboard.storage.repositories.mission_journal import (
    MissionJournalError,
    MissionJournalRepository,
)


PROJECT = "switchboard"
TASK = "QA-27"
OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40

MATRIX = {
    "pull_request_state": ("OPEN", "CLOSED", "MERGED"),
    "mergeable": ("MERGEABLE", "CONFLICTING", "UNKNOWN"),
    "merge_state_status": (
        "DIRTY", "UNKNOWN", "BLOCKED", "BEHIND", "UNSTABLE", "HAS_HOOKS",
    ),
    "review_decision": ("APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED", None),
    "commit_status": ("EXPECTED", "PENDING", "SUCCESS", "FAILURE", "ERROR"),
    "check_run_status": (
        "REQUESTED", "QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "COMPLETED",
    ),
    "check_run_conclusion": (
        "SUCCESS", "FAILURE", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE",
        "ACTION_REQUIRED", "STALE", "NEUTRAL", "SKIPPED",
    ),
    "queue_state": (
        "UNARMED", "ARMED", "QUEUED", "AWAITING_CHECKS", "MERGEABLE",
        "UNMERGEABLE", "LOCKED", "EJECTED",
    ),
    "ci_transport": (
        "DISPATCH_MISSING", "DISPATCHED", "RUNNER_LOST", "CALLBACK_MISSING",
        "CALLBACK_PENDING", "CALLBACK_SUCCESS", "PROVIDER_UNAVAILABLE",
    ),
    "deployment_state": (
        "QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "SUCCESS", "FAILURE",
        "ERROR", "INACTIVE",
    ),
}

INCIDENTS = (
    {
        "name": "green_reviewed_unarmed",
        "head_sha": NEW_HEAD,
        "merge_state_status": "CLEAN",
        "required_status": "SUCCESS",
        "review": "APPROVED",
        "auto_merge": None,
        "queue": None,
    },
    {
        "name": "blocked_aggregate_queue_admission_remaining",
        "head_sha": NEW_HEAD,
        "merge_state_status": "BLOCKED",
        "required_status": "SUCCESS",
        "review": "APPROVED",
        "auto_merge": None,
        "queue": None,
    },
    {
        "name": "merge_group_ejected",
        "head_sha": OLD_HEAD,
        "merge_group_sha": "c" * 40,
        "queue": "EJECTED",
        "removal_reason": "FAILED_CHECKS",
    },
    {
        "name": "merge_group_rebuilt",
        "head_sha": NEW_HEAD,
        "merge_group_sha": "d" * 40,
        "queue": "AWAITING_CHECKS",
        "old_group_evidence_current": False,
    },
    {
        "name": "bug_239_null_task_exact_sha_ci",
        "head_sha": NEW_HEAD,
        "external_ci_task_id": None,
        "external_ci_source_sha": NEW_HEAD,
        "external_ci_conclusion": "SUCCESS",
    },
    {
        "name": "bug_240_sibling_session_exact_head_preflight",
        "head_sha": NEW_HEAD,
        "preflight_session": "implementation",
        "current_session": "review_merge",
        "preflight_matches_exact_head": True,
    },
    {
        "name": "provider_outage_context_incomplete",
        "head_sha": NEW_HEAD,
        "context_complete": False,
        "provider_error": "rate_limited",
    },
    {
        "name": "workflow_success_callback_missing",
        "head_sha": NEW_HEAD,
        "workflow": "SUCCESS",
        "required_callback": "MISSING",
    },
)


class MissionBotV4ReplayHarness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "qa27.db"

        @contextmanager
        def connector(_project):
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

        self.connector = connector
        self.journal = MissionJournalRepository(connector)
        self.journal.ensure_item(
            TASK, project=PROJECT, state="ACTIVE", requested_role="review_merge",
            now=100.0,
        )
        with self.journal._connection(PROJECT) as connection:
            connection.execute(
                "INSERT INTO task_git_state(task_id,head_sha) VALUES (?,?)",
                (TASK, NEW_HEAD),
            )
        self.starts: list[dict] = []
        self.runner_live = False
        self.scope_allowed = True
        self.authority = {
            "schema": "switchboard.autopilot_scope_authority.v1",
            "scope_id": "scope-qa27",
            "lease_id": "lease-qa27",
            "holder_agent_id": "qa27-coordinator",
            "generation": 7,
            "fence_epoch": 3,
        }

    def tearDown(self):
        self.temp.cleanup()

    def ports(self, *, start_receipt=None):
        def start(task_id, **kwargs):
            self.starts.append({"task_id": task_id, **kwargs})
            return dict(start_receipt or {"action": "started", "started": True})

        return ScopedMissionWorkerPorts(
            validate_scope=lambda *_args, **_kwargs: {
                "allowed": self.scope_allowed,
                **({} if self.scope_allowed else {"error": "scope_authority_denied"}),
            },
            get_task=lambda *_args, **_kwargs: {
                "task_id": TASK,
                "status": "Blocked",
                "dependency_state": {"satisfied": True},
                "git_state": {"head_sha": NEW_HEAD},
            },
            has_live_execution=lambda *_args, **_kwargs: self.runner_live,
            start_task=start,
            journal=self.journal,
        )

    def tick(self, *, ports=None):
        return tick_scoped_mission(
            TASK,
            project=PROJECT,
            scope_authority=self.authority,
            actor="qa27-coordinator",
            ports=ports or self.ports(),
        )

    def append_raw(self, key, payload, *, head=NEW_HEAD, event_type="github_changed"):
        return self.journal.append_event(
            TASK,
            project=PROJECT,
            event_type=event_type,
            source_plane="communication",
            idempotency_key=key,
            head_sha=head,
            payload=payload,
        )

    def assert_pages_persisted_role_once(self, key, payload, *, head=NEW_HEAD):
        event = self.append_raw(key, payload, head=head)
        before = self.journal.get_item(TASK, project=PROJECT)
        result = self.tick()
        replay = self.tick()
        after = self.journal.get_item(TASK, project=PROJECT)
        self.assertTrue(event["created"])
        self.assertEqual("start_task", result["action"])
        self.assertEqual("review_merge", result["requested_role"])
        self.assertEqual("review_merge", self.starts[-1]["role"])
        self.assertEqual(event["sequence"], result["event_pointer"])
        self.assertEqual(head, self.starts[-1]["source_sha"])
        self.assertEqual("wait", replay["action"])
        self.assertEqual("no_unhandled_event", replay["reason"])
        self.assertEqual("ACTIVE", after["state"])
        self.assertEqual("review_merge", after["requested_role"])
        self.assertEqual(before["handled_through"] + 1, after["handled_through"])
        self.assertNotIn("human", result)
        self.assertNotIn("diagnosis", result)

    def test_complete_documented_github_ci_matrix_only_pages_the_llm(self):
        for family, values in MATRIX.items():
            for value in values:
                with self.subTest(family=family, value=value):
                    self.assert_pages_persisted_role_once(
                        f"matrix:{family}:{value}",
                        {"family": family, "value": value},
                    )

    def test_recent_incident_corpus_remains_raw_and_exact_head_fenced(self):
        for incident in INCIDENTS:
            with self.subTest(incident=incident["name"]):
                self.assert_pages_persisted_role_once(
                    f"incident:{incident['name']}",
                    incident,
                    head=incident["head_sha"],
                )

        starts_by_name = {
            incident["name"]: start
            for incident, start in zip(INCIDENTS, self.starts[-len(INCIDENTS):])
        }
        self.assertEqual(
            OLD_HEAD, starts_by_name["merge_group_ejected"]["source_sha"],
        )
        self.assertEqual(
            NEW_HEAD, starts_by_name["merge_group_rebuilt"]["source_sha"],
        )
        self.assertNotEqual(
            starts_by_name["merge_group_ejected"]["source_sha"],
            starts_by_name["merge_group_rebuilt"]["source_sha"],
        )

    def test_material_webhooks_dedupe_without_state_or_role_classification(self):
        pr = {
            "action": "ready_for_review",
            "repository": {"full_name": "6th-Element-Labs/projectplanner"},
            "pull_request": {
                "number": 1200,
                "title": f"{TASK} replay",
                "body": "",
                "html_url": "https://github.test/pull/1200",
                "draft": False,
                "head": {"ref": f"agent/{TASK}", "sha": NEW_HEAD},
                "base": {"ref": "master"},
            },
        }
        first = project_delivery(
            "pull_request", pr, project=PROJECT, repository=self.journal,
        )
        duplicate = project_delivery(
            "pull_request", pr, project=PROJECT, repository=self.journal,
        )
        self.assertTrue(first["events"][0]["created"])
        self.assertFalse(duplicate["events"][0]["created"])
        item = self.journal.get_item(TASK, project=PROJECT)
        self.assertEqual("ACTIVE", item["state"])
        self.assertEqual("review_merge", item["requested_role"])
        self.assertEqual("start_task", self.tick()["action"])
        self.assertEqual(1, len(self.starts))

    def test_runner_callback_and_provider_loss_have_bounded_recovery(self):
        self.append_raw(
            "runner-ended:impl",
            {"terminal": "lost", "execution_id": "impl-1"},
            event_type="runner_ended",
        )
        self.runner_live = True
        self.assertEqual("runner_live", self.tick()["reason"])
        self.assertEqual([], self.starts)
        self.runner_live = False
        recovered = self.tick()
        self.assertEqual("start_task", recovered["action"])
        self.assertEqual("review_merge", recovered["requested_role"])

        item = self.journal.get_item(TASK, project=PROJECT)
        self.journal.update_item(
            TASK,
            project=PROJECT,
            state="WAITING",
            requested_role="review_merge",
            expected_version=item["version"],
            handled_through=item["handled_through"],
            now=200.0,
        )
        early = append_due_observations(
            project=PROJECT, now=499.0, repository=self.journal,
        )
        due = append_due_observations(
            project=PROJECT, now=500.0, repository=self.journal,
        )
        repeated = append_due_observations(
            project=PROJECT, now=800.0, repository=self.journal,
        )
        self.assertEqual([], early["events"])
        self.assertTrue(due["events"][0]["created"])
        self.assertFalse(repeated["events"][0]["created"])
        self.assertEqual("start_task", self.tick()["action"])

    def test_failed_admission_stale_cursor_and_scope_takeover_fail_closed(self):
        self.append_raw("provider-outage", {"context_complete": False})
        failed_ports = self.ports(start_receipt={"error": "provider unavailable"})
        first = self.tick(ports=failed_ports)
        second = self.tick(ports=failed_ports)
        self.assertEqual("start_not_admitted", first["reason"])
        self.assertEqual("start_not_admitted", second["reason"])
        self.assertEqual(
            self.starts[-1]["mission_key"], self.starts[-2]["mission_key"],
        )
        self.assertEqual(
            0, self.journal.get_item(TASK, project=PROJECT)["handled_through"],
        )

        item = self.journal.get_item(TASK, project=PROJECT)
        self.journal.update_item(
            TASK,
            project=PROJECT,
            state="ACTIVE",
            requested_role="review_merge",
            expected_version=item["version"],
            handled_through=item["handled_through"],
        )
        with self.assertRaisesRegex(MissionJournalError, "current version"):
            self.journal.update_item(
                TASK,
                project=PROJECT,
                state="HUMAN",
                requested_role="review_merge",
                expected_version=item["version"],
            )

        self.scope_allowed = False
        denied = self.tick()
        self.assertEqual("scope_authority_denied", denied["reason"])
        self.assertEqual("wait", denied["action"])

    def test_restart_recovers_item_role_cursor_history_and_no_duplicate_boot(self):
        self.assert_pages_persisted_role_once(
            "restart:green-unarmed",
            INCIDENTS[0],
        )
        before = self.journal.get_item(TASK, project=PROJECT)
        events_before = self.journal.list_events(TASK, project=PROJECT, limit=200)
        restarted = MissionJournalRepository(self.connector)
        self.journal = restarted
        after = restarted.get_item(TASK, project=PROJECT)
        events_after = restarted.list_events(TASK, project=PROJECT, limit=200)
        self.assertEqual(before["state"], after["state"])
        self.assertEqual(before["requested_role"], after["requested_role"])
        self.assertEqual(before["handled_through"], after["handled_through"])
        self.assertEqual(events_before, events_after)
        self.assertEqual("no_unhandled_event", self.tick()["reason"])
        self.assertEqual(1, len(self.starts))

    def test_only_persisted_terminal_provenance_can_make_done(self):
        item = self.journal.get_item(TASK, project=PROJECT)
        with self.assertRaisesRegex(MissionJournalError, "DONE requires"):
            self.journal.update_item(
                TASK,
                project=PROJECT,
                state="DONE",
                requested_role="review_merge",
                expected_version=item["version"],
                terminal_kind="github_merge",
                terminal_ref=NEW_HEAD,
                authority="mission_bot",
            )
        done = self.journal.update_item(
            TASK,
            project=PROJECT,
            state="DONE",
            requested_role="review_merge",
            expected_version=item["version"],
            terminal_kind="github_merge",
            terminal_ref=NEW_HEAD,
            authority="canonical_provenance_projector",
        )
        self.assertEqual("DONE", done["state"])
        self.assertEqual("terminal_provenance", self.tick()["reason"])
        self.assertEqual([], self.starts)

    def test_v4_worker_has_no_legacy_controller_dependency(self):
        source = inspect.getsource(worker_module)
        for retired in (
            "domain.mission_bot import",
            "completion.routing",
            "completion.effects",
            "decision_episode",
            "build_dossier",
            "agent_requires_human",
        ):
            self.assertNotIn(retired, source)


if __name__ == "__main__":
    unittest.main()
