#!/usr/bin/env python3
"""COORD-46: classify real payloads, not hand-tidied ones.

Two production shapes never reach the classifier's contract on their own:

* raw GitHub ``StatusContext`` rows carry no ``failure_attribution`` field, so
  every red required check lands on ``required_ci_failure_unknown`` -> human;
* COORD-20 emits findings as ``class=auto|escalate``, not the classifier's
  ``finding_class``, and a single escalation must not collapse the automatic
  work into ``route=human``.
"""
from __future__ import annotations

import unittest

from path_setup import ROOT  # noqa: F401

from switchboard.domain.completion import normalize  # noqa: E402
from switchboard.domain.completion.state_machine import (  # noqa: E402
    build_completion_snapshot, classify_completion,
)

HEAD = "88624a605727fd44df98191d5b7dd99c73b75d9c"


class StatusContextAttribution(unittest.TestCase):
    def test_raw_failure_with_test_evidence_is_product(self):
        row = normalize.normalize_status_context(
            {"name": "Switchboard CI / VM gate", "conclusion": "FAILURE",
             "description": "3 tests failed"})
        self.assertEqual(row["failure_attribution"], "product")

    def test_runner_and_host_failures_are_infrastructure(self):
        for text in ("Host key verification failed",
                     "runner lost communication",
                     "The self-hosted runner did not respond"):
            row = normalize.normalize_status_context(
                {"name": "gate", "conclusion": "failure", "description": text})
            self.assertEqual(row["failure_attribution"], "infrastructure", text)

    def test_permission_failures_are_authority(self):
        row = normalize.normalize_status_context(
            {"name": "gate", "conclusion": "failure",
             "description": "Resource not accessible by integration"})
        self.assertEqual(row["failure_attribution"], "authority")

    def test_action_required_stays_authority(self):
        row = normalize.normalize_status_context(
            {"name": "gate", "conclusion": "action_required"})
        self.assertEqual(row["failure_attribution"], "authority")

    def test_cancelled_is_infrastructure_not_product(self):
        row = normalize.normalize_status_context(
            {"name": "gate", "conclusion": "cancelled"})
        self.assertEqual(row["failure_attribution"], "infrastructure")

    def test_an_explicit_attribution_is_never_overwritten(self):
        row = normalize.normalize_status_context(
            {"name": "gate", "conclusion": "failure",
             "description": "runner lost communication",
             "failure_attribution": "product"})
        self.assertEqual(row["failure_attribution"], "product")

    def test_a_bare_required_failure_defaults_to_product_not_human(self):
        """The whole point: an unadorned red required check is the PR's fault."""
        row = normalize.normalize_status_context(
            {"name": "Switchboard CI / VM gate", "state": "failure"})
        self.assertEqual(row["failure_attribution"], "product")

    def test_success_rows_carry_no_attribution(self):
        row = normalize.normalize_status_context(
            {"name": "gate", "conclusion": "success"})
        self.assertIsNone(row.get("failure_attribution"))


class ReviewFindingNormalization(unittest.TestCase):
    def test_coord20_auto_class_becomes_automatic(self):
        out = normalize.normalize_review_findings([{"class": "auto", "code": "x"}])
        self.assertEqual(out["automatic"][0]["finding_class"], "automatic")
        self.assertEqual(out["escalated"], [])

    def test_coord20_escalate_class_becomes_judgment(self):
        out = normalize.normalize_review_findings([{"class": "escalate", "code": "y"}])
        self.assertEqual(out["escalated"][0]["finding_class"], "judgment")
        self.assertEqual(out["automatic"], [])

    def test_mixed_findings_keep_both_sides_separate(self):
        out = normalize.normalize_review_findings([
            {"class": "auto", "code": "a"},
            {"class": "escalate", "code": "b"},
            {"class": "auto", "code": "c"},
        ])
        self.assertEqual([f["code"] for f in out["automatic"]], ["a", "c"])
        self.assertEqual([f["code"] for f in out["escalated"]], ["b"])
        self.assertTrue(out["has_automatic"])
        self.assertTrue(out["has_escalated"])


