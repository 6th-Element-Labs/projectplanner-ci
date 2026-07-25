#!/usr/bin/env python3
"""BUG-193: hand-run merge_gate must not emit a phantom pr_not_mergeable.

GitHub reports mergeStateStatus=BLOCKED when any required status is red —
including Switchboard's own ``merge authorization`` context. The publisher
already strips that self-referential roll-up before calling merge_gate; the
MCP / hydration path must apply the same strip so a mergeable PR is not told
it is blocked by the gate evaluating itself.
"""
from __future__ import annotations

import unittest
from unittest import mock

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands import merge_gate as merge_gate_command


HEAD = "0c0eef0be9eac80810df49cef3904a6bacf5501f"
PR_URL = "https://github.com/6th-Element-Labs/projectplanner/pull/900"
REPO = "6th-Element-Labs/projectplanner"


def _blocked_but_mergeable_pr(**overrides):
    """Exact shape that produced the phantom finding on PR #900."""
    pr = {
        "number": 900,
        "html_url": PR_URL,
        "draft": False,
        "mergeable": True,
        "mergeable_state": "blocked",
        "mergeStateStatus": "BLOCKED",
        "base": {"ref": "master"},
        "head": {"sha": HEAD, "ref": "claude/COORD-61"},
        "status_contexts": [
            {"context": "Switchboard CI / VM gate", "state": "success"},
            {
                "context": "Switchboard / merge authorization",
                "state": "failure",
                "description": "no review verdict at head",
            },
        ],
    }
    pr.update(overrides)
    return pr


class Bug193StripOnHydration(unittest.TestCase):
    def test_github_api_hydration_drops_self_referential_blocked(self):
        with mock.patch.object(
            merge_gate_command, "_github_pr", return_value=_blocked_but_mergeable_pr()
        ), mock.patch.object(merge_gate_command, "_github_token", return_value=""):
            hydrated, source = merge_gate_command._merge_gate_pr_evidence(
                PR_URL, 900, {}, REPO
            )

        self.assertEqual(source.get("source"), "github_api")
        self.assertTrue(hydrated.get("mergeable") is True)
        for key in ("mergeable_state", "mergeStateStatus", "merge_state"):
            self.assertNotIn(
                key,
                hydrated,
                f"{key} must be stripped so merge_gate cannot self-deadlock",
            )

    def test_dirty_and_behind_survive_hydration_strip(self):
        for state in ("dirty", "behind"):
            with self.subTest(state=state), mock.patch.object(
                merge_gate_command,
                "_github_pr",
                return_value=_blocked_but_mergeable_pr(
                    mergeable=False,
                    mergeable_state=state,
                    mergeStateStatus=state.upper(),
                ),
            ), mock.patch.object(merge_gate_command, "_github_token", return_value=""):
                hydrated, _ = merge_gate_command._merge_gate_pr_evidence(
                    PR_URL, 900, {}, REPO
                )
            self.assertEqual(hydrated.get("mergeable_state"), state)
            self.assertIs(hydrated.get("mergeable"), False)


def _would_emit_pr_not_mergeable(pr: dict) -> bool:
    """Mirror merge_gate's mergeability predicate after hydration."""
    mergeable = merge_gate_command._merge_gate_bool(pr.get("mergeable"), default=True)
    merge_state = str(
        pr.get("mergeable_state")
        or pr.get("mergeStateStatus")
        or pr.get("merge_state")
        or ""
    ).strip().lower()
    return mergeable is False or merge_state in {
        "dirty", "blocked", "behind", "unstable", "unknown",
    }


class Bug193MergeGateVerdict(unittest.TestCase):
    def test_hand_run_hydration_does_not_trip_pr_not_mergeable(self):
        with mock.patch.object(
            merge_gate_command, "_github_pr", return_value=_blocked_but_mergeable_pr()
        ), mock.patch.object(merge_gate_command, "_github_token", return_value=""):
            hydrated, source = merge_gate_command._merge_gate_pr_evidence(
                PR_URL, 900, {}, REPO
            )
        self.assertEqual(source.get("source"), "github_api")
        self.assertFalse(
            _would_emit_pr_not_mergeable(hydrated),
            "mergeable=true + self-referential blocked must not emit pr_not_mergeable",
        )

    def test_real_conflict_still_trips_pr_not_mergeable(self):
        with mock.patch.object(
            merge_gate_command,
            "_github_pr",
            return_value=_blocked_but_mergeable_pr(
                mergeable=False,
                mergeable_state="dirty",
                mergeStateStatus="DIRTY",
            ),
        ), mock.patch.object(merge_gate_command, "_github_token", return_value=""):
            hydrated, _ = merge_gate_command._merge_gate_pr_evidence(
                PR_URL, 900, {}, REPO
            )
        self.assertTrue(_would_emit_pr_not_mergeable(hydrated))

    def test_supplied_evidence_also_gets_stripped(self):
        """Publisher already strips; merge_gate must strip too so paths agree."""
        hydrated, source = merge_gate_command._merge_gate_pr_evidence(
            PR_URL,
            900,
            {"github_pr": _blocked_but_mergeable_pr()},
            REPO,
        )
        self.assertEqual(source.get("source"), "supplied_evidence")
        self.assertFalse(_would_emit_pr_not_mergeable(hydrated))


if __name__ == "__main__":
    unittest.main()
