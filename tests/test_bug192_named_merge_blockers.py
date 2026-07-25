"""BUG-192 — the merge-authorization status names what it found.

A GitHub commit status carries only state/context/description, so the typed finding
`code` genuinely cannot survive the trip to branch protection. The description can, and
it was being spent on a constant.

Live on PR #899, 2026-07-25T19:11:41Z, the published status read exactly:

    failure  "PR branch does not match task/session evidence."

while the finding it was built from carried `expected_branch` and `pr_branch`, and the
gate result carried the task id that produced it. Three values computed, three values
dropped. Diagnosing a one-word branch mistake therefore required reading
task_id_parser.py and merge_gate.py. The condition never self-heals, so the PR was
permanently unmergeable with a description that named nothing.

These tests pin: the identifying values reach the description, the description stays
inside GitHub's 140-character cap, truncation keeps the values rather than the prose,
and a malformed finding can never break publishing.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands.merge_gate import (
    STATUS_DESCRIPTION_LIMIT,
    _merge_gate_finding,
    describe_blocking_finding,
)


def _load_gate():
    """Load scripts/switchboard_pr_gate.py the way the systemd unit does."""
    path = Path(ROOT) / "scripts" / "switchboard_pr_gate.py"
    spec = importlib.util.spec_from_file_location("switchboard_pr_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The exact finding that produced the useless status on PR #899.
def _stale_branch_finding():
    return _merge_gate_finding(
        "stale_branch",
        "PR branch does not match task/session evidence.",
        "stale_branch",
        details={"expected_branch": "claude/COORD-51-corpus-close",
                 "pr_branch": "claude/COORD-51-evidence-identity"},
    )


class DescriptionNamesTheFindingTest(unittest.TestCase):
    def test_the_live_incident_now_names_both_branches_and_the_task(self):
        described = describe_blocking_finding(
            _stale_branch_finding(), task_id="COORD-51")
        self.assertIn("claude/COORD-51-corpus-close", described)
        self.assertIn("claude/COORD-51-evidence-identity", described)
        self.assertIn("COORD-51", described)
        self.assertLessEqual(len(described), STATUS_DESCRIPTION_LIMIT)

    def test_it_reads_the_splatted_shape_the_gate_emits(self):
        # _merge_gate_finding puts details on the TOP level, not under "details".
        finding = _stale_branch_finding()
        self.assertNotIn("details", finding)
        self.assertIn("expected_branch", finding)
        self.assertIn("expected claude/COORD-51-corpus-close",
                      describe_blocking_finding(finding))

    def test_a_stale_head_sha_is_shortened_not_dropped(self):
        described = describe_blocking_finding(_merge_gate_finding(
            "stale_head_sha", "PR head SHA does not match task/session evidence.",
            "stale_branch",
            details={"expected_head_sha": "a" * 40, "pr_head_sha": "b" * 40}))
        self.assertIn("a" * 12, described)
        self.assertIn("b" * 12, described)
        self.assertLessEqual(len(described), STATUS_DESCRIPTION_LIMIT)

    def test_missing_contexts_are_named(self):
        described = describe_blocking_finding(_merge_gate_finding(
            "missing_required_status_contexts",
            "Required CI/status contexts are missing or not successful.",
            "failed_gate",
            details={"missing_contexts": ["Switchboard CI / VM gate"]}))
        self.assertIn("Switchboard CI / VM gate", described)

    def test_a_wrong_target_branch_names_both_branches(self):
        described = describe_blocking_finding(_merge_gate_finding(
            "wrong_target_branch", "PR base does not match target.", "failed_gate",
            details={"pr_base": "develop", "target_branch": "master"}))
        self.assertIn("develop", described)
        self.assertIn("master", described)

    def test_an_evidence_refusal_names_the_key_and_the_near_miss(self):
        """COORD-61's missing_artifact block, finally reaching a human."""
        described = describe_blocking_finding(_merge_gate_finding(
            "missing_executed_test_run",
            "Merge gate requires a passing executed test run with output/log hash.",
            "missing_data",
            details={"executed_test_gate": {"ok": False, "missing_artifact": {
                "expected_key": "executed_test_run",
                "found_near_miss": [{"key": "executed_tests",
                                     "work_session_id": "worksession-aa0ccd80bb504bbd"}],
            }}}))
        self.assertIn("executed_test_run", described)
        self.assertIn("executed_tests", described)
        self.assertLessEqual(len(described), STATUS_DESCRIPTION_LIMIT)

    def test_a_code_without_a_renderer_still_returns_its_message(self):
        described = describe_blocking_finding(_merge_gate_finding(
            "review_required", "Review required for current head abc123.",
            "failed_gate"))
        self.assertEqual(described, "Review required for current head abc123.")

    def test_the_task_id_leads_when_supplied(self):
        # When a PR resolves to several tasks, WHICH task disagreed is the answer.
        described = describe_blocking_finding(
            _stale_branch_finding(), task_id="COORD-51")
        self.assertTrue(described.startswith("COORD-51: "), described)


