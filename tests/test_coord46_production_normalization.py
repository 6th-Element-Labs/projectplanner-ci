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

from itertools import permutations
import unittest
from unittest import mock

from path_setup import ROOT  # noqa: F401

from switchboard.domain.completion import effects, normalize  # noqa: E402
from switchboard.application.commands import merge_gate as merge_gate_command  # noqa: E402
from switchboard.domain.completion.state_machine import (  # noqa: E402
    build_completion_snapshot, classify_completion,
)

HEAD = "88624a605727fd44df98191d5b7dd99c73b75d9c"
PR_810 = "https://github.com/6th-Element-Labs/projectplanner/pull/810"
PR_812 = "https://github.com/6th-Element-Labs/projectplanner/pull/812"


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


class MergeGateStatusHydration(unittest.TestCase):
    def test_pull_rest_payload_hydrates_exact_head_commit_statuses(self):
        pull = {
            "number": 840,
            "head": {"sha": HEAD, "ref": "codex/SIMPLIFY-25"},
            "base": {"ref": "master"},
        }
        statuses = {
            "statuses": [{
                "context": "Switchboard CI / VM gate",
                "state": "success",
            }],
        }
        with (
            mock.patch.object(merge_gate_command, "_github_pr",
                              return_value=pull),
            mock.patch.object(merge_gate_command.urllib.request, "urlopen",
                              return_value=_GitHubResponse(statuses)),
        ):
            hydrated, source = merge_gate_command._merge_gate_pr_evidence(
                "", 840, {}, "6th-Element-Labs/projectplanner")

        self.assertEqual(source.get("source"), "github_api")
        self.assertTrue(source.get("hydrated_status_contexts"))
        self.assertEqual(
            hydrated["status_contexts"][0]["context"],
            "Switchboard CI / VM gate",
        )


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


