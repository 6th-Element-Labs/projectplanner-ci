"""COORD-61 — name the missing artifact, don't just refuse (COORD-51 amendment).

COORD-51 §3.3 stopped `_required_ci_decision` from discarding *which* required check
failed. The same discard existed one family over: the merge gate refused with a bare
`missing_executed_test_run` and threw away everything it knew about what it had
required and what it had actually found.

Measured on CO-21 / PR #896, 2026-07-25. An implementation runner executed five real
suites and recorded them under `executed_tests`. The gate reads `executed_test_run` and
correctly refused. The attempt-2 repair dispatch received the bare reason code, wrote no
evidence, orphaned a fresh work session (worksession-7e0e58113497497d), and exited after
~90 seconds. The correct evidence was one key away the whole time, in
worksession-aa0ccd80bb504bbd, and was mechanically derivable at the moment of refusal.

These tests pin: the gate reports the contract it enforced, the near-miss is named with
the session holding it, the report survives to `features_json`, it never crosses the
export boundary, it is bounded, and it changes no route.
"""
from __future__ import annotations

import unittest

from path_setup import ROOT  # noqa: F401

from constants import EXECUTED_TEST_RUN_SCHEMA, MISSING_ARTIFACT_SCHEMA
from switchboard.domain.decisions import features as features_mod
from switchboard.storage.repositories import claims as claims_repo


HEAD = "a" * 40
PR_URL = "https://github.com/acme/private/pull/810"

# The live CO-21 shape: right surface, right content, wrong key.
CO21_SESSION_ID = "worksession-aa0ccd80bb504bbd"
CO21_HYGIENE = {
    "executed_tests": [
        {"commands": ["python test_co_fleet.py"], "passed": True,
         "output_sha256": "0" * 64, "completed_at": 1784995000},
    ],
    "git_diff_check": "clean",
}


# ---------------------------------------------------------------------------
# the gate reports what it enforced
# ---------------------------------------------------------------------------

class MissingArtifactReportTest(unittest.TestCase):
    def test_the_co21_near_miss_is_named_with_the_session_holding_it(self):
        """The whole point: one key away, and nobody was told."""
        gate = claims_repo._executed_test_run_gate(
            {}, {"work_session_id": CO21_SESSION_ID, "hygiene": dict(CO21_HYGIENE)})

        self.assertFalse(gate["ok"])
        self.assertEqual(gate["reason"], "missing_executed_test_run")
        report = gate["missing_artifact"]
        self.assertEqual(report["schema"], MISSING_ARTIFACT_SCHEMA)
        self.assertEqual(report["expected_key"], "executed_test_run")
        self.assertEqual(report["expected_schema"], EXECUTED_TEST_RUN_SCHEMA)
        self.assertEqual(
            report["found_near_miss"],
            [{"key": "executed_tests", "surface": "hygiene.executed_tests",
              "work_session_id": CO21_SESSION_ID}],
        )

    def test_the_report_names_the_hash_keys_and_the_surfaces(self):
        gate = claims_repo._executed_test_run_gate({}, None)
        report = gate["missing_artifact"]
        self.assertIn("output_sha256", report["accepted_hash_keys"])
        self.assertIn("work_session.hygiene", report["read_surfaces"])
        self.assertIn("claim evidence", report["read_surfaces"])

    def test_the_report_is_derived_from_the_keys_the_gate_actually_reads(self):
        """A report built from a copy of the list would drift from the list enforced.

        This is the defect class the task exists to remove, so the advertised contract
        and the enforced contract must be the same object, not two tuples that agree
        today.
        """
        report = claims_repo._executed_test_run_gate({}, None)["missing_artifact"]
        self.assertEqual(
            report["accepted_keys"], list(claims_repo.EXECUTED_TEST_RUN_KEYS))
        self.assertEqual(
            report["accepted_hash_keys"],
            list(claims_repo.EXECUTED_TEST_RUN_HASH_KEYS))
        self.assertIn(report["expected_key"], claims_repo.EXECUTED_TEST_RUN_KEYS)

    def test_an_unrelated_evidence_key_is_not_reported_as_a_near_miss(self):
        # git_diff_check is a different legitimate field. Naming it would send a
        # repair runner after the wrong artifact — worse than saying nothing.
        gate = claims_repo._executed_test_run_gate(
            {}, {"work_session_id": "ws-1",
                 "hygiene": {"git_diff_check": "clean", "repo_preflight": {"ok": True}}})
        self.assertNotIn("found_near_miss", gate["missing_artifact"])

    def test_no_near_miss_is_absent_rather_than_empty(self):
        # "looked and found nothing" must be distinguishable from "never looked".
        gate = claims_repo._executed_test_run_gate({}, None)
        self.assertNotIn("found_near_miss", gate["missing_artifact"])

    def test_a_near_miss_in_claim_evidence_is_found_too(self):
        gate = claims_repo._executed_test_run_gate(
            {"test_suite_results": [{"passed": True}]}, None)
        near = gate["missing_artifact"]["found_near_miss"]
        self.assertEqual([item["key"] for item in near], ["test_suite_results"])
        self.assertEqual(near[0]["surface"], "evidence.test_suite_results")

    def test_an_empty_stray_key_is_not_a_near_miss(self):
        gate = claims_repo._executed_test_run_gate({"executed_tests": []}, None)
        self.assertNotIn("found_near_miss", gate["missing_artifact"])

    def test_the_near_miss_list_is_bounded(self):
        evidence = {f"test_run_{index}_results": [{"ok": True}] for index in range(40)}
        gate = claims_repo._executed_test_run_gate(evidence, None)
        self.assertEqual(
            len(gate["missing_artifact"]["found_near_miss"]),
            claims_repo.MAX_NEAR_MISS_KEYS,
        )

    def test_a_valid_run_still_passes_and_reports_nothing(self):
        gate = claims_repo._executed_test_run_gate(
            {"executed_test_run": {"schema": EXECUTED_TEST_RUN_SCHEMA,
                                   "commands": ["pytest"], "passed": True,
                                   "output_sha256": "0" * 64,
                                   "completed_at": 1784995000}},
            None)
        self.assertTrue(gate["ok"])
        self.assertNotIn("missing_artifact", gate)