class DescriptionIsBoundedTest(unittest.TestCase):
    def test_every_description_fits_githubs_cap(self):
        oversized = [
            _merge_gate_finding("stale_branch", "M" * 400, "stale_branch",
                                details={"expected_branch": "e" * 300,
                                         "pr_branch": "p" * 300}),
            _merge_gate_finding("missing_required_status_contexts", "M" * 400,
                                "failed_gate",
                                details={"missing_contexts": [f"ctx-{i}" * 20
                                                              for i in range(40)]}),
            _merge_gate_finding("pr_not_mergeable", "M" * 400, "failed_gate",
                                details={"merge_state": "s" * 200}),
            _merge_gate_finding("review_required", "M" * 400, "failed_gate"),
        ]
        for finding in oversized:
            described = describe_blocking_finding(finding, task_id="T" * 60)
            self.assertLessEqual(
                len(described), STATUS_DESCRIPTION_LIMIT, finding.get("code"))

    def test_truncation_keeps_the_values_and_drops_the_prose(self):
        """The message is boilerplate; the values are what cannot be reconstructed."""
        described = describe_blocking_finding(
            _merge_gate_finding("stale_branch", "X" * 300, "stale_branch",
                                details={"expected_branch": "branch-alpha",
                                         "pr_branch": "branch-beta"}),
            task_id="COORD-99")
        self.assertLessEqual(len(described), STATUS_DESCRIPTION_LIMIT)
        self.assertIn("expected branch-alpha, PR has branch-beta", described)
        self.assertIn("…", described)


class DescriptionNeverBreaksPublishingTest(unittest.TestCase):
    """A status description is cosmetic; it must not be able to fail a gate run."""

    def test_degenerate_findings_are_tolerated(self):
        for junk in ({}, {"code": "stale_branch"}, {"message": "m"},
                     {"code": "stale_branch", "expected_branch": None},
                     {"code": "missing_executed_test_run",
                      "executed_test_gate": "not-a-mapping"},
                     {"code": "missing_required_status_contexts",
                      "missing_contexts": "not-a-list"}):
            described = describe_blocking_finding(junk)
            self.assertIsInstance(described, str)
            self.assertLessEqual(len(described), STATUS_DESCRIPTION_LIMIT)

    def test_an_empty_finding_falls_back_to_a_generic_message(self):
        self.assertEqual(
            describe_blocking_finding({}), "Switchboard merge gate blocked")


