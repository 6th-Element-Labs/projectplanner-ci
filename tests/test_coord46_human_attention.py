#!/usr/bin/env python3
"""COORD-46 attention closeout (live remainder after SIMPLIFY-30).

The v1 classifier/effect-executor path that once minted human attention items
was deleted; ``agent_requires_human`` / ``record_human_blocker`` are now the
only authors of route=human closeouts. What stays pinned here is the frozen
closeout contract itself: the truthful choice ladder per human reason, and the
operator provider-item projection of a completion closeout.
"""
from __future__ import annotations

import unittest

from path_setup import ROOT  # noqa: F401

from switchboard.api.routers.attention import _provider_item
from switchboard.domain.completion.human_closeout import (
    build_human_closeout_request,
)

HEAD = "c" * 40
PR_812 = "https://github.com/6th-Element-Labs/projectplanner/pull/812"


def _pr812_snapshot(**extra):
    """A plain completion-snapshot mapping (the v1 builder is deleted)."""
    snap = {
        "schema": "switchboard.completion_snapshot.v1",
        "task_id": "COORD-20",
        "task": {
            "task_id": "COORD-20",
            "status": "In Review",
            "git_state": {
                "head_sha": HEAD, "pr_number": 812, "pr_url": PR_812,
            },
            "deliverable": {
                "deliverable_id": "alerts", "milestone_id": "alerts-m3-ui",
            },
        },
        "head_sha": HEAD,
        "pr_number": 812,
        "pr_url": PR_812,
        "status_contexts": {
            "Switchboard CI / VM gate": {
                "name": "Switchboard CI / VM gate",
                "conclusion": "success",
            },
        },
        "review": {"status": "passed", "head_sha": HEAD, "pr_url": PR_812},
        "merge_gate": {
            "findings": [{
                "code": "credentialed_live_proof_unavailable",
                "failure_class": "absent_permission",
                "blocking": True,
                "message": (
                    "Eligible authenticated host/credential required for "
                    "live proof"
                ),
            }],
        },
        "work_session": {
            "work_session_id": "worksession-812",
            "status": "blocked",
        },
        "runner": {
            "live": True,
            "runner_session_id": "runner-812",
            "generation": 4,
            "role": "review_merge",
            "head_sha": HEAD,
            "host_id": "host-812",
        },
    }
    snap.update(extra)
    return snap


class HumanCloseoutContract(unittest.TestCase):
    def test_noncredential_human_reasons_offer_truthful_choices(self):
        cases = {
            "wrong_target_branch": "correct_target_branch",
            "canonical_repo_missing": "configure_canonical_repo",
            "pr_closed_unmerged": "reopen_pull_request",
            "review_retry_budget_exhausted": "resolve_finding",
            "human_review_findings": "resolve_finding",
        }
        for reason, expected_choice in cases.items():
            with self.subTest(reason=reason):
                request = build_human_closeout_request(
                    plan={
                        "task_id": "COORD-20", "pr_number": 812,
                        "head_sha": HEAD, "idem_key": f"human:{reason}",
                        "reason_code": reason,
                    },
                    decision={"reason_code": reason},
                    snapshot=_pr812_snapshot(),
                    run={"run_id": "run", "state_version": 1},
                )
                self.assertEqual(request["choices"][0]["id"], expected_choice)
                if "credential" not in reason:
                    self.assertNotEqual(
                        request["choices"][0]["id"], "supply_credential")

    def test_completion_closeout_projects_as_blocking_provider_item(self):
        item = _provider_item({
            "request_id": "attention-x",
            "task_id": "COORD-20",
            "provider": "switchboard.completion",
            "prompt": "Supply credential",
            "created_at": 1.0,
            "expires_at": None,
            "context": {"reason_code": "credentialed_live_proof_unavailable"},
            "choices": [{"id": "supply_credential"}],
            "recommended_default": {"id": "supply_credential"},
            "version": 1,
        })
        self.assertEqual(item["source"], "provider")
        self.assertEqual(item["delivery_impact"], "blocking")
        self.assertEqual(item["decide"]["body"]["expected_version"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
