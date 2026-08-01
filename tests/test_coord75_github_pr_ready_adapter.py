#!/usr/bin/env python3
"""COORD-75: GitHub PR-ready integration is project-bound and re-read proven."""
from __future__ import annotations

import unittest

from path_setup import SRC  # noqa: F401

from switchboard.integrations.github_pull_requests import (
    GitHubPullRequestReadyAdapter,
)


class GitHubPrReadyAdapterTest(unittest.TestCase):
    def adapter(self, responses, *, repository="6th-Element-Labs/projectplanner"):
        calls = []

        def transport(method, url, *, token, payload):
            calls.append((method, url, token, payload))
            return responses.pop(0)

        adapter = GitHubPullRequestReadyAdapter(
            repository_for_project=lambda project: repository,
            token_for_project=lambda project: f"token-for-{project}",
            transport=transport,
        )
        return adapter, calls

    def test_already_ready_is_read_only(self):
        adapter, calls = self.adapter([(200, {
            "draft": False,
            "node_id": "PR_x",
            "html_url": "https://github.com/6th-Element-Labs/projectplanner/pull/930",
            "head": {"sha": "a" * 40, "ref": "codex/COORD-111"},
        })])
        result = adapter(
            {"pr_number": 930},
            project="switchboard",
            actor="agent/codex",
        )
        self.assertEqual(result["status"], "already_ready")
        self.assertFalse(result["is_draft"])
        self.assertEqual(result["head_sha"], "a" * 40)
        self.assertEqual(result["head_ref"], "codex/COORD-111")
        self.assertEqual(len(calls), 1)
        self.assertIn("/6th-Element-Labs/projectplanner/pulls/930", calls[0][1])

    def test_draft_is_mutated_then_reread(self):
        adapter, calls = self.adapter([
            (200, {"draft": True, "node_id": "PR_node"}),
            (200, {"data": {"markPullRequestReadyForReview": {}}}),
            (200, {
                "draft": False,
                "node_id": "PR_node",
                "html_url": "https://github.com/6th-Element-Labs/projectplanner/pull/930",
                "head": {"sha": "b" * 40, "ref": "codex/COORD-111"},
            }),
        ])
        result = adapter(
            {"pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/930"},
            project="switchboard",
            actor="agent/codex",
        )
        self.assertEqual(result["status"], "marked_ready")
        self.assertFalse(result["is_draft"])
        self.assertEqual(result["head_sha"], "b" * 40)
        self.assertEqual([call[0] for call in calls], ["GET", "POST", "GET"])
        self.assertEqual(
            calls[1][3]["variables"]["pullRequestId"],
            "PR_node",
        )

    def test_mutation_without_ready_reread_fails_closed(self):
        adapter, _ = self.adapter([
            (200, {"draft": True, "node_id": "PR_node"}),
            (200, {"data": {"markPullRequestReadyForReview": {}}}),
            (200, {"draft": True, "node_id": "PR_node"}),
        ])
        result = adapter(
            {"pr_number": 930},
            project="switchboard",
            actor="agent/codex",
        )
        self.assertEqual(result["error_code"], "completion_pr_ready_unproven")
        self.assertTrue(result["is_draft"])

    def test_repository_mismatch_is_rejected_before_github(self):
        adapter, calls = self.adapter([])
        result = adapter(
            {
                "pr_number": 4,
                "repo": "other-org/other-repo",
            },
            project="switchboard",
            actor="agent/codex",
        )
        self.assertEqual(
            result["error_code"],
            "completion_pr_repository_mismatch",
        )
        self.assertEqual(result["failure_class"], "unbound_identity")
        self.assertEqual(calls, [])

    def test_missing_project_repository_is_explicit(self):
        adapter, calls = self.adapter([], repository="")
        result = adapter(
            {"pr_number": 930},
            project="switchboard",
            actor="agent/codex",
        )
        self.assertEqual(result["error_code"], "canonical_repository_unavailable")
        self.assertEqual(calls, [])

    def test_missing_credential_is_explicit(self):
        adapter = GitHubPullRequestReadyAdapter(
            repository_for_project=lambda project: "org/repo",
            token_for_project=lambda project: "",
            transport=lambda *_args, **_kwargs: self.fail("must not call GitHub"),
        )
        result = adapter(
            {"pr_number": 1},
            project="switchboard",
            actor="agent/codex",
        )
        self.assertEqual(result["error_code"], "scm_credential_unavailable")


if __name__ == "__main__":
    unittest.main()
