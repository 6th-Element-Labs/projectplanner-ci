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

import json
import sqlite3
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from constants import EXECUTED_TEST_RUN_SCHEMA, MISSING_ARTIFACT_SCHEMA
from switchboard.domain.completion import state_machine
from switchboard.domain.decisions import features as features_mod
from switchboard.storage.migrations import runner as migrations
from switchboard.storage.repositories import claims as claims_repo
from switchboard.storage.repositories import completion_runs, decision_records


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


def _gate_finding(missing_artifact: dict | None = None,
                  code: str = "missing_executed_test_run") -> dict:
    """One merge_gate finding shaped exactly as the gate emits it."""
    gate = {
        "ok": False,
        "schema": EXECUTED_TEST_RUN_SCHEMA,
        "reason": code,
        "problems": [],
    }
    if missing_artifact is not None:
        gate["missing_artifact"] = missing_artifact
    return {
        "code": code,
        "message": "Merge gate requires a passing executed test run.",
        "failure_class": "missing_data",
        "blocking": True,
        "details": {"executed_test_gate": gate, "policy_profile": "code_strict"},
    }


def _snapshot(findings, *, head=HEAD):
    return state_machine.build_completion_snapshot(
        task={"task_id": "CO-21", "status": "In Review",
              "git_state": {"head_sha": head, "pr_number": 810, "pr_url": PR_URL}},
        github_pr={"number": 810, "state": "OPEN", "draft": False, "mergeable": True,
                   "mergeStateStatus": "CLEAN", "head": {"sha": head},
                   "status_contexts": [{"context": "Switchboard CI / VM gate",
                                        "state": "SUCCESS"}],
                   "url": PR_URL},
        required_status_contexts=["Switchboard CI / VM gate"],
        review={"status": "passed", "head_sha": head, "pr_url": PR_URL},
        merge_gate={"findings": findings, "pr_url": PR_URL},
        runner={"live": False, "generation": 1, "host_id": "host-1"},
    )


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


# ---------------------------------------------------------------------------
# the classifier stops discarding it
# ---------------------------------------------------------------------------

class ClassifierCarriesMissingArtifactTest(unittest.TestCase):
    def _report(self):
        return claims_repo._executed_test_run_gate(
            {}, {"work_session_id": CO21_SESSION_ID,
                 "hygiene": dict(CO21_HYGIENE)})["missing_artifact"]

    def test_the_decision_carries_the_report(self):
        decision = state_machine.classify_completion(
            None, _snapshot([_gate_finding(self._report())]))
        self.assertEqual(decision["reason_code"], "missing_executed_test_run")
        self.assertEqual(
            decision["missing_artifact"]["found_near_miss"][0]["work_session_id"],
            CO21_SESSION_ID,
        )

    def test_carrying_the_report_changes_no_route(self):
        """Feature-only, same contract as COORD-51 §3.3."""
        routing_keys = ("state", "route", "reason_code", "desired_role",
                        "retry_policy", "board_projection", "effect")
        for code in ("missing_executed_test_run", "invalid_executed_test_run",
                     "missing_ui_playwright_evidence", "work_session_required"):
            bare = state_machine.classify_completion(
                None, _snapshot([_gate_finding(None, code=code)])) or {}
            rich = state_machine.classify_completion(
                None, _snapshot([_gate_finding(self._report(), code=code)])) or {}
            self.assertEqual(
                {key: bare.get(key) for key in routing_keys},
                {key: rich.get(key) for key in routing_keys},
                f"the missing-artifact report moved the route for {code}",
            )

    def test_a_finding_without_a_report_adds_no_key(self):
        decision = state_machine.classify_completion(
            None, _snapshot([_gate_finding(None)]))
        self.assertNotIn("missing_artifact", decision)

    def test_a_non_blocking_finding_is_not_mined_for_a_report(self):
        finding = _gate_finding(self._report())
        finding["blocking"] = False
        decision = state_machine.classify_completion(None, _snapshot([finding])) or {}
        self.assertNotIn("missing_artifact", decision)


# ---------------------------------------------------------------------------
# it reaches the corpus, and stops at the export boundary
# ---------------------------------------------------------------------------

