#!/usr/bin/env python3
"""BUG-172: human decisions drive one durable, fenced completion wake."""
from __future__ import annotations

import sqlite3
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from path_setup import ROOT  # noqa: F401

import app_impl
from switchboard.api.routers.attention import create_router
from switchboard.domain.completion.executor import mark_human_resume_receipt
from switchboard.storage.migrations.attention import upgrade_attention_schema
from switchboard.storage.repositories import attention as attention_repo
from switchboard.storage.repositories import autopilot_scopes
from coordinator_daemon import DaemonConfig
from scoped_completion_coordinator import ScopedCompletionCoordinator


HEAD = "a" * 40
PROJECT = "switchboard"
TASK = "COORD-46"
RUN_ID = "completion-run-coord-46"


def _project(raw: str) -> str:
    if raw != PROJECT:
        raise HTTPException(400, "unknown project")
    return raw


def _body_project(body: dict) -> str:
    return _project(str(body.get("project") or ""))


def _principal(_request, project_id, scopes=("read",), dev_actor="test"):
    del scopes
    return {
        "id": f"principal/{project_id}",
        "kind": "user",
        "project": project_id,
        "scopes": ["read", "write:ixp", "admin"],
        "effective_scopes": ["read", "write:ixp", "admin"],
        "display_name": dev_actor,
    }


