#!/usr/bin/env python3
"""BUG-173: merge-queue must not lose Switchboard / merge authorization.

Acceptance pinned here:
1. hydrate current exact-head contexts before classification (even with supplied evidence)
2. never overwrite a valid authorization from an empty/stale payload
3. durably publish gate results with readback + retry
4. authorize the temporary merge-group SHA automatically
5. failure injection: delayed, duplicate, stale, credential-failure
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands import merge_gate as merge_gate_command  # noqa: E402
import github_sync  # noqa: E402


HEAD = "79e1f4e137b5e999987ec6640c9ffb535410c570"
MERGE_GROUP_SHA = "956f04197f0fb9933b40076a4e90896178125470"
REPO = "6th-Element-Labs/projectplanner"
MERGE_CONTEXT = "Switchboard / merge authorization"


def _load_pr_gate():
    spec = importlib.util.spec_from_file_location(
        "switchboard_pr_gate_bug173",
        ROOT / "scripts" / "switchboard_pr_gate.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class _GitHubResponse:
    def __init__(self, payload):
        import json
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


class SuppliedEvidenceHydration(unittest.TestCase):
    """Queue-time callers always pass github_pr without status_contexts."""

    def test_supplied_pull_rest_payload_hydrates_exact_head_statuses(self):
        supplied = {
            "number": 849,
            "head": {"sha": HEAD, "ref": "codex/BUG-172"},
            "base": {"ref": "master"},
            # Pulls REST omits contexts — this is the BUG-173 failure mode.
        }
        statuses = {
            "statuses": [
                {"context": "Switchboard CI / VM gate", "state": "success"},
                {"context": "Switchboard UI / Playwright", "state": "success"},
            ],
        }
        with mock.patch.object(
            merge_gate_command.urllib.request,
            "urlopen",
            return_value=_GitHubResponse(statuses),
        ):
            hydrated, source = merge_gate_command._merge_gate_pr_evidence(
                "",
                849,
                {"github_pr": supplied},
                REPO,
            )

        self.assertEqual(source.get("source"), "supplied_evidence")
        self.assertTrue(source.get("hydrated_status_contexts"))
        contexts = {
            row["context"]: row["state"] for row in hydrated["status_contexts"]
        }
        self.assertEqual(contexts["Switchboard CI / VM gate"], "success")
        self.assertEqual(contexts["Switchboard UI / Playwright"], "success")

    def test_empty_status_contexts_list_still_hydrates(self):
        supplied = {
            "number": 849,
            "head": {"sha": HEAD, "ref": "codex/BUG-172"},
            "base": {"ref": "master"},
            "status_contexts": [],
        }
        statuses = {
            "statuses": [
                {"context": "Switchboard CI / VM gate", "state": "success"},
            ],
        }
        with mock.patch.object(
            merge_gate_command.urllib.request,
            "urlopen",
            return_value=_GitHubResponse(statuses),
        ):
            hydrated, source = merge_gate_command._merge_gate_pr_evidence(
                "",
                849,
                {"github_pr": supplied},
                REPO,
            )
        self.assertTrue(source.get("hydrated_status_contexts"))
        self.assertEqual(
            hydrated["status_contexts"][0]["context"],
            "Switchboard CI / VM gate",
        )


class DurablePublish(unittest.TestCase):
    def setUp(self):
        self.gate = _load_pr_gate()

    def test_post_status_retries_then_confirms_via_readback(self):
        attempts = {"post": 0, "get": 0}
        posted = []

        def _req(method, path, *, token, body=None):
            if method == "GET" and "/statuses" in path:
                attempts["get"] += 1
                # First GET (idempotency check) empty; after POSTs, return match
                # only once a successful POST has landed.
                if posted and attempts["post"] >= 2:
                    return [{
                        "context": MERGE_CONTEXT,
                        "state": "success",
                        "description": "Exact-head CI, review, and merge gate passed",
                    }]
                return []
            if method == "POST":
                attempts["post"] += 1
                if attempts["post"] == 1:
                    raise self.gate.GateError(
                        "GitHub API POST failed: HTTP 502 upstream"
                    )
                posted.append(body)
                return {"ok": True, "state": body["state"]}
            return {}

        self.gate._github_request = _req
        result = self.gate.post_status(
            REPO,
            HEAD,
            "success",
            context=MERGE_CONTEXT,
            description="Exact-head CI, review, and merge gate passed",
            token="t",
        )
        self.assertTrue(result.get("published"))
        self.assertGreaterEqual(attempts["post"], 2)
        self.assertEqual(posted[-1]["state"], "success")

    def test_post_status_credential_failure_does_not_claim_published(self):
        def _req(method, path, *, token, body=None):
            if method == "GET":
                return []
            raise self.gate.GateError(
                "GitHub API POST failed: HTTP 401 Bad credentials"
            )

        self.gate._github_request = _req
        with self.assertRaises(self.gate.GateError) as ctx:
            self.gate.post_status(
                REPO,
                HEAD,
                "success",
                context=MERGE_CONTEXT,
                description="Exact-head CI, review, and merge gate passed",
                token="bad",
            )
        self.assertIn("401", str(ctx.exception))

    def test_duplicate_unchanged_post_is_idempotent_success(self):
        def _req(method, path, *, token, body=None):
            if method == "GET":
                return [{
                    "context": MERGE_CONTEXT,
                    "state": "success",
                    "description": "Exact-head CI, review, and merge gate passed",
                }]
            raise AssertionError("unchanged status must not POST")

        self.gate._github_request = _req
        result = self.gate.post_status(
            REPO,
            HEAD,
            "success",
            context=MERGE_CONTEXT,
            description="Exact-head CI, review, and merge gate passed",
            token="t",
        )
        self.assertEqual(result.get("skipped"), "unchanged")


class PreserveValidAuthorization(unittest.TestCase):
    def setUp(self):
        self.gate = _load_pr_gate()

    def test_empty_stale_failure_does_not_overwrite_prior_success(self):
        posts = []
        self.gate.list_pr_files = lambda *_a, **_k: ["src/example.py"]
        self.gate.pr_provenance_gate.evaluate_pr_provenance = lambda *_a, **_k: {
            "exempt": False,
            "resolved": [{"task_id": "BUG-173", "project": "switchboard"}],
        }
        self.gate.store.merge_gate = lambda payload, **_k: {
            "ok": False,
            "findings": [{
                "code": "missing_required_status_contexts",
                "message": "Required CI/status contexts are missing or not successful.",
                "blocking": True,
                "details": {
                    "status_contexts": {},
                    "missing_contexts": ["Switchboard CI / VM gate"],
                },
            }],
            "evidence_quality": "empty_or_stale",
        }
        self.gate.latest_status = lambda *_a, **_k: {
            "context": MERGE_CONTEXT,
            "state": "success",
            "description": "Exact-head CI, review, and merge gate passed",
        }
        self.gate.post_status = lambda *a, **k: posts.append({"args": a, "kwargs": k}) or {
            "published": True
        }

        result = self.gate.run_merge_authorization_for_pr(
            {
                "number": 849,
                "html_url": f"https://github.com/{REPO}/pull/849",
                "head": {"sha": HEAD, "ref": "codex/BUG-172"},
            },
            repo=REPO,
            token="t",
        )
        self.assertEqual(result["state"], "success")
        self.assertEqual(result.get("skipped"), "preserve_valid_authorization")
        self.assertEqual(posts, [])


class MergeGroupAuthorization(unittest.TestCase):
    def test_handle_merge_group_publishes_merge_authorization_on_temp_sha(self):
        published = {}

        def _publish(repo, head_sha, head_ref, project=""):
            published.update({
                "repo": repo,
                "head_sha": head_sha,
                "head_ref": head_ref,
                "project": project,
                "pr_number": 849,
            })
            return {
                "published": True,
                "pr": 849,
                "sha": head_sha,
                "state": "success",
                "context": MERGE_CONTEXT,
            }

        with (
            mock.patch.object(
                github_sync.verify_ci_command,
                "verify",
                return_value={
                    "ok": True,
                    "sha": MERGE_GROUP_SHA,
                    "status": "pending",
                    "ensured": True,
                    "run_id": "run-mg",
                    "ensure_result": {
                        "dispatched": True,
                        "skip_reason": None,
                        "run_id": "run-mg",
                        "head_sha": MERGE_GROUP_SHA,
                    },
                },
            ),
            mock.patch.object(
                github_sync,
                "_repo_role",
                return_value={"canonical": True, "role": "canonical", "repo": REPO},
            ),
            mock.patch.object(
                github_sync,
                "_maybe_publish_merge_group_authorization",
                side_effect=_publish,
            ),
        ):
            res = github_sync.handle_merge_group(
                {
                    "action": "checks_requested",
                    "repository": {
                        "full_name": REPO,
                        "default_branch": "master",
                    },
                    "merge_group": {
                        "head_sha": MERGE_GROUP_SHA,
                        "head_ref": (
                            f"refs/heads/gh-readonly-queue/master/"
                            f"pr-849-{MERGE_GROUP_SHA}"
                        ),
                    },
                },
                "switchboard",
            )

        self.assertEqual(res["action"], "merge_group_ci_dispatched")
        self.assertEqual(published["head_sha"], MERGE_GROUP_SHA)
        self.assertEqual(published["pr_number"], 849)
        self.assertTrue((res.get("merge_authorization") or {}).get("published"))

    def test_parse_merge_group_pr_number_from_head_ref(self):
        pr = github_sync._merge_group_pr_number(
            f"refs/heads/gh-readonly-queue/master/pr-849-{MERGE_GROUP_SHA}"
        )
        self.assertEqual(pr, 849)


class ProtectedPrIntegrationShape(unittest.TestCase):
    """Green exact-head gate must publish success without operator status writes."""

    def test_run_merge_authorization_publishes_success_for_clean_gate(self):
        gate = _load_pr_gate()
        posts = []
        gate.list_pr_files = lambda *_a, **_k: ["src/example.py"]
        gate.pr_provenance_gate.evaluate_pr_provenance = lambda *_a, **_k: {
            "exempt": False,
            "resolved": [{"task_id": "BUG-173", "project": "switchboard"}],
        }
        gate.store.merge_gate = lambda *_a, **_k: {"ok": True, "findings": []}
        gate.post_status = lambda repo, sha, state, **kwargs: posts.append({
            "repo": repo, "sha": sha, "state": state, **kwargs,
        }) or {"published": True, "state": state}

        result = gate.run_merge_authorization_for_pr(
            {
                "number": 849,
                "html_url": f"https://github.com/{REPO}/pull/849",
                "head": {"sha": HEAD, "ref": "codex/BUG-172"},
            },
            repo=REPO,
            token="t",
            status_sha=MERGE_GROUP_SHA,
        )
        self.assertEqual(result["state"], "success")
        self.assertEqual(posts[-1]["sha"], MERGE_GROUP_SHA)
        self.assertEqual(posts[-1]["context"], MERGE_CONTEXT)
        self.assertEqual(posts[-1]["state"], "success")


if __name__ == "__main__":
    unittest.main(verbosity=2)
