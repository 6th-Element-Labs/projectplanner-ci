#!/usr/bin/env python3
"""BREAKDOWN 5: MCP/server complete_claim must not accept a draft PR.

ADAPTER-30 closed the local-adapter exit path. Codex workers call
``taikun_plan.complete_claim`` directly and bypass that adapter, so draft PRs
still reached In Review (COORD-56 / #930). The application command must mark
ready or fail closed before persistence.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from path_setup import ROOT, SRC  # noqa: F401

from switchboard.application.commands import complete_claim
from switchboard.application.contracts.claims import CompleteClaimCommand


class ServerCompleteClaimPrReady(unittest.TestCase):
    def test_marks_draft_ready_before_persistence(self):
        complete_calls = []

        def fake_complete(claim_id, **kwargs):
            complete_calls.append((claim_id, kwargs))
            return {"completed": True, "claim_id": claim_id}

        def fake_ensure(evidence, **kwargs):
            return {
                "schema": "switchboard.pr_ready.v1",
                "status": "marked_ready",
                "pr_number": 930,
                "is_draft": False,
                "message": "PR marked ready",
            }

        with patch.object(complete_claim, "ensure_pr_ready", side_effect=fake_ensure):
            result = complete_claim.execute(
                CompleteClaimCommand.from_mapping({
                    "claim_id": "taskclaim-coord-56",
                    "evidence": {
                        "pr_number": 930,
                        "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/930",
                        "branch": "codex/COORD-56",
                        "head_sha": "a" * 40,
                    },
                    "project": "switchboard",
                }),
                actor="agent/codex/coord-56",
                complete=fake_complete,
            )

        self.assertTrue(result.get("completed"))
        self.assertEqual(len(complete_calls), 1)
        evidence = complete_calls[0][1]["evidence"]
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        self.assertEqual(evidence["pr_ready"]["status"], "marked_ready")
        self.assertFalse(evidence["pr_ready"]["is_draft"])

    def test_refuses_when_still_draft(self):
        def fake_complete(*args, **kwargs):
            raise AssertionError("persistence must not run for draft PRs")

        def fake_ensure(evidence, **kwargs):
            return {
                "schema": "switchboard.pr_ready.v1",
                "status": "error",
                "pr_number": 946,
                "is_draft": True,
                "message": "gh pr ready failed",
            }

        with patch.object(complete_claim, "ensure_pr_ready", side_effect=fake_ensure):
            result = complete_claim.execute(
                CompleteClaimCommand.from_mapping({
                    "claim_id": "taskclaim-adapter-36",
                    "evidence": {
                        "pr_number": 946,
                        "branch": "codex/ADAPTER-36",
                        "head_sha": "b" * 40,
                    },
                    "project": "switchboard",
                }),
                actor="agent/codex/adapter-36",
                complete=fake_complete,
            )

        self.assertEqual(result.get("error"), "pr_still_draft")
        self.assertEqual(result.get("failure_class"), "failed_gate")
        self.assertTrue(result.get("pr_ready", {}).get("is_draft"))

    def test_skips_when_no_pr_evidence(self):
        complete_calls = []

        def fake_complete(claim_id, **kwargs):
            complete_calls.append((claim_id, kwargs))
            return {"completed": True, "claim_id": claim_id}

        with patch.object(
            complete_claim,
            "ensure_pr_ready",
            return_value={
                "schema": "switchboard.pr_ready.v1",
                "status": "skipped_no_pr",
                "pr_number": None,
                "is_draft": None,
                "message": "",
            },
        ) as ensure:
            result = complete_claim.execute(
                CompleteClaimCommand.from_mapping({
                    "claim_id": "taskclaim-docs",
                    "evidence": {"note": "docs-only offline evidence"},
                    "project": "switchboard",
                }),
                actor="tester",
                complete=fake_complete,
            )

        ensure.assert_called_once()
        self.assertTrue(result.get("completed"))
        self.assertEqual(len(complete_calls), 1)
        self.assertEqual(
            complete_calls[0][1]["evidence"],
            {"note": "docs-only offline evidence"},
        )


if __name__ == "__main__":
    unittest.main()
