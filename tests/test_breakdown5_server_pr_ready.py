#!/usr/bin/env python3
"""BREAKDOWN 5: all server complete_claim paths require SCM readiness proof."""
from __future__ import annotations

import json
import unittest

from path_setup import ROOT, SRC  # noqa: F401

from switchboard.application.commands import complete_claim
from switchboard.application.contracts.claims import CompleteClaimCommand


def command(evidence):
    return CompleteClaimCommand.from_mapping({
        "claim_id": "taskclaim-coord-75",
        "evidence": evidence,
        "project": "switchboard",
    })


class ServerCompleteClaimPrReady(unittest.TestCase):
    def test_injected_gateway_proves_ready_before_persistence(self):
        gateway_calls = []
        complete_calls = []

        def gateway(evidence, *, project, actor):
            gateway_calls.append((evidence, project, actor))
            return {
                "schema": "switchboard.pr_ready.v1",
                "status": "marked_ready",
                "pr_number": 930,
                "is_draft": False,
                "head_sha": "a" * 40,
                "message": "GitHub confirmed ready",
            }

        def persist(claim_id, **kwargs):
            complete_calls.append((claim_id, kwargs))
            return {"completed": True, "claim_id": claim_id}

        evidence = {
            "pr_number": 930,
            "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/930",
            "branch": "codex/COORD-75",
            "head_sha": "a" * 40,
        }
        result = complete_claim.execute(
            command(evidence),
            actor="agent/codex/coord-75",
            complete=persist,
            ensure_ready=gateway,
        )

        self.assertTrue(result.get("completed"))
        self.assertEqual(
            gateway_calls,
            [(evidence, "switchboard", "agent/codex/coord-75")],
        )
        saved = complete_calls[0][1]["evidence"]
        if isinstance(saved, str):
            saved = json.loads(saved)
        self.assertEqual(saved["pr_ready"]["status"], "marked_ready")
        self.assertFalse(saved["pr_ready"]["is_draft"])

    def test_refuses_agent_invented_head_before_persistence(self):
        result = complete_claim.execute(
            command({
                "pr_number": 930,
                "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/930",
                "head_sha": "a" * 40,
            }),
            actor="agent/codex/coord-111",
            complete=lambda *_args, **_kwargs: self.fail("must not persist"),
            ensure_ready=lambda *_args, **_kwargs: {
                "schema": "switchboard.pr_ready.v1",
                "status": "already_ready",
                "pr_number": 930,
                "is_draft": False,
                "head_sha": "b" * 40,
            },
        )

        self.assertFalse(result.get("completed"))
        self.assertEqual(result.get("error_code"), "completion_pr_head_mismatch")
        self.assertEqual(result.get("submitted_head_sha"), "a" * 40)
        self.assertEqual(result.get("live_head_sha"), "b" * 40)

    def test_refuses_ready_pr_without_authoritative_head(self):
        result = complete_claim.execute(
            command({"pr_number": 930, "head_sha": "a" * 40}),
            actor="agent/codex/coord-111",
            complete=lambda *_args, **_kwargs: self.fail("must not persist"),
            ensure_ready=lambda *_args, **_kwargs: {
                "schema": "switchboard.pr_ready.v1",
                "status": "already_ready",
                "pr_number": 930,
                "is_draft": False,
            },
        )

        self.assertEqual(result.get("error_code"), "completion_pr_head_unproven")

    def test_refuses_still_draft_without_persisting(self):
        def gateway(evidence, *, project, actor):
            return {
                "schema": "switchboard.pr_ready.v1",
                "status": "error",
                "pr_number": 946,
                "is_draft": True,
                "error_code": "completion_pr_mark_ready_failed",
                "failure_class": "provider_rejected",
                "message": "GitHub rejected the mutation",
            }

        result = complete_claim.execute(
            command({"pr_number": 946}),
            actor="agent/codex/adapter-36",
            complete=lambda *_args, **_kwargs: self.fail("must not persist"),
            ensure_ready=gateway,
        )

        self.assertFalse(result.get("completed"))
        self.assertEqual(result.get("error_code"), "completion_pr_mark_ready_failed")
        self.assertEqual(result.get("failure_class"), "provider_rejected")
        self.assertTrue(result["pr_ready"]["is_draft"])

    def test_refuses_unverifiable_pr_without_lying_that_it_is_draft(self):
        def gateway(evidence, *, project, actor):
            return {
                "schema": "switchboard.pr_ready.v1",
                "status": "error",
                "pr_number": 930,
                "is_draft": None,
                "error_code": "completion_pr_read_failed",
                "failure_class": "provider_unavailable",
                "message": "GitHub did not return the completion PR.",
            }

        result = complete_claim.execute(
            command({"pr_number": 930}),
            actor="agent/codex/coord-75",
            complete=lambda *_args, **_kwargs: self.fail("must not persist"),
            ensure_ready=gateway,
        )

        self.assertEqual(result.get("error_code"), "completion_pr_read_failed")
        self.assertEqual(result.get("failure_class"), "provider_unavailable")
        self.assertIsNone(result["pr_ready"]["is_draft"])

    def test_pr_fails_closed_when_gateway_is_not_injected(self):
        result = complete_claim.execute(
            command({"pr_number": 930}),
            actor="tester",
            complete=lambda *_args, **_kwargs: self.fail("must not persist"),
        )
        self.assertEqual(result.get("error_code"), "pr_ready_adapter_unavailable")
        self.assertEqual(result["pr_ready"]["status"], "adapter_unavailable")

    def test_pr_fails_closed_when_gateway_raises(self):
        def gateway(*_args, **_kwargs):
            raise RuntimeError("credential detail must not escape")

        result = complete_claim.execute(
            command({"pr_number": 930}),
            actor="tester",
            complete=lambda *_args, **_kwargs: self.fail("must not persist"),
            ensure_ready=gateway,
        )
        self.assertEqual(result.get("error_code"), "pr_ready_adapter_failed")
        self.assertNotIn("credential detail", result.get("message", ""))
        self.assertIsNone(result["pr_ready"]["is_draft"])

    def test_proof_for_a_different_pr_is_rejected(self):
        result = complete_claim.execute(
            command({"pr_number": 930}),
            actor="tester",
            complete=lambda *_args, **_kwargs: self.fail("must not persist"),
            ensure_ready=lambda *_args, **_kwargs: {
                "schema": "switchboard.pr_ready.v1",
                "pr_number": 931,
                "status": "already_ready",
                "is_draft": False,
            },
        )
        self.assertEqual(result.get("error_code"), "pr_ready_unproven")

    def test_invalid_gateway_result_fails_closed(self):
        result = complete_claim.execute(
            command({"pr_number": 930}),
            actor="tester",
            complete=lambda *_args, **_kwargs: self.fail("must not persist"),
            ensure_ready=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(
            result.get("error_code"),
            "pr_ready_adapter_invalid_result",
        )

    def test_no_pr_offline_evidence_bypasses_scm_gateway(self):
        calls = []

        def persist(claim_id, **kwargs):
            calls.append((claim_id, kwargs))
            return {"completed": True}

        result = complete_claim.execute(
            command({"note": "docs-only offline evidence"}),
            actor="tester",
            complete=persist,
            ensure_ready=lambda *_args, **_kwargs: self.fail("must not call gateway"),
        )
        self.assertTrue(result.get("completed"))
        self.assertEqual(
            calls[0][1]["evidence"],
            {"note": "docs-only offline evidence"},
        )

    def test_application_boundary_has_no_provider_or_process_implementation(self):
        app_source = (
            ROOT / "src/switchboard/application/pr_ready.py"
        ).read_text(encoding="utf-8")
        command_source = (
            ROOT / "src/switchboard/application/commands/complete_claim.py"
        ).read_text(encoding="utf-8")
        combined = app_source + command_source
        self.assertNotIn("subprocess", combined)
        self.assertNotIn("urllib", combined)
        self.assertNotIn("_github_token", combined)
        self.assertNotIn("GH_TOKEN", combined)

    def test_rest_and_mcp_both_inject_the_production_gateway(self):
        rest = (
            ROOT / "src/switchboard/api/routers/claims.py"
        ).read_text(encoding="utf-8")
        mcp = (
            ROOT / "src/switchboard/mcp/tools/claims.py"
        ).read_text(encoding="utf-8")
        monolith_rest = (ROOT / "app_impl.py").read_text(encoding="utf-8")
        monolith_mcp = (ROOT / "mcp_server_impl.py").read_text(encoding="utf-8")
        tasks_service = (
            ROOT / "src/switchboard/services/tasks/app.py"
        ).read_text(encoding="utf-8")
        self.assertIn("ensure_ready=ensure_pr_ready", rest)
        self.assertIn("ensure_ready=services.ensure_pr_ready", mcp)
        self.assertIn("ensure_pr_ready=production_pr_ready_gateway", monolith_rest)
        self.assertIn("ensure_pr_ready=production_pr_ready_gateway", monolith_mcp)
        self.assertIn("ensure_pr_ready=production_pr_ready_gateway", tasks_service)


if __name__ == "__main__":
    unittest.main()