# (retired with SIMPLIFY-30) The v1 classifier/dossier lift and the decision-
# corpus write path that carried this report were deleted with the Mission Bot
# v1 controller. The live contract is the gate report above and the bounded
# feature projection below.


class MissingArtifactProjectionBoundsTest(unittest.TestCase):
    def test_the_projection_bounds_every_list_and_string(self):
        oversized = {
            "schema": MISSING_ARTIFACT_SCHEMA,
            "expected_key": "k" * 500,
            "accepted_keys": [f"key-{index}" for index in range(100)],
            "accepted_hash_keys": [f"hash-{index}" for index in range(100)],
            "read_surfaces": [f"surface-{index}" for index in range(100)],
            "found_near_miss": [
                {"key": f"stray-{index}", "surface": f"hygiene.stray-{index}",
                 "work_session_id": "ws-" + "x" * 500}
                for index in range(50)
            ],
        }
        projected = features_mod.project_diagnostics(
            {}, {"missing_artifact": oversized})["missing_artifact"]

        self.assertEqual(len(projected["expected_key"]), features_mod.MAX_KEY_CHARS)
        for name in ("accepted_keys", "accepted_hash_keys", "read_surfaces"):
            self.assertEqual(len(projected[name]), features_mod.MAX_ACCEPTED_KEYS)
        self.assertEqual(
            len(projected["found_near_miss"]), features_mod.MAX_NEAR_MISS)
        self.assertEqual(
            len(projected["found_near_miss"][0]["work_session_id"]),
            features_mod.MAX_KEY_CHARS,
        )

    def test_a_missing_report_projects_nothing(self):
        self.assertEqual(features_mod.project_diagnostics({}, {}), {})
        self.assertEqual(
            features_mod.project_diagnostics({}, {"missing_artifact": {}}), {})

    def test_a_malformed_report_is_dropped_rather_than_half_written(self):
        for junk in ("a string", 17, [1, 2, 3], None):
            self.assertEqual(
                features_mod.project_diagnostics({}, {"missing_artifact": junk}), {})


if __name__ == "__main__":
    unittest.main()