class DuplicateStatusContextHydration(unittest.TestCase):
    """A lossy merge-gate map must not erase richer raw GitHub evidence."""

    CONTEXT = "Switchboard CI / VM gate"
    PR_URL = "https://github.com/6th-Element-Labs/projectplanner/pull/899"
    RICH_FAILURE = {
        "name": CONTEXT,
        "state": "failure",
        "description": "The self-hosted runner did not respond",
        "target_url": "https://github.com/example/actions/runs/123",
    }
    BARE_FAILURE = {"name": CONTEXT, "state": "failure"}

    def _snapshot(self, sources):
        pr = {
            "number": 899,
            "url": self.PR_URL,
            "state": "open",
            "draft": False,
            "mergeable": True,
            "mergeStateStatus": "BLOCKED",
            "head": {"sha": HEAD},
            "status_contexts": sources[0],
            "statusCheckRollup": sources[1],
            "checks": sources[2],
        }
        gate_contexts = sources[3]
        gate = {
            "task_id": "BUG-172",
            "pr_number": 899,
            "pr_url": self.PR_URL,
            "head_sha": HEAD,
            "required_status_contexts": [self.CONTEXT],
            "status_contexts": gate_contexts,
        }
        return build_completion_snapshot(
            task={
                "task_id": "BUG-172",
                "status": "In Review",
                "git_state": {
                    "head_sha": HEAD,
                    "pr_number": 899,
                    "pr_url": self.PR_URL,
                },
            },
            github_pr=pr,
            required_status_contexts=[self.CONTEXT],
            status_contexts=sources[4],
            review={
                "status": "passed",
                "head_sha": HEAD,
                "pr_url": self.PR_URL,
            },
            merge_gate=gate,
        )

    def test_rich_raw_row_wins_across_every_source_order(self):
        # The five positions are the exact build_completion_snapshot sources:
        # three PR fields, merge-gate projection, and the explicit argument.
        for rich_at, bare_at in permutations(range(5), 2):
            with self.subTest(rich_at=rich_at, bare_at=bare_at):
                sources = [None] * 5
                sources[rich_at] = [dict(self.RICH_FAILURE)]
                sources[bare_at] = (
                    {self.CONTEXT: "failure"}
                    if bare_at == 3
                    else [dict(self.BARE_FAILURE)]
                )
                snapshot = self._snapshot(sources)
                selected = snapshot["status_contexts"][self.CONTEXT]
                self.assertEqual(
                    selected["description"],
                    "The self-hosted runner did not respond",
                )
                normalized = normalize.normalize_snapshot(snapshot)
                self.assertEqual(
                    normalized["status_contexts"][self.CONTEXT][
                        "failure_attribution"
                    ],
                    "infrastructure",
                )
                decision = classify_completion(None, normalized)
                self.assertEqual(decision["route"], "coordination_retry")
                self.assertEqual(
                    decision["reason_code"],
                    "required_ci_infrastructure_failure",
                )

    def test_rich_row_wins_both_orders_inside_one_source(self):
        for rows in (
            [self.RICH_FAILURE, self.BARE_FAILURE],
            [self.BARE_FAILURE, self.RICH_FAILURE],
        ):
            with self.subTest(rows=rows):
                snapshot = self._snapshot([None, None, None, None, rows])
                self.assertIn(
                    "description",
                    snapshot["status_contexts"][self.CONTEXT],
                )

    def test_latest_valid_timestamp_precedes_richness(self):
        older_rich = {
            **self.RICH_FAILURE,
            "completedAt": "2026-07-24T01:00:00Z",
        }
        newer_bare = {
            **self.BARE_FAILURE,
            "state": "success",
            "completedAt": "2026-07-24T01:01:00Z",
        }
        snapshot = self._snapshot([
            [older_rich], None, None, None, [newer_bare],
        ])
        self.assertEqual(
            snapshot["status_contexts"][self.CONTEXT]["state"],
            "success",
        )

    def test_equal_timestamp_prefers_richer_provenance(self):
        timestamp = "2026-07-24T01:00:00Z"
        rich = {**self.RICH_FAILURE, "completedAt": timestamp}
        bare = {**self.BARE_FAILURE, "completedAt": timestamp}
        snapshot = self._snapshot([[rich], None, None, None, [bare]])
        self.assertEqual(
            snapshot["status_contexts"][self.CONTEXT]["description"],
            "The self-hosted runner did not respond",
        )

    def test_exact_rank_tie_is_deterministic_not_last_writer_wins(self):
        first = {
            "name": self.CONTEXT,
            "state": "failure",
            "description": "aaa",
        }
        second = {
            "name": self.CONTEXT,
            "state": "failure",
            "description": "zzz",
        }
        forward = self._snapshot([[first, second], None, None, None, None])
        reverse = self._snapshot([[second, first], None, None, None, None])
        self.assertEqual(
            forward["status_contexts"][self.CONTEXT],
            reverse["status_contexts"][self.CONTEXT],
        )


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
                  "git_state": {
                      "head_sha": HEAD, "pr_number": 810, "pr_url": PR_810,
                  }},
            github_pr={"number": 810, "state": "open", "draft": True,
                       "url": PR_810,
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
            review={"status": "passed", "head_sha": HEAD, "pr_url": PR_810},
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
                  "git_state": {
                      "head_sha": HEAD, "pr_number": 812, "pr_url": PR_812,
                  }},
            github_pr={"number": 812, "state": "open", "draft": False,
                       "url": PR_812,
                       "mergeable": True, "mergeStateStatus": "BLOCKED",
                       "head": {"sha": HEAD}},
            required_status_contexts=["Switchboard CI / VM gate"],
            status_contexts=[{"name": "Switchboard CI / VM gate",
                              "state": "success"}],
            review={"status": "changes_requested", "head_sha": HEAD,
                    "pr_url": PR_812,
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


class Pr834RawFixture(unittest.TestCase):
    """BUG-169: current-head repair findings outrank absent CI hydration."""

    HEAD = "ae33752d11db4bf83797070ebf6dbab9b82120be"
    PR_URL = "https://github.com/6th-Element-Labs/projectplanner/pull/834"
    FINDINGS = [
        {
            "class": "auto",
            "id": "S16-CENSUS-2",
            "summary": "Unconditional zero census is not instrumentation",
        },
        {
            "class": "auto",
            "id": "S16-LIVE-3",
            "summary": "A fabricated fixture cannot prove the live run",
        },
        {
            "class": "auto",
            "id": "S16-LIVE-4",
            "summary": "The test only asserts self-authored JSON",
        },
    ]

    def _snapshot(self):
        return normalize.normalize_snapshot(build_completion_snapshot(
            task={
                "task_id": "SIMPLIFY-16",
                "status": "In Review",
                "git_state": {
                    "head_sha": self.HEAD,
                    "pr_number": 834,
                    "pr_url": self.PR_URL,
                },
            },
            github_pr={
                "number": 834,
                "url": self.PR_URL,
                "state": "open",
                "draft": True,
                "mergeable": True,
                "mergeStateStatus": "BLOCKED",
                "head": {"sha": self.HEAD},
            },
            required_status_contexts=[
                "Switchboard CI / VM gate",
                "Switchboard CI / Playwright",
            ],
            # This is the observed hydration defect: GitHub is green, while
            # the completion snapshot has no persisted context rows.
            status_contexts=[],
            review={
                "status": "changes_requested",
                "head_sha": self.HEAD,
                "pr_url": self.PR_URL,
                "findings": self.FINDINGS,
            },
            runner={
                "live": True,
                "role": "review_merge",
                "head_sha": self.HEAD,
                "generation": 4,
            },
        ))

    def test_exact_head_findings_route_remediation_before_ci_hydration(self):
        decision = classify_completion(None, self._snapshot())
        self.assertEqual(decision["route"], "remediation")
        self.assertEqual(decision["reason_code"], "automatic_review_findings")
        self.assertEqual(decision["desired_role"], "remediation")
        self.assertEqual(
            [row["id"] for row in decision["acceptance_findings"]],
            ["S16-CENSUS-2", "S16-LIVE-3", "S16-LIVE-4"],
        )

    def test_planner_fences_review_and_issues_a_new_complete_contract(self):
        snapshot = self._snapshot()
        decision = classify_completion(None, snapshot)
        plan = effects.plan_effect(
            decision,
            snapshot,
            {"run_id": "run-834", "state_version": 4, "attempt": 2},
        )
        self.assertEqual(plan["effect"], "start_remediation")
        self.assertTrue(plan["fence_required"])
        self.assertEqual(plan["fence_generation"], 4)
        self.assertEqual(plan["head_sha"], self.HEAD)
        self.assertEqual(
            [row["id"] for row in plan["acceptance_findings"]],
            ["S16-CENSUS-2", "S16-LIVE-3", "S16-LIVE-4"],
        )

    def test_missing_ci_without_actionable_findings_stays_coordination_retry(self):
        snapshot = self._snapshot()
        snapshot["review"] = {
            "status": "passed",
            "head_sha": self.HEAD,
            "pr_url": self.PR_URL,
        }
        decision = classify_completion(None, snapshot)
        self.assertEqual(decision["route"], "coordination_retry")
        self.assertEqual(decision["reason_code"], "required_ci_hydration_missing")

    def test_terminal_merge_queue_truth_outranks_review_remediation(self):
        snapshot = self._snapshot()
        snapshot["merge_queue"] = {"state": "merged"}
        decision = classify_completion(None, snapshot)
        self.assertEqual(decision["route"], "reconcile")
        self.assertEqual(decision["reason_code"], "merge_queue_merged")


if __name__ == "__main__":
    unittest.main(verbosity=2)
