from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from path_setup import ROOT  # noqa: F401

from switchboard.application.completion_shadow import (
    compare_shadow_actions,
    explain_fresh_snapshot,
)
from switchboard.application.scar_promotion import promote, scar_kind


class CompletionShadowTest(unittest.TestCase):
    def snapshot(self):
        return {
            "task_id": "COORD-87",
            "snapshot_id": "fresh-1",
            "head_sha": "a" * 40,
            "observed_at": 120.0,
            "source_observed_at": {"github_pr": 119.0, "merge_gate": 118.0},
        }

    def normalized(self, action="START"):
        return {
            "action": action,
            "controller_build_sha": "new-build",
            "table_version": "1",
        }

    def test_same_snapshot_match_records_required_identity_and_metrics(self):
        row = compare_shadow_actions(
            task_id="COORD-87",
            snapshot=self.snapshot(),
            decision={"route": "review_merge"},
            legacy_plan={"effect": "ensure_review_generation"},
            normalized=self.normalized(),
            legacy_build_sha="old-build",
            now=121.0,
            fix_landed_at=100.0,
            deployed_at=110.0,
        )
        self.assertEqual(row["divergence_class"], "match")
        self.assertEqual(row["legacy_controller_build_sha"], "old-build")
        self.assertEqual(row["new_controller_build_sha"], "new-build")
        self.assertEqual(row["table_version"], "1")
        self.assertEqual(row["evidence_age_seconds"], 3.0)
        self.assertEqual(
            row["metrics"]["fix_landing_to_deployment_seconds"], 10.0)
        self.assertEqual(
            row["metrics"]["deployment_to_first_fresh_tick_seconds"], 10.0)
        self.assertFalse(row["shadow_is_lifecycle_authority"])

    def test_fail_closed_and_unsafe_divergences_are_distinct(self):
        safe = compare_shadow_actions(
            task_id="COORD-87", snapshot=self.snapshot(),
            decision={"route": "coordination_retry"},
            legacy_plan={"effect": "repair_dispatch"},
            normalized=self.normalized("BLOCK"), now=121.0,
        )
        unsafe = compare_shadow_actions(
            task_id="COORD-87", snapshot=self.snapshot(),
            decision={"route": "review_merge"},
            legacy_plan={"effect": "enqueue"},
            normalized=self.normalized("START"), now=121.0,
        )
        self.assertEqual(safe["divergence_class"], "safe_fail_closed")
        self.assertEqual(unsafe["divergence_class"], "unsafe")
        self.assertTrue(unsafe["scar_candidate"])
        self.assertEqual(scar_kind(unsafe), "unsafe_shadow_divergence")

    def test_unsafe_divergence_enters_existing_scar_promoter(self):
        snapshot = {
            **self.snapshot(),
            "schema": "switchboard.completion_snapshot.v1",
            "pr_number": 994,
            "github_pr": {
                "number": 994,
                "state": "OPEN",
                "draft": True,
                "mergeable": True,
                "mergeStateStatus": "CLEAN",
                "head": {"sha": "a" * 40},
            },
            "required_status_contexts": ["Switchboard CI / VM gate"],
            "status_contexts": [{
                "context": "Switchboard CI / VM gate",
                "state": "SUCCESS",
                "failure_attribution": "product",
            }],
            "review": {"status": "passed", "head_sha": "a" * 40},
            "merge_gate": {"findings": []},
            "merge_queue": {},
            "runner": {"live": False},
        }
        unsafe = compare_shadow_actions(
            task_id="COORD-87",
            snapshot=snapshot,
            decision={"route": "review_merge"},
            legacy_plan={"effect": "ensure_review_generation"},
            normalized=self.normalized("ARM_MERGE"),
            now=121.0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            manifest = promote([unsafe], Path(tmp))
            self.assertEqual(manifest["skipped"], [])
            self.assertEqual(len(manifest["written"]), 1)
            scenario = json.loads(Path(manifest["written"][0]).read_text())
        self.assertEqual(
            scenario["expect"]["shadow_divergence_class"], "unsafe")
        self.assertEqual(
            scenario["expect"]["normalized_action"], "ARM_MERGE")

    def test_explanation_is_fresh_bounded_and_cache_independent(self):
        snapshot = self.snapshot()
        normalized = {
            **self.normalized("BLOCK"),
            "reason_code": "live_gate_failed",
            "owner": "Task Execution",
            "block": {
                "owner": "Task Execution",
                "live_evidence": [f"source-{i}" for i in range(20)],
                "minimum_repair": "repair the live gate",
            },
        }
        result = explain_fresh_snapshot(snapshot, normalized)
        self.assertTrue(result["blocked"])
        self.assertFalse(result["derived_from_cached_verdict"])
        self.assertEqual(len(result["live_evidence"]), 8)
        self.assertLess(len(json.dumps(result)), 2_000)


if __name__ == "__main__":
    unittest.main()