class PublisherUsesTheDescriptionTest(unittest.TestCase):
    """The renderer only matters if the publisher actually calls it."""

    def setUp(self):
        self.gate = _load_gate()
        self.posts = []
        self._originals = {
            "post_status": self.gate.post_status,
            "list_pr_files": self.gate.list_pr_files,
            "evaluate": self.gate.pr_provenance_gate.evaluate_pr_provenance,
            "merge_gate": self.gate.store.merge_gate,
        }
        self.gate.list_pr_files = lambda *a, **k: ["src/example.py"]
        self.gate.post_status = lambda repo, sha, state, **kwargs: self.posts.append(
            {"repo": repo, "sha": sha, "state": state, **kwargs})

    def tearDown(self):
        self.gate.post_status = self._originals["post_status"]
        self.gate.list_pr_files = self._originals["list_pr_files"]
        self.gate.pr_provenance_gate.evaluate_pr_provenance = self._originals["evaluate"]
        self.gate.store.merge_gate = self._originals["merge_gate"]

    def _run(self, resolved, gate_by_task):
        self.gate.pr_provenance_gate.evaluate_pr_provenance = (
            lambda *a, **k: {"exempt": False, "resolved": resolved})
        self.gate.store.merge_gate = (
            lambda payload, **k: gate_by_task[str(payload.get("task_id"))])
        return self.gate.run_merge_authorization_for_pr(
            {"number": 899,
             "html_url": "https://github.com/6th-Element-Labs/projectplanner/pull/899",
             "head": {"sha": "0c0eef0b", "ref": "claude/COORD-51-evidence-identity"}},
            repo="6th-Element-Labs/projectplanner",
            token="token-value",
        )

    def test_the_pr_899_scenario_publishes_an_actionable_description(self):
        """Two resolved tasks, the extra one blocks — exactly what happened."""
        result = self._run(
            resolved=[{"task_id": "COORD-61", "project": "switchboard"},
                      {"task_id": "COORD-51", "project": "switchboard"}],
            gate_by_task={
                "COORD-61": {"ok": True, "task_id": "COORD-61", "findings": []},
                "COORD-51": {"ok": False, "task_id": "COORD-51",
                             "findings": [_stale_branch_finding()]},
            },
        )
        self.assertEqual(result["state"], "failure")
        self.assertEqual(result["reason"], "stale_branch")

        description = self.posts[-1]["description"]
        self.assertNotEqual(
            description, "PR branch does not match task/session evidence.",
            "the published status must no longer be the bare constant")
        self.assertIn("COORD-51", description)
        self.assertIn("claude/COORD-51-corpus-close", description)
        self.assertIn("claude/COORD-51-evidence-identity", description)

    def test_a_single_task_pr_omits_the_redundant_task_prefix(self):
        self._run(
            resolved=[{"task_id": "COORD-61", "project": "switchboard"}],
            gate_by_task={"COORD-61": {"ok": False, "task_id": "COORD-61",
                                       "findings": [_stale_branch_finding()]}},
        )
        description = self.posts[-1]["description"]
        self.assertFalse(description.startswith("COORD-61:"), description)
        self.assertIn("claude/COORD-51-corpus-close", description)

    def test_a_clean_gate_still_publishes_success_unchanged(self):
        result = self._run(
            resolved=[{"task_id": "COORD-61", "project": "switchboard"}],
            gate_by_task={"COORD-61": {"ok": True, "task_id": "COORD-61",
                                       "findings": []}},
        )
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["reason"], "exact_head_merge_gate_passed")
        self.assertEqual(self.posts[-1]["description"],
                         "Exact-head CI, review, and merge gate passed")

    def test_the_published_description_always_fits_the_cap(self):
        self._run(
            resolved=[{"task_id": "COORD-61", "project": "switchboard"},
                      {"task_id": "COORD-51", "project": "switchboard"}],
            gate_by_task={
                "COORD-61": {"ok": True, "task_id": "COORD-61", "findings": []},
                "COORD-51": {"ok": False, "task_id": "COORD-51", "findings": [
                    _merge_gate_finding("stale_branch", "M" * 400, "stale_branch",
                                        details={"expected_branch": "e" * 200,
                                                 "pr_branch": "p" * 200})]},
            },
        )
        self.assertLessEqual(
            len(self.posts[-1]["description"]), STATUS_DESCRIPTION_LIMIT)


if __name__ == "__main__":
    unittest.main()