class CompletionAttentionWakeContract(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            );
            CREATE TABLE task_git_state (
                task_id TEXT PRIMARY KEY,
                head_sha TEXT,
                pr_number INTEGER
            );
            CREATE TABLE completion_runs (
                run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE,
                pr_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                state TEXT NOT NULL,
                route TEXT NOT NULL,
                reason_code TEXT NOT NULL DEFAULT '',
                desired_role TEXT NOT NULL DEFAULT '',
                attempt INTEGER NOT NULL DEFAULT 1,
                state_version INTEGER NOT NULL DEFAULT 1,
                next_retry_at REAL,
                evidence_refs_json TEXT NOT NULL DEFAULT '{}',
                board_status TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                actor TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE autopilot_scopes (
                scope_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                lease_id TEXT,
                holder_agent_id TEXT,
                generation INTEGER,
                fence_epoch INTEGER,
                expires_at REAL,
                scope_type TEXT,
                task_project TEXT,
                task_id TEXT,
                deliverable_id TEXT
            );
            """
        )
        upgrade_attention_schema(self.db)
        self.db.execute(
            "INSERT INTO tasks(task_id,status) VALUES (?,?)",
            (TASK, "Blocked"),
        )
        self.db.execute(
            "INSERT INTO task_git_state(task_id,head_sha,pr_number) VALUES (?,?,?)",
            (TASK, HEAD, 825),
        )
        self.db.execute(
            "INSERT INTO completion_runs("
            "run_id,task_id,pr_number,head_sha,state,route,reason_code,"
            "state_version,board_status,created_at,updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                RUN_ID, TASK, 825, HEAD, "blocked", "human",
                "credentialed_live_proof_unavailable", 3, "Blocked", 1.0, 1.0,
            ),
        )
        self.db.commit()
        self.patches = [
            patch.object(attention_repo, "_conn", return_value=self.db),
            patch.object(
                attention_repo,
                "_write_through",
                side_effect=lambda _project_id, fn: fn(),
            ),
        ]
        for item in self.patches:
            item.start()
        self.wake_calls: list[dict] = []
        self.fail_wake = False

        def start_scope(**kwargs):
            self.wake_calls.append(dict(kwargs))
            if self.fail_wake:
                raise RuntimeError("injected completion-owner outage")
            self.db.execute(
                "INSERT OR REPLACE INTO autopilot_scopes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "scope-coord-46", "active", "lease-coord-46",
                    "owner-coord-46", 1, 1, 9_999_999_999.0,
                    "task", PROJECT, TASK, "completion-control",
                ),
            )
            self.db.commit()
            return {
                "scope_id": "scope-coord-46",
                "status": "active",
                "generation": 1,
                "fence_epoch": 1,
            }

        self.scope_patch = patch.object(
            autopilot_scopes,
            "start_autopilot_scope",
            side_effect=start_scope,
        )
        self.scope_patch.start()

        app = FastAPI()
        app.include_router(create_router(
            resolve_project=_project,
            resolve_principal=_principal,
            resolve_body_project=_body_project,
            list_pending_acks=lambda **_: [],
            list_inbox=lambda *_args, **_kwargs: [],
            on_decision_recorded=app_impl._wake_completion_after_attention,
        ))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.scope_patch.stop()
        for item in self.patches:
            item.stop()
        self.db.close()

    def _completion_request(
        self, suffix: str = "1", *, state_version: int = 3,
    ) -> dict:
        return attention_repo.default_attention_repository.create_request(
            {
                "provider": attention_repo.COMPLETION_PROVIDER,
                "provider_request_id": f"completion-human:{suffix}",
                "schema_version": attention_repo.COMPLETION_CLOSEOUT_SCHEMA,
                "prompt": "Supply the missing authority or keep the task blocked.",
                "choices": [
                    {
                        "id": "resume",
                        "label": "Resume",
                        "effect": "resume_assessment",
                    },
                    {
                        "id": "hold",
                        "label": "Keep blocked",
                        "effect": "remain_blocked",
                    },
                ],
                "recommended_default": {"id": "resume"},
                "idempotency_key": f"completion-closeout:{suffix}",
                "task_id": TASK,
                "host_id": "operator",
                "context": {
                    "schema": attention_repo.COMPLETION_CLOSEOUT_SCHEMA,
                    "task_id": TASK,
                    "deliverable_id": "completion-control",
                    "completion_run_id": RUN_ID,
                    "state_version": state_version,
                    "head_sha": HEAD,
                    "pr_number": 825,
                    "reason_code": "credentialed_live_proof_unavailable",
                },
            },
            actor="completion-owner",
            project=PROJECT,
        )["request"]

    def _decide(self, request: dict, choice: str, idem: str):
        return self.client.post(
            f"/api/attention/requests/{request['request_id']}/decide"
            f"?project={PROJECT}",
            json={
                "expected_version": request["version"],
                "choice": {"id": choice},
                "idempotency_key": idem,
            },
        )

    def test_failure_survives_restart_then_exact_tick_resolves_once(self):
        request = self._completion_request()
        self.fail_wake = True
        first = self._decide(request, "resume", "decision-resume").json()

        self.assertEqual(first["request"]["status"], "decision_recorded")
        self.assertEqual(first["completion_wake"]["status"], "failed")
        self.assertIn("injected completion-owner outage",
                      first["completion_wake"]["last_error"])
        queued = self.client.get(
            f"/api/attention/requests?project={PROJECT}").json()
        self.assertEqual(queued["count"], 1)
        self.assertEqual(
            queued["items"][0]["completion_wake"]["status"], "failed")

        # A fresh daemon process can recover solely from the committed outbox.
        self.fail_wake = False

        def restarted_owner(payload):
            self.wake_calls.append(dict(payload))
            self.db.execute(
                "INSERT OR REPLACE INTO autopilot_scopes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "scope-coord-46", "active", "lease-coord-46",
                    "owner-coord-46", 1, 1, 9_999_999_999.0,
                    "task", PROJECT, TASK, "completion-control",
                ),
            )
            self.db.commit()
            return {"scope_id": "scope-coord-46", "status": "active"}

        drained = attention_repo.drain_completion_wakes(
            wake_completion_owner=restarted_owner,
            actor="completion-daemon/restart",
            project=PROJECT,
            now=time.time() + 10,
        )
        self.assertEqual(drained["accepted"], 1)
        self.assertEqual(
            self.db.execute(
                "SELECT state_version FROM completion_runs WHERE task_id=?",
                (TASK,),
            ).fetchone()["state_version"],
            4,
        )
        in_flight = self.client.get(
            f"/api/attention/requests/{request['request_id']}?project={PROJECT}"
        ).json()
        self.assertEqual(in_flight["status"], "delivering")
        self.assertIsNone(in_flight["delivery_receipt"])

        # Replaying the same POST cannot issue a second external wake.
        call_count = len(self.wake_calls)
        replay = self._decide(request, "resume", "decision-resume")
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(len(self.wake_calls), call_count)

        with self.assertRaises(attention_repo.AttentionStoreError) as forged:
            mark_human_resume_receipt(
                request["request_id"],
                expected_version=3,
                host_id="any-host",
                actor="forger",
                receipt={"schema": "fake", "claimed": True},
                project=PROJECT,
            )
        self.assertEqual(
            forged.exception.code, "attention_completion_owner_required")

        tick = {
            "schema": "switchboard.completion_tick.v1",
            "task_id": TASK,
            "snapshot": {
                "schema": "switchboard.completion_snapshot.v1",
                "task_id": TASK,
                "head_sha": HEAD,
                "pr_number": 825,
            },
            "decision": {
                "schema": "switchboard.completion_decision.v1",
                "route": "review_merge",
            },
            "plan": {
                "schema": "switchboard.completion_effect.v1",
                "task_id": TASK,
                "head_sha": HEAD,
                "pr_number": 825,
                "route": "review_merge",
                "effect": "ensure_review_generation",
                "idem_key": "completion-wake-test",
            },
            "execution": {
                "run": {
                    "run_id": RUN_ID,
                    "state_version": 4,
                    "route": "review_merge",
                    "reason_code": "exact_head_review_required",
                },
                "receipt": {
                    "schema": "switchboard.completion_effect_receipt.v1",
                    "effect": "ensure_review_generation",
                    "idem_key": "completion-wake-test",
                    "verified": True,
                    "pending": False,
                },
            },
        }
        forged_tick = {
            **tick,
            "plan": {**tick["plan"], "idem_key": "forged-effect"},
        }
        rejected = attention_repo.complete_completion_wake_for_tick(
            TASK,
            tick=forged_tick,
            scope_authority={
                "schema": "switchboard.autopilot_scope_authority.v1",
                "scope_id": "scope-coord-46",
                "lease_id": "lease-coord-46",
                "holder_agent_id": "owner-coord-46",
                "generation": 1,
                "fence_epoch": 1,
            },
            actor="forger",
            project=PROJECT,
        )
        self.assertEqual(rejected["status"], "blocked")
        self.assertEqual(
            self.client.get(
                f"/api/attention/requests/{request['request_id']}?project={PROJECT}"
            ).json()["status"],
            "delivering",
        )
        pending_tick = {
            **tick,
            "execution": {
                **tick["execution"],
                "receipt": {
                    **tick["execution"]["receipt"],
                    "verified": False,
                    "pending": True,
                },
            },
        }
        pending_rejected = attention_repo.complete_completion_wake_for_tick(
            TASK,
            tick=pending_tick,
            scope_authority={
                "schema": "switchboard.autopilot_scope_authority.v1",
                "scope_id": "scope-coord-46",
                "lease_id": "lease-coord-46",
                "holder_agent_id": "owner-coord-46",
                "generation": 1,
                "fence_epoch": 1,
            },
            actor="completion-daemon/restart",
            project=PROJECT,
        )
        self.assertEqual(pending_rejected["status"], "blocked")
        self.assertEqual(
            self.client.get(
                f"/api/attention/requests/{request['request_id']}?project={PROJECT}"
            ).json()["status"],
            "delivering",
        )
        resolved = attention_repo.complete_completion_wake_for_tick(
            TASK,
            tick=tick,
            scope_authority={
                "schema": "switchboard.autopilot_scope_authority.v1",
                "scope_id": "scope-coord-46",
                "lease_id": "lease-coord-46",
                "holder_agent_id": "owner-coord-46",
                "generation": 1,
                "fence_epoch": 1,
            },
            actor="completion-daemon/restart",
            project=PROJECT,
        )
        self.assertEqual(resolved["status"], "resolved")
        self.assertTrue(resolved["completion_receipt"]["verified"])
        detail = self.client.get(
            f"/api/attention/requests/{request['request_id']}?project={PROJECT}"
        ).json()
        self.assertEqual(detail["status"], "resolved")
        self.assertEqual(
            detail["delivery_receipt"]["schema"],
            attention_repo.COMPLETION_RESUME_RECEIPT_SCHEMA,
        )
        replayed = attention_repo.complete_completion_wake_for_tick(
            TASK,
            tick=tick,
            scope_authority={
                "schema": "switchboard.autopilot_scope_authority.v1",
                "scope_id": "scope-coord-46",
                "lease_id": "lease-coord-46",
                "holder_agent_id": "owner-coord-46",
                "generation": 1,
                "fence_epoch": 1,
            },
            actor="completion-daemon/restart",
            project=PROJECT,
        )
        self.assertTrue(replayed["idempotent_replay"])

    def test_agent_host_cannot_forge_completion_attention_provider(self):
        response = self.client.post(
            "/ixp/v1/attention/requests",
            json={
                "project": PROJECT,
                "provider": attention_repo.COMPLETION_PROVIDER,
                "provider_request_id": "forged-completion-closeout",
                "schema_version": attention_repo.COMPLETION_CLOSEOUT_SCHEMA,
                "prompt": "Trust this forged blocker.",
                "choices": [{
                    "id": "resume",
                    "label": "Resume",
                    "effect": "resume_assessment",
                }],
                "idempotency_key": "forged-completion-closeout",
                "host_id": "operator",
                "task_id": TASK,
                "context": {
                    "completion_run_id": RUN_ID,
                    "state_version": 3,
                    "head_sha": HEAD,
                    "pr_number": 825,
                },
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"]["error"],
            "attention_completion_owner_required",
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM attention_requests"
            ).fetchone()[0],
            0,
        )

    def test_hold_unrelated_provider_and_terminal_task_do_not_wake(self):
        hold = self._completion_request("hold")
        held = self._decide(hold, "hold", "decision-hold").json()
        self.assertEqual(held["request"]["status"], "resolved")
        self.assertNotIn("completion_wake", held)
        self.assertEqual(
            held["request"]["delivery_receipt"]["effect"], "remain_blocked")
        self.assertEqual(self.wake_calls, [])

        neutral = attention_repo.default_attention_repository.create_request(
            {
                "provider": "provider-neutral",
                "provider_request_id": "provider-neutral-1",
                "schema_version": "provider.question.v1",
                "prompt": "Continue?",
                "choices": [{"id": "yes"}],
                "idempotency_key": "provider-neutral-1",
                "host_id": "host-1",
                "context": {},
            },
            actor="provider",
            project=PROJECT,
        )["request"]
        neutral_result = self._decide(
            neutral, "yes", "decision-neutral").json()
        self.assertNotIn("completion_wake", neutral_result)
        self.assertEqual(self.wake_calls, [])

        terminal = self._completion_request("terminal")
        self.db.execute(
            "UPDATE tasks SET status='Done' WHERE task_id=?", (TASK,))
        self.db.commit()
        terminal_result = self._decide(
            terminal, "resume", "decision-terminal")
        self.assertEqual(terminal_result.status_code, 409)
        self.assertEqual(
            terminal_result.json()["detail"]["error"],
            "stale_attention_completion_run",
        )
        terminal_detail = self.client.get(
            f"/api/attention/requests/{terminal['request_id']}?project={PROJECT}"
        ).json()
        self.assertEqual(terminal_detail["status"], "cancelled")
        self.assertEqual(self.wake_calls, [])

        self.db.execute(
            "UPDATE tasks SET status='Blocked' WHERE task_id=?", (TASK,))
        self.db.execute(
            "UPDATE task_git_state SET pr_number=825 WHERE task_id=?", (TASK,))
        same_head_replacement = self._completion_request("replacement-pr")
        self.db.execute(
            "UPDATE task_git_state SET pr_number=826 WHERE task_id=?", (TASK,))
        self.db.commit()
        stale_pr = self._decide(
            same_head_replacement, "resume", "decision-replacement-pr")
        self.assertEqual(stale_pr.status_code, 409)
        self.assertEqual(
            stale_pr.json()["detail"]["error"], "stale_attention_pr")
        stale_detail = self.client.get(
            f"/api/attention/requests/{same_head_replacement['request_id']}"
            f"?project={PROJECT}"
        ).json()
        self.assertEqual(stale_detail["status"], "cancelled")
        self.assertEqual(self.wake_calls, [])

    def test_hold_rejects_a_same_head_stale_completion_run(self):
        request = self._completion_request("stale-hold")
        self.db.execute(
            "UPDATE completion_runs SET state_version=4, route='review_merge' "
            "WHERE task_id=?",
            (TASK,),
        )
        self.db.commit()

        response = self._decide(
            request, "hold", "decision-stale-hold")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["error"],
            "stale_attention_completion_run",
        )
        detail = self.client.get(
            f"/api/attention/requests/{request['request_id']}?project={PROJECT}"
        ).json()
        self.assertEqual(detail["status"], "cancelled")
        self.assertIsNone(detail["delivery_receipt"])
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM attention_decisions WHERE request_id=?",
                (request["request_id"],),
            ).fetchone()[0],
            0,
        )

    def test_accepted_wake_is_cancelled_when_head_or_task_becomes_stale(self):
        request = self._completion_request("accepted-head-race")
        decided = attention_repo.default_attention_repository.record_decision(
            request["request_id"],
            {
                "expected_version": request["version"],
                "choice": {"id": "resume"},
                "idempotency_key": "decision-accepted-head-race",
            },
            actor="operator",
            actor_principal_id="principal-1",
            project=PROJECT,
        )
        self.db.execute(
            "INSERT OR REPLACE INTO autopilot_scopes VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "scope-coord-46", "active", "lease-coord-46",
                "owner-coord-46", 1, 1, 9_999_999_999.0,
                "task", PROJECT, TASK, "completion-control",
            ),
        )
        self.db.commit()
        accepted = attention_repo.attempt_completion_wake(
            decided["completion_wake"]["request_id"],
            wake_completion_owner=lambda _payload: {
                "scope_id": "scope-coord-46",
            },
            actor="completion-owner",
            project=PROJECT,
        )
        self.assertEqual(accepted["status"], "accepted")

        self.db.execute(
            "UPDATE task_git_state SET head_sha=? WHERE task_id=?",
            ("b" * 40, TASK),
        )
        self.db.commit()
        stale = attention_repo.attempt_completion_wake(
            request["request_id"],
            wake_completion_owner=lambda _payload: self.fail(
                "stale accepted wake must not be reissued"),
            actor="completion-daemon",
            project=PROJECT,
            now=time.time() + 61,
        )
        self.assertEqual(stale["status"], "cancelled")
        self.assertEqual(
            stale["last_error"], "completion_attention_head_changed")
        self.assertEqual(stale["request"]["status"], "cancelled")

        self.db.execute(
            "UPDATE task_git_state SET head_sha=? WHERE task_id=?",
            (HEAD, TASK),
        )
        self.db.execute(
            "UPDATE completion_runs SET state_version=4, route='human', "
            "head_sha=? WHERE task_id=?",
            (HEAD, TASK),
        )
        self.db.execute(
            "UPDATE tasks SET status='Blocked' WHERE task_id=?",
            (TASK,),
        )
        self.db.commit()
        terminal_request = self._completion_request(
            "accepted-terminal-race", state_version=4)
        terminal_decision = (
            attention_repo.default_attention_repository.record_decision(
                terminal_request["request_id"],
                {
                    "expected_version": terminal_request["version"],
                    "choice": {"id": "resume"},
                    "idempotency_key": "decision-accepted-terminal-race",
                },
                actor="operator",
                actor_principal_id="principal-1",
                project=PROJECT,
            )
        )
        terminal_accepted = attention_repo.attempt_completion_wake(
            terminal_decision["completion_wake"]["request_id"],
            wake_completion_owner=lambda _payload: {
                "scope_id": "scope-coord-46",
            },
            actor="completion-owner",
            project=PROJECT,
        )
        self.assertEqual(terminal_accepted["status"], "accepted")
        self.db.execute(
            "UPDATE tasks SET status='Done' WHERE task_id=?", (TASK,))
        self.db.commit()
        terminal = attention_repo.attempt_completion_wake(
            terminal_request["request_id"],
            wake_completion_owner=lambda _payload: self.fail(
                "terminal accepted wake must not be reissued"),
            actor="completion-daemon",
            project=PROJECT,
            now=time.time() + 61,
        )
        self.assertEqual(terminal["status"], "cancelled")
        self.assertEqual(
            terminal["last_error"], "completion_attention_task_terminal")
        self.assertEqual(terminal["request"]["status"], "cancelled")

    def test_unleased_scope_stays_retryable_until_fenced_owner_acquires_it(self):
        request = self._completion_request("unleased-scope")
        decided = attention_repo.default_attention_repository.record_decision(
            request["request_id"],
            {
                "expected_version": request["version"],
                "choice": {"id": "resume"},
                "idempotency_key": "decision-unleased-scope",
            },
            actor="operator",
            actor_principal_id="principal-1",
            project=PROJECT,
        )

        def schedule_only(_payload):
            self.db.execute(
                "INSERT OR REPLACE INTO autopilot_scopes VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "scope-unleased", "active", "", "", 1, 0, None,
                    "task", PROJECT, TASK, "completion-control",
                ),
            )
            return {"scope_id": "scope-unleased", "status": "active"}

        scheduled = attention_repo.attempt_completion_wake(
            decided["completion_wake"]["request_id"],
            wake_completion_owner=schedule_only,
            actor="attention-api",
            project=PROJECT,
        )
        self.assertEqual(scheduled["status"], "failed")
        self.assertEqual(
            scheduled["last_error"], "completion_wake_scope_binding_invalid")
        self.assertEqual(
            self.db.execute(
                "SELECT state_version FROM completion_runs WHERE task_id=?",
                (TASK,),
            ).fetchone()[0],
            3,
        )

        self.db.execute(
            "UPDATE autopilot_scopes SET lease_id='lease-unleased', "
            "holder_agent_id='owner-unleased', generation=2, fence_epoch=1, "
            "expires_at=? WHERE scope_id='scope-unleased'",
            (time.time() + 600,),
        )
        self.db.commit()
        fenced = attention_repo.attempt_completion_wake(
            request["request_id"],
            wake_completion_owner=lambda _payload: {
                "scope_id": "scope-unleased",
            },
            actor="completion-daemon",
            project=PROJECT,
            now=time.time() + 6,
        )
        self.assertEqual(fenced["status"], "accepted")
        self.assertEqual(fenced["wake_receipt"]["resume_state_version"], 4)

    def test_scope_loss_rewakes_without_double_advancing_then_receipts(self):
        request = self._completion_request("scope-loss")
        decided = attention_repo.default_attention_repository.record_decision(
            request["request_id"],
            {
                "expected_version": request["version"],
                "choice": {"id": "resume"},
                "idempotency_key": "decision-scope-loss",
            },
            actor="operator",
            actor_principal_id="principal-1",
            project=PROJECT,
        )
        self.db.execute(
            "INSERT OR REPLACE INTO autopilot_scopes VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "scope-coord-46", "active", "lease-coord-46",
                "owner-coord-46", 1, 1, 9_999_999_999.0,
                "task", PROJECT, TASK, "completion-control",
            ),
        )
        self.db.commit()
        first = attention_repo.attempt_completion_wake(
            decided["completion_wake"]["request_id"],
            wake_completion_owner=lambda _payload: {
                "scope_id": "scope-coord-46",
            },
            actor="completion-owner",
            project=PROJECT,
        )
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(first["wake_receipt"]["resume_state_version"], 4)
        self.db.execute(
            "DELETE FROM autopilot_scopes WHERE scope_id='scope-coord-46'")
        self.db.commit()

        def replacement_scope(_payload):
            self.db.execute(
                "INSERT INTO autopilot_scopes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "scope-replacement", "active", "lease-replacement",
                    "owner-replacement", 2, 3, 9_999_999_999.0,
                    "task", PROJECT, TASK, "completion-control",
                ),
            )
            return {"scope_id": "scope-replacement"}

        replacement = attention_repo.attempt_completion_wake(
            request["request_id"],
            wake_completion_owner=replacement_scope,
            actor="completion-daemon/restart",
            project=PROJECT,
            now=time.time() + 61,
        )
        self.assertEqual(replacement["status"], "accepted")
        self.assertEqual(replacement["wake_id"], first["wake_id"])
        self.assertEqual(
            replacement["wake_receipt"]["resume_state_version"], 4)
        self.assertEqual(
            self.db.execute(
                "SELECT state_version FROM completion_runs WHERE task_id=?",
                (TASK,),
            ).fetchone()[0],
            4,
        )

        self.db.execute(
            "UPDATE completion_runs SET state_version=5, route='review_merge', "
            "reason_code='exact_head_review_required' WHERE task_id=?",
            (TASK,),
        )
        self.db.commit()
        tick = {
            "schema": "switchboard.completion_tick.v1",
            "task_id": TASK,
            "snapshot": {
                "schema": "switchboard.completion_snapshot.v1",
                "task_id": TASK,
                "head_sha": HEAD,
                "pr_number": 825,
            },
            "decision": {
                "schema": "switchboard.completion_decision.v1",
                "route": "review_merge",
            },
            "plan": {
                "schema": "switchboard.completion_effect.v1",
                "task_id": TASK,
                "head_sha": HEAD,
                "pr_number": 825,
                "route": "review_merge",
                "effect": "ensure_review_generation",
                "idem_key": "scope-loss-reassessment",
            },
            "execution": {
                "run": {
                    "run_id": RUN_ID,
                    "state_version": 5,
                    "route": "review_merge",
                    "reason_code": "exact_head_review_required",
                },
                "receipt": {
                    "schema": "switchboard.completion_effect_receipt.v1",
                    "effect": "ensure_review_generation",
                    "idem_key": "scope-loss-reassessment",
                    "verified": True,
                    "pending": False,
                },
            },
        }
        resolved = attention_repo.complete_completion_wake_for_tick(
            TASK,
            tick=tick,
            scope_authority={
                "schema": "switchboard.autopilot_scope_authority.v1",
                "scope_id": "scope-replacement",
                "lease_id": "lease-replacement",
                "holder_agent_id": "owner-replacement",
                "generation": 2,
                "fence_epoch": 3,
            },
            actor="completion-daemon/restart",
            project=PROJECT,
        )
        self.assertEqual(resolved["status"], "resolved")
        self.assertTrue(resolved["completion_receipt"]["verified"])

    def test_new_pending_wake_is_not_starved_by_an_accepted_wake(self):
        old_request = self._completion_request("old-accepted")
        old_decision = attention_repo.default_attention_repository.record_decision(
            old_request["request_id"],
            {
                "expected_version": old_request["version"],
                "choice": {"id": "resume"},
                "idempotency_key": "decision-old-accepted",
            },
            actor="operator",
            actor_principal_id="principal-1",
            project=PROJECT,
        )

        def existing_scope(_payload):
            self.db.execute(
                "INSERT OR REPLACE INTO autopilot_scopes VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "scope-coord-46", "active", "lease-coord-46",
                    "owner-coord-46", 1, 1, 9_999_999_999.0,
                    "task", PROJECT, TASK, "completion-control",
                ),
            )
            return {"scope_id": "scope-coord-46"}

        old_wake = attention_repo.attempt_completion_wake(
            old_decision["completion_wake"]["request_id"],
            wake_completion_owner=existing_scope,
            actor="completion-owner",
            project=PROJECT,
        )
        self.assertEqual(old_wake["status"], "accepted")

        new_request = self._completion_request(
            "new-pending", state_version=4)
        new_decision = attention_repo.default_attention_repository.record_decision(
            new_request["request_id"],
            {
                "expected_version": new_request["version"],
                "choice": {"id": "resume"},
                "idempotency_key": "decision-new-pending",
            },
            actor="operator",
            actor_principal_id="principal-1",
            project=PROJECT,
        )
        new_wake_id = new_decision["completion_wake"]["wake_id"]
        drained = attention_repo.drain_completion_wakes(
            wake_completion_owner=lambda _payload: {
                "scope_id": "scope-coord-46",
            },
            actor="completion-daemon",
            project=PROJECT,
            limit=1,
            now=time.time() + 1,
        )
        self.assertEqual(drained["checked"], 1)
        self.assertEqual(drained["accepted"], 1)
        self.assertEqual(drained["results"][0]["wake_id"], new_wake_id)
        self.assertNotEqual(new_wake_id, old_wake["wake_id"])


class ScopedOwnerWiringContract(unittest.TestCase):
    def test_daemon_drain_rearms_the_exact_task_scope(self):
        class Store:
            def __init__(self):
                self.started = []
                self.acquired = []

            def start_autopilot_scope(self, **kwargs):
                self.started.append(kwargs)
                return {"scope_id": "scope-1", "status": "active"}

            def acquire_autopilot_scope_lease(self, scope_id, **kwargs):
                self.acquired.append((scope_id, kwargs))
                return {
                    "schema": "switchboard.autopilot_scope_authority.v1",
                    "scope_id": scope_id,
                    "lease_id": "lease-1",
                    "holder_agent_id": kwargs["holder_agent_id"],
                    "generation": 1,
                    "fence_epoch": 1,
                }

            def drain_completion_wakes(self, **kwargs):
                receipt = kwargs["wake_completion_owner"]({
                    "task_id": TASK,
                    "deliverable_id": "completion-control",
                })
                return {
                    "schema": "switchboard.completion_wake_drain.v1",
                    "checked": 1,
                    "accepted": 1,
                    "failed": 0,
                    "cancelled": 0,
                    "results": [receipt],
                }

        store = Store()
        owner = ScopedCompletionCoordinator(
            DaemonConfig(projects=(PROJECT,), act=True),
            store_mod=store,
            agent_id="codex/BUG-172",
        )
        drained = owner._drain_completion_wakes(PROJECT)
        self.assertEqual(drained["accepted"], 1)
        self.assertEqual(store.started[0]["task_id"], TASK)
        self.assertEqual(
            store.started[0]["deliverable_id"], "completion-control")
        self.assertEqual(store.acquired[0][0], "scope-1")
        self.assertEqual(
            store.acquired[0][1]["holder_agent_id"], "codex/BUG-172")

    def test_standalone_owner_records_the_exact_tick_receipt(self):
        tick = {"schema": "switchboard.completion_tick.v1", "task_id": TASK}
        authority = {
            "schema": "switchboard.autopilot_scope_authority.v1",
            "scope_id": "scope-1",
            "lease_id": "lease-1",
            "holder_agent_id": "codex/BUG-172",
            "generation": 4,
            "fence_epoch": 7,
        }

        class Store:
            def __init__(self):
                self.completed = []
                self.scope_updates = []

            @staticmethod
            def get_task(task_id, *, project):
                return {
                    "task_id": task_id,
                    "status": "Blocked",
                    "git_state": {"pr_number": 825, "head_sha": HEAD},
                    "provenance": {"terminal": False},
                }

            def complete_completion_wake_for_tick(self, task_id, **kwargs):
                self.completed.append((task_id, kwargs))
                return {"status": "resolved", "wake_id": "wake-1"}

            def update_autopilot_scope(self, scope_id, **kwargs):
                self.scope_updates.append((scope_id, kwargs))
                return {"scope_id": scope_id}

        store = Store()
        owner = ScopedCompletionCoordinator(
            DaemonConfig(projects=(PROJECT,), act=True),
            store_mod=store,
            agent_id="codex/BUG-172",
        )
        from switchboard.application import completion_driver
        from switchboard.storage.repositories import completion_runs

        with (
            patch.object(
                completion_runs,
                "get_active_completion_run",
                return_value={"run_id": RUN_ID, "route": "human"},
            ),
            patch.object(
                completion_driver,
                "run_completion_tick",
                return_value=tick,
            ),
        ):
            result = owner._run_standalone_task_scope(
                PROJECT,
                {
                    "scope_id": "scope-1",
                    "scope_type": "task",
                    "task_project": PROJECT,
                    "task_id": TASK,
                },
                authority,
            )

        self.assertEqual(result["completion_wake"]["status"], "resolved")
        self.assertEqual(store.completed[0][0], TASK)
        self.assertIs(store.completed[0][1]["tick"], tick)
        self.assertEqual(
            store.completed[0][1]["scope_authority"], authority)


if __name__ == "__main__":
    unittest.main()