class MissingArtifactInTheCorpusTest(unittest.TestCase):
    project = "switchboard"

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE deliverable_task_links ("
            "id TEXT PRIMARY KEY, deliverable_id TEXT NOT NULL, "
            "project_id TEXT NOT NULL, task_id TEXT NOT NULL, created_at REAL)")
        for name, sql in migrations.DDL_MIGRATIONS:
            if name in {"0117_decision_records",
                        "0118_ix_decision_records_projection",
                        "0119_ix_decision_records_convergence"}:
                self.db.execute(sql)
        self.patches = [
            patch.object(decision_records, "_conn", return_value=self.db),
            patch.object(decision_records, "_write_through",
                         side_effect=lambda _project, fn: fn()),
            patch.object(completion_runs, "_conn", return_value=self.db),
            patch.object(completion_runs, "_write_through",
                         side_effect=lambda _project, fn: fn()),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.db.close()

    def _co21_episode(self):
        report = claims_repo._executed_test_run_gate(
            {}, {"work_session_id": CO21_SESSION_ID,
                 "hygiene": dict(CO21_HYGIENE)})["missing_artifact"]
        snapshot = _snapshot([_gate_finding(report)])
        decision = state_machine.classify_completion(None, snapshot)
        return decision_records.record_decision_episode(
            project=self.project, snapshot=snapshot, decision=decision,
            classifier_version=state_machine.COMPLETION_CLASSIFIER_VERSION,
            now=1_700_000_000.0,
        )

    def test_the_co21_episode_names_the_contract_and_the_near_miss(self):
        """The amendment's acceptance criterion, end to end."""
        self._co21_episode()
        episode = decision_records.list_decision_episodes(
            project=self.project, task_id="CO-21")[0]

        self.assertEqual(episode["reason_code"], "missing_executed_test_run")
        artifact = episode["features"]["missing_artifact"]
        self.assertEqual(artifact["expected_key"], "executed_test_run")
        self.assertEqual(artifact["expected_schema"], EXECUTED_TEST_RUN_SCHEMA)
        self.assertIn("output_sha256", artifact["accepted_hash_keys"])
        self.assertEqual(
            artifact["found_near_miss"],
            [{"key": "executed_tests", "surface": "hygiene.executed_tests",
              "work_session_id": CO21_SESSION_ID}],
        )

    def test_the_poolable_allowlist_is_untouched(self):
        self._co21_episode()
        episode = decision_records.list_decision_episodes(
            project=self.project, task_id="CO-21")[0]
        self.assertEqual(
            set(features_mod.FEATURE_FIELDS) - set(episode["features"]), set())

    def test_the_report_never_crosses_the_export_boundary(self):
        """Work-session ids and internal key names are environment identity."""
        self._co21_episode()
        exported = decision_records.export_projection(project=self.project)
        raw = json.dumps(exported[0])
        for leak in (CO21_SESSION_ID, "executed_tests", "missing_artifact",
                     "hygiene.executed_tests"):
            self.assertNotIn(leak, raw, f"{leak!r} crossed the export boundary")
        features = json.loads(exported[0]["features_json"])
        self.assertEqual(set(features), set(features_mod.FEATURE_FIELDS))

    def test_missing_artifact_is_a_declared_diagnostic_field(self):
        self.assertIn("missing_artifact", features_mod.DIAGNOSTIC_FIELDS)
        self.assertNotIn("missing_artifact", features_mod.FEATURE_FIELDS)

    def test_the_report_does_not_fragment_an_episode(self):
        # Two ticks whose reports differ only in a stray key must stay one episode;
        # the diagnostic is not part of episode identity.
        first = self._co21_episode()
        report = claims_repo._executed_test_run_gate(
            {"other_test_results": [{"ok": True}]},
            {"work_session_id": CO21_SESSION_ID,
             "hygiene": dict(CO21_HYGIENE)})["missing_artifact"]
        snapshot = _snapshot([_gate_finding(report)])
        second = decision_records.record_decision_episode(
            project=self.project, snapshot=snapshot,
            decision=state_machine.classify_completion(None, snapshot),
            classifier_version=state_machine.COMPLETION_CLASSIFIER_VERSION,
            now=1_700_000_001.0,
        )
        self.assertEqual(first["record_id"], second["record_id"])
        self.assertEqual(second["tick_count"], 2)


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