class Pr810RawFixture(unittest.TestCase):
    """Acceptance 8: observed payload, no synthetic attribution field."""

    def _snapshot(self):
        return build_completion_snapshot(
            task={"task_id": "COORD-41", "status": "In Review",
                  "git_state": {"head_sha": HEAD}},
            github_pr={"number": 810, "state": "open", "draft": True,
                       "mergeable": True, "mergeStateStatus": "BLOCKED",
                       "head": {"sha": HEAD}},
            required_status_contexts=["Switchboard CI / VM gate",
                                      "Switchboard CI / Playwright"],
            # Raw GitHub rows: no failure_attribution anywhere.
            status_contexts=[
                {"name": "Switchboard CI / VM gate", "state": "FAILURE",
                 "description": "2 tests failed"},
                {"name": "Switchboard CI / Playwright", "state": "FAILURE",
                 "description": "1 test failed"},
            ],
            review={"status": "passed", "head_sha": HEAD},
            runner={"live": True, "role": "review_merge", "head_sha": HEAD,
                    "generation": 9},
        )

    def test_raw_red_ci_routes_remediation_not_human(self):
        decision = classify_completion(None, normalize.normalize_snapshot(self._snapshot()))
        self.assertEqual(decision["route"], "remediation")
        self.assertEqual(decision["desired_role"], "remediation")
        self.assertEqual(decision["board_projection"], "Blocked")

    def test_without_normalization_it_would_have_gone_to_human(self):
        """Pins the defect this normalization exists to fix."""
        decision = classify_completion(None, self._snapshot())
        self.assertEqual(decision["route"], "human")


class Pr812RawFixture(unittest.TestCase):
    """Acceptance 9: changes_requested with class=auto, escalate, auto."""

    def _snapshot(self):
        return normalize.normalize_snapshot(build_completion_snapshot(
            task={"task_id": "ADAPTER-24", "status": "In Review",
                  "git_state": {"head_sha": HEAD}},
            github_pr={"number": 812, "state": "open", "draft": False,
                       "mergeable": True, "mergeStateStatus": "BLOCKED",
                       "head": {"sha": HEAD}},
            required_status_contexts=["Switchboard CI / VM gate"],
            status_contexts=[{"name": "Switchboard CI / VM gate",
                              "state": "success"}],
            review={"status": "changes_requested", "head_sha": HEAD,
                    "findings": [
                        {"class": "auto", "code": "missing_test"},
                        {"class": "escalate", "code": "design_judgment"},
                        {"class": "auto", "code": "lint"},
                    ]},
            runner={"live": False},
        ))

    def test_mixed_findings_dispatch_automatic_remediation(self):
        decision = classify_completion(None, self._snapshot())
        self.assertEqual(decision["route"], "remediation")
        self.assertEqual(decision["desired_role"], "remediation")

    def test_the_escalation_is_retained_separately_not_dropped(self):
        decision = classify_completion(None, self._snapshot())
        escalated = decision.get("escalated_findings") or []
        self.assertEqual([f.get("code") for f in escalated], ["design_judgment"])

    def test_only_the_automatic_findings_go_to_the_coder(self):
        decision = classify_completion(None, self._snapshot())
        automatic = decision.get("acceptance_findings") or []
        self.assertEqual([f.get("code") for f in automatic],
                         ["missing_test", "lint"])

    def test_all_escalations_still_require_a_human(self):
        snap = self._snapshot()
        snap["review"]["findings"] = [{"class": "escalate", "code": "j",
                                       "finding_class": "judgment"}]
        self.assertEqual(classify_completion(None, snap)["route"], "human")


if __name__ == "__main__":
    unittest.main(verbosity=2)
