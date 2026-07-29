#!/usr/bin/env python3
"""Regression: ``provenance._github_pr`` must not discard the failure cause.

During the 2026-07-30 Mission Bot incident the helper collapsed every HTTP
failure — 403 rate limit, timeout, auth — into ``return None``, indistinguishable
from a genuinely missing PR. Downstream that surfaced as "pull request node_id
unavailable" (97 blind retries on QA-19) and contributed to the #1086 rollback
misdiagnosis. Only a definitive HTTP 404 may map to None; every other failure
must raise ``GitHubPRFetchError`` carrying the exception class and HTTP status,
and the caller boundaries must record that cause in their error payloads so
ledger ``last_error`` fields hold the real reason.
"""
from __future__ import annotations

import io
import unittest
import urllib.error
import urllib.request
from unittest import mock

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands import merge_gate as merge_gate_command
from switchboard.application.commands import reconcile_task_merge
from switchboard.storage.repositories import provenance

REPO = "6th-Element-Labs/projectplanner"
PR_URL = f"https://github.com/{REPO}/pull/1086"


def _http_error(code: int, msg: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"https://api.github.com/repos/{REPO}/pulls/1086",
        code, msg, None, io.BytesIO(b""))


class GithubPrFetchRaisesOnUnknownFailure(unittest.TestCase):
    def test_http_404_still_means_missing_pr(self):
        with mock.patch.object(
                urllib.request, "urlopen",
                side_effect=_http_error(404, "Not Found")):
            self.assertIsNone(provenance._github_pr(REPO, 1086))

    def test_http_403_raises_with_status_and_class(self):
        with mock.patch.object(
                urllib.request, "urlopen",
                side_effect=_http_error(403, "rate limit exceeded")):
            with self.assertRaises(provenance.GitHubPRFetchError) as ctx:
                provenance._github_pr(REPO, 1086)
        message = str(ctx.exception)
        self.assertIn("403", message)
        self.assertIn("HTTPError", message)
        self.assertEqual(ctx.exception.status, 403)

    def test_timeout_raises_with_exception_class(self):
        with mock.patch.object(
                urllib.request, "urlopen",
                side_effect=TimeoutError("timed out")):
            with self.assertRaises(provenance.GitHubPRFetchError) as ctx:
                provenance._github_pr(REPO, 1086)
        self.assertIn("TimeoutError", str(ctx.exception))


class CallerBoundariesRecordTheCause(unittest.TestCase):
    def test_reconcile_task_merge_detail_carries_cause(self):
        def fetch(_repo, _number):
            raise provenance.GitHubPRFetchError(
                "HTTPError 403: rate limit exceeded", status=403)

        result = reconcile_task_merge.execute(
            "QA-19", project="switchboard", actor="test",
            load_subject=lambda *_a: (
                {"task_id": "QA-19", "status": "In Review"},
                {"pr_number": 1086, "pr_url": PR_URL}),
            canonical_repo_for=lambda _p: REPO,
            fetch_pull_request=fetch,
            mark_merged=lambda **_k: {},
        )
        self.assertEqual(result.get("error"), "pr_state_unavailable")
        self.assertIn("403", str(result.get("detail")))

    def test_merge_gate_evidence_source_carries_cause(self):
        with mock.patch.object(
                merge_gate_command, "_github_pr",
                side_effect=provenance.GitHubPRFetchError(
                    "HTTPError 403: rate limit exceeded", status=403)):
            pr, source = merge_gate_command._merge_gate_pr_evidence(
                PR_URL, 1086, {}, REPO)
        self.assertEqual(pr, {})
        self.assertEqual(source.get("reason"), "unavailable")
        self.assertIn("403", str(source.get("error")))

    def test_fetch_github_prs_survives_and_records_errors(self):
        import store

        def raising(_repo, _number, token=""):
            raise provenance.GitHubPRFetchError(
                "URLError: timed out", status=None)

        original = store._github_pr
        store._github_pr = raising
        try:
            fetched, checks = provenance._fetch_github_prs(
                [(REPO, 1086)], token="")
        finally:
            store._github_pr = original
        self.assertIsNone(fetched.get((REPO, 1086)))
        errors = checks.get("github_pr_fetch_errors") or {}
        self.assertIn("URLError", str(errors.get(f"{REPO}#1086")))


if __name__ == "__main__":
    unittest.main()
