#!/usr/bin/env python3
"""ADAPTER-30: worker completion contract marks the PR ready before complete_claim.

Regression for AUTOPILOT BREAKDOWN 5: workers opened draft PRs and nothing in the
shared exit path called `gh pr ready`, so green CI still failed merge authorization.
"""
from __future__ import annotations

import importlib.util
import json
import unittest
from unittest.mock import patch

from path_setup import ROOT


def _load_core():
    path = ROOT / "adapters" / "switchboard_core.py"
    spec = importlib.util.spec_from_file_location("switchboard_core_adapter30", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


sb = _load_core()


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class EnsurePrReady(unittest.TestCase):
    def test_marks_draft_ready(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[:3] == ["gh", "pr", "view"]:
                return FakeProc(0, json.dumps({"isDraft": True, "number": 863}))
            if cmd[:3] == ["gh", "pr", "ready"]:
                return FakeProc(0, "✓ Pull request 6th-Element-Labs/projectplanner#863 is marked as ready")
            raise AssertionError(f"unexpected command: {cmd}")

        with patch.object(sb.subprocess, "run", side_effect=fake_run):
            result = sb.ensure_pr_ready({
                "pr_number": 863,
                "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/863",
                "branch": "codex/CO-20",
                "head_sha": "a" * 40,
            })

        self.assertEqual(result["status"], "marked_ready")
        self.assertEqual(result["pr_number"], 863)
        self.assertFalse(result["is_draft"])
        self.assertTrue(any(c[:3] == ["gh", "pr", "ready"] for c in calls))

    def test_already_ready_is_noop(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[:3] == ["gh", "pr", "view"]:
                return FakeProc(0, json.dumps({"isDraft": False, "number": 864}))
            raise AssertionError(f"ready must not run when already non-draft: {cmd}")

        with patch.object(sb.subprocess, "run", side_effect=fake_run):
            result = sb.ensure_pr_ready({
                "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/864",
            })

        self.assertEqual(result["status"], "already_ready")
        self.assertFalse(result["is_draft"])
        self.assertFalse(any(c[:3] == ["gh", "pr", "ready"] for c in calls))

    def test_skips_when_no_pr(self):
        with patch.object(sb.subprocess, "run") as run:
            result = sb.ensure_pr_ready({"branch": "codex/DOCS-1", "head_sha": "b" * 40})
        self.assertEqual(result["status"], "skipped_no_pr")
        run.assert_not_called()


class CompleteClaimMarksReady(unittest.TestCase):
    def test_complete_claim_marks_ready_before_http(self):
        order = []

        def fake_ensure(evidence, **kwargs):
            order.append("ensure_pr_ready")
            return {
                "schema": "switchboard.pr_ready.v1",
                "status": "marked_ready",
                "pr_number": 867,
                "is_draft": False,
            }

        def fake_http(method, path, body=None, base=None, token=None):
            order.append("http")
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/txp/v1/complete_claim")
            evidence = json.loads(body["evidence"])
            self.assertEqual(evidence["pr_ready"]["status"], "marked_ready")
            self.assertFalse(evidence["pr_ready"]["is_draft"])
            return {"completed": True, "status": "In Review"}

        with patch.object(sb, "ensure_pr_ready", side_effect=fake_ensure), \
                patch.object(sb, "_http", side_effect=fake_http), \
                patch.object(sb, "_personal_execution_enabled", return_value=False):
            out = sb.complete_claim(
                "switchboard",
                "taskclaim-remediation",
                {
                    "pr_number": 867,
                    "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/867",
                    "branch": "codex/BUG-178",
                    "head_sha": "c" * 40,
                    "role": "remediation",
                },
            )

        self.assertTrue(out["completed"])
        self.assertEqual(order, ["ensure_pr_ready", "http"])

    def test_complete_claim_refuses_when_still_draft(self):
        def fake_ensure(evidence, **kwargs):
            return {
                "schema": "switchboard.pr_ready.v1",
                "status": "error",
                "pr_number": 865,
                "is_draft": True,
                "message": "gh pr ready failed",
            }

        with patch.object(sb, "ensure_pr_ready", side_effect=fake_ensure), \
                patch.object(sb, "_http") as http, \
                patch.object(sb, "_personal_execution_enabled", return_value=False):
            out = sb.complete_claim(
                "switchboard",
                "taskclaim-adapter27",
                {"pr_number": 865, "branch": "codex/ADAPTER-27", "head_sha": "d" * 40},
            )

        http.assert_not_called()
        self.assertEqual(out.get("error"), "pr_still_draft")
        self.assertTrue(out.get("pr_ready", {}).get("is_draft"))


if __name__ == "__main__":
    unittest.main()
