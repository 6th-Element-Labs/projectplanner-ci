#!/usr/bin/env python3
"""BUG-182: DIRTY open PRs must not classify as exact_head_pr_missing.

A conflicted PR that GitHub still reports OPEN at the evaluated head must
reach ``pr_merge_conflict`` → remediation. When live PR hydration fails but
the board still names a PR (or merge_gate already saw DIRTY), that must be a
loud hydration / conflict signal — never indistinguishable from "no PR".
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.application import completion_driver
from switchboard.application.commands.merge_gate import _merge_gate_finding
from switchboard.domain.completion.state_machine import (
    build_completion_snapshot,
    classify_completion,
)


HEAD = "8a79c572037366f1b100531441b19908c4717b51"
PR_URL = "https://github.com/6th-Element-Labs/projectplanner/pull/863"

def _dirty_pr_finding():
    """The conflict finding built by the gate's OWN constructor.

    Hand-building it with a nested "details" key is what let the original
    BUG-182 fix ship dead: _merge_gate_finding SPLATS details, so the shape the
    test asserted against never occurs in production.
    """
    return _merge_gate_finding(
        "pr_not_mergeable",
        "GitHub PR state is not cleanly mergeable.",
        "failed_gate",
        details={"mergeable": False, "merge_state": "dirty"},
    )



def _board_task(**overrides):
    task = {
        "task_id": "CO-20",
        "status": "In Review",
        "git_state": {
            "head_sha": HEAD,
            "pr_number": 863,
            "pr_url": PR_URL,
            "branch": "claude/CO-20-hybrid-placement",
        },
        "review_verdict": {
            "current_verdict": {
                "status": "passed",
                "head_sha": HEAD,
                "number": 863,
                "pr_url": PR_URL,
            },
        },
        "session_health": {"latest_sessions": []},
        "provenance": {},
    }
    task.update(overrides)
    return task


def _dirty_pr():
    return {
        "number": 863,
        "state": "open",
        "draft": True,
        "mergeable": False,
        "mergeable_state": "dirty",
        "mergeStateStatus": "DIRTY",
        "html_url": PR_URL,
        "head": {"sha": HEAD, "ref": "claude/CO-20-hybrid-placement"},
        "status_contexts": [{
            "context": "Switchboard CI / VM gate",
            "state": "success",
            "failure_attribution": "product",
        }],
    }


class Bug182Classifier(unittest.TestCase):
    def test_dirty_pr_routes_remediation_even_with_coord_findings(self):
        """Merge conflicts outrank missing evidence so an agent rebases first."""
        snap = build_completion_snapshot(
            task=_board_task(),
            github_pr=_dirty_pr(),
            required_status_contexts=["Switchboard CI / VM gate"],
            review={
                "status": "passed", "head_sha": HEAD, "number": 863,
                "pr_url": PR_URL,
            },
            merge_gate={
                "findings": [{
                    "code": "missing_executed_test_run",
                    "failure_class": "missing_data",
                    "blocking": True,
                }],
            },
        )
        decision = classify_completion(None, snap)
        self.assertEqual(decision["reason_code"], "pr_merge_conflict")
        self.assertEqual(decision["route"], "remediation")
        self.assertEqual(decision["desired_role"], "remediation")

    def test_empty_live_pr_with_board_identity_is_not_exact_head_pr_missing(self):
        """Board-named PR + empty hydration must not look like 'no PR exists'."""
        snap = build_completion_snapshot(
            task=_board_task(),
            github_pr={},
            review={
                "status": "passed", "head_sha": HEAD, "number": 863,
                "pr_url": PR_URL,
            },
            merge_gate={
                "findings": [{
                    "code": "github_pr_state_unavailable",
                    "failure_class": "broken_connection",
                    "blocking": True,
                }],
                "head_sha": HEAD,
                "pr_number": 863,
                "pr_url": PR_URL,
            },
        )
        # Prefer board/gate head so the decision is about hydration, not a blank head.
        snap["head_sha"] = HEAD
        decision = classify_completion(None, snap)
        self.assertNotEqual(decision["reason_code"], "exact_head_pr_missing")
        self.assertEqual(decision["reason_code"], "github_pr_state_unavailable")
        # Mission Bot treats unavailable PR state as factory failure → remediation.
        self.assertEqual(decision["route"], "remediation")

    def test_empty_live_pr_with_dirty_gate_finding_routes_conflict(self):
        """merge_gate already saw DIRTY — do not discard that as PR-missing."""
        snap = build_completion_snapshot(
            task=_board_task(),
            github_pr={},
            review={
                "status": "passed", "head_sha": HEAD, "number": 863,
                "pr_url": PR_URL,
            },
            merge_gate={
                "findings": [_dirty_pr_finding()],
                "head_sha": HEAD,
                "pr_number": 863,
                "pr_url": PR_URL,
            },
        )
        snap["head_sha"] = HEAD
        decision = classify_completion(None, snap)
        self.assertEqual(decision["reason_code"], "pr_merge_conflict")
        self.assertEqual(decision["route"], "remediation")

    def test_genuine_missing_pr_is_not_confused_with_dirty_hydration(self):
        """No board PR identity: Mission Bot does not invent exact_head_pr_missing.

        In Review without a PR falls through to review/implementation mission
        outputs rather than the retired missing-PR classifier reason.
        """
        snap = build_completion_snapshot(
            task={
                "task_id": "CO-20",
                "status": "In Review",
                "git_state": {"head_sha": HEAD},
            },
            github_pr={},
        )
        snap["head_sha"] = HEAD
        decision = classify_completion(None, snap)
        self.assertNotEqual(decision["reason_code"], "pr_merge_conflict")
        self.assertIn(
            decision["reason_code"],
            {"review_required", "needs_implementation"},
        )
        self.assertIn(
            decision["route"],
            {"review_merge", "implementation"},
        )


class Bug182Hydrator(unittest.TestCase):
    def test_hydrator_reuses_one_pr_fetch_for_merge_gate_and_snapshot(self):
        """Dual independent fetches can leave snapshot empty while gate saw DIRTY."""
        class Store:
            @staticmethod
            def get_task(_task_id, project):
                return _board_task()

            @staticmethod
            def get_project_github_repo(_project):
                return "6th-Element-Labs/projectplanner"

        dirty = _dirty_pr()
        gate = {
            "task_id": "CO-20",
            "pr_number": 863,
            "pr_url": PR_URL,
            "head_sha": HEAD,
            "github_pr": dirty,
            "findings": [_dirty_pr_finding()],
            "required_status_contexts": ["Switchboard CI / VM gate"],
            "status_contexts": {
                "Switchboard CI / VM gate": {
                    "name": "Switchboard CI / VM gate", "state": "success",
                },
            },
            "review_gate": {
                "status": "passed", "head_sha": HEAD, "number": 863,
                "pr_url": PR_URL,
            },
        }
        with (
            patch(
                "switchboard.storage.repositories.provenance._github_token",
                return_value="token",
            ),
            patch(
                "switchboard.storage.repositories.provenance._github_pr",
                return_value=dirty,
            ) as fetch_pr,
            patch(
                "switchboard.application.commands.merge_gate.merge_gate",
                return_value=gate,
            ) as merge_gate,
            patch(
                "switchboard.application.queries.task_session.execute_for",
                return_value={},
            ),
        ):
            snapshot = completion_driver.hydrate_completion_snapshot(
                "CO-20", project="switchboard", actor="owner",
                store_mod=Store,
            )
        self.assertEqual(fetch_pr.call_count, 1)
        evidence = (merge_gate.call_args.args[0].get("evidence") or {})
        self.assertEqual(evidence.get("github_pr"), dirty)
        self.assertEqual(snapshot["github_pr"]["mergeStateStatus"], "DIRTY")
        self.assertEqual(snapshot["github_pr"]["number"], 863)

    def test_hydrator_prefers_merge_gate_pr_when_direct_fetch_fails(self):
        class Store:
            @staticmethod
            def get_task(_task_id, project):
                return _board_task()

            @staticmethod
            def get_project_github_repo(_project):
                return "6th-Element-Labs/projectplanner"

        dirty = _dirty_pr()
        gate = {
            "task_id": "CO-20",
            "pr_number": 863,
            "pr_url": PR_URL,
            "head_sha": HEAD,
            "github_pr": dirty,
            "findings": [_dirty_pr_finding()],
            "required_status_contexts": ["Switchboard CI / VM gate"],
            "status_contexts": {
                "Switchboard CI / VM gate": {
                    "name": "Switchboard CI / VM gate", "state": "success",
                },
            },
            "review_gate": {
                "status": "passed", "head_sha": HEAD, "number": 863,
                "pr_url": PR_URL,
            },
        }
        with (
            patch(
                "switchboard.storage.repositories.provenance._github_token",
                return_value="token",
            ),
            patch(
                "switchboard.storage.repositories.provenance._github_pr",
                return_value=None,
            ),
            patch(
                "switchboard.application.commands.merge_gate.merge_gate",
                return_value=gate,
            ),
            patch(
                "switchboard.application.queries.task_session.execute_for",
                return_value={},
            ),
        ):
            snapshot = completion_driver.hydrate_completion_snapshot(
                "CO-20", project="switchboard", actor="owner",
                store_mod=Store,
            )
        self.assertEqual(snapshot["github_pr"]["number"], 863)
        self.assertEqual(
            str(snapshot["github_pr"].get("mergeStateStatus") or "").upper(),
            "DIRTY",
        )
        decision = classify_completion(None, snapshot)
        self.assertEqual(decision["reason_code"], "pr_merge_conflict")


if __name__ == "__main__":
    unittest.main()
