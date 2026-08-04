"""DOCS-8: fail-closed checks for the frozen Compand Phase 2 CES-1 contract."""

from __future__ import annotations

import copy
import json
import unittest

import yaml
from jsonschema import Draft202012Validator

from path_setup import ROOT

CONTRACT = ROOT / "docs" / "compand" / "phase2"


def load_json(name: str) -> dict:
    return json.loads((CONTRACT / name).read_text(encoding="utf-8"))


class Docs8CompandPhase2ContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.benchmark = yaml.safe_load(
            (CONTRACT / "benchmark.yaml").read_text(encoding="utf-8")
        )
        cls.catalog = load_json("technique-catalog.json")
        cls.corpus = load_json("corpus-manifest.json")
        cls.system = load_json("system-card.json")
        cls.scorecard = load_json("public-scorecard.schema.json")

    def valid_public_scorecard(self) -> dict:
        effect = {
            "estimate": 0.0,
            "lower_95": 0.0,
            "upper_95": 0.0,
            "unit": "ratio",
            "numerator": 1,
            "denominator": 1,
        }
        gate = {"passed": True, "evidence_ids": ["evidence-1"], "reason": None}
        grade = {"score": 85, "band": "A", "components": {"criterion": 85}}
        kpi = {
            "value": 1.0,
            "unit": "ratio",
            "evidence_state": "verified",
            "board_kpi_id": "switchboard-kpi-1",
            "movement_allowed": True,
        }
        return {
            "schema": "compand.ces1.public_scorecard.v1",
            "release_id": "ces1-release-1",
            "technique": {"id": "line-rle-v1", "version": "1", "arm": "E1"},
            "certification_tuple": {
                "agent": "codex",
                "dialect": "responses-v1",
                "model_provider_snapshot": "provider-snapshot-1",
                "workload_revision": "workload-1",
                "ces_release": "CES-1",
            },
            "evidence": {
                "tier": "C2",
                "state": "verified",
                "value_states": ["verified"],
                "claim": "Named release result only.",
            },
            "sample": {
                "task_pairs": 30,
                "independent_repetitions": 2,
                "failures": 0,
                "invalid_attempts": 0,
                "exclusions": 0,
                "missing": 0,
            },
            "effects": {
                name: copy.deepcopy(effect)
                for name in (
                    "net_cost_per_verified_task",
                    "verified_task_success",
                    "latency_p95",
                    "reliability",
                    "exact_recovery",
                )
            },
            "hard_gates": {
                name: copy.deepcopy(gate)
                for name in (
                    "correctness",
                    "attribution",
                    "isolation",
                    "reproducibility",
                    "protocol_safety",
                    "exact_recovery",
                    "fail_open",
                    "whole_task_economics",
                    "quality_noninferiority",
                    "clean_environment_regeneration",
                )
            },
            "grades": {
                "technical": copy.deepcopy(grade),
                "user_value": copy.deepcopy(grade),
                "company_value": copy.deepcopy(grade),
                "asset_value": copy.deepcopy(grade),
                "hard_gate_grade": "pass",
            },
            "kpis": {
                name: copy.deepcopy(kpi)
                for name in self.scorecard["properties"]["kpis"]["required"]
            },
            "disposition": "promote_cloud_canary",
            "trace": {
                "benchmark_sha256": "a" * 64,
                "catalog_sha256": "b" * 64,
                "corpus_sha256": "c" * 64,
                "system_card_sha256": "d" * 64,
                "run_ids": ["run-1"],
                "event_root_sha256": "e" * 64,
                "scorecard_sha256": "f" * 64,
            },
        }

    def test_named_contract_files_exist(self) -> None:
        for name in (
            "benchmark.yaml",
            "BENCHMARK-CARD.md",
            "technique-catalog.json",
            "corpus-manifest.json",
            "system-card.json",
            "public-scorecard.schema.json",
        ):
            self.assertTrue((CONTRACT / name).is_file(), name)

    def test_arms_and_confirmatory_gate_are_frozen(self) -> None:
        self.assertEqual(set(self.benchmark["arms"]), {"B0", "S1", "E1", "C1"})
        self.assertEqual(self.benchmark["arms"]["B0"]["mutation"], "none")
        self.assertEqual(self.benchmark["arms"]["S1"]["mutation"], "none")
        self.assertEqual(self.benchmark["arms"]["E1"]["active_technique_count"], 1)
        self.assertIn("passing_frozen_E1", self.benchmark["arms"]["C1"]["prerequisite"])
        self.assertFalse(self.benchmark["scope"]["confirmatory_traffic_allowed"])
        self.assertTrue(self.benchmark["scope"]["confirmatory_blockers"])

    def test_every_researched_technique_has_the_complete_inventory_contract(self) -> None:
        required = {
            "id",
            "version",
            "name",
            "lineage",
            "prior_art",
            "cloud_eligibility",
            "cloud_gateway_enforceable",
            "guarantee",
            "transform_oracle",
            "cache_interaction",
            "recovery_contract",
            "unsupported_reason",
            "host_dependency",
            "source_refs",
        }
        techniques = self.catalog["techniques"]
        ids = [item["id"] for item in techniques]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 30)
        self.assertEqual(
            {
                "line-rle-v1",
                "exact-duplicate-reference-v1",
                "subresult-chunk-dedup-v1",
                "structured-data-codec-v1",
                "command-aware-projection-v1",
                "delta-reread-v1",
                "prefix-cache-shaping-v1",
                "context-paging-v1",
                "schema-deferral-v1",
                "turn-elimination-v1",
                "semantic-cache-v1",
                "injected-text-hard-compression-v1",
                "provider-kv-reuse-v1",
                "speculative-decoding-v1",
                "transport-gzip-v1",
            }
            - set(ids),
            set(),
        )
        for item in techniques:
            self.assertEqual(required - set(item), set(), item["id"])
            self.assertTrue(item["lineage"], item["id"])
            self.assertTrue(item["prior_art"], item["id"])
            self.assertTrue(item["transform_oracle"], item["id"])
            self.assertTrue(item["cache_interaction"], item["id"])
            self.assertTrue(item["recovery_contract"], item["id"])
            self.assertIn(
                item["guarantee"],
                {"exact", "recoverable", "semantic", "provider_native"},
                item["id"],
            )
            if item["cloud_gateway_enforceable"]:
                self.assertIn(
                    item["cloud_eligibility"],
                    {"eligible", "eligible_with_cloud_recovery", "eligible_with_cloud_identity"},
                    item["id"],
                )
            else:
                self.assertIsNotNone(item["unsupported_reason"], item["id"])

    def test_hard_gates_and_grade_weights_cannot_drift(self) -> None:
        expected_gates = {
            "correctness",
            "attribution",
            "isolation",
            "reproducibility",
            "protocol_safety",
            "exact_recovery",
            "fail_open",
            "whole_task_economics",
            "quality_noninferiority",
            "clean_environment_regeneration",
        }
        self.assertEqual(
            {gate["id"] for gate in self.benchmark["hard_gates"]}, expected_gates
        )
        for grade_name in ("technical", "user_value", "company_value", "asset_value"):
            self.assertEqual(sum(self.benchmark["grades"][grade_name].values()), 100)
        self.assertEqual(self.benchmark["grades"]["hard_gate_failure_grade"], "F")
        self.assertFalse(
            self.benchmark["grades"]["override_rules"][
                "company_or_asset_may_override_safety"
            ]
        )

    def test_statistics_fail_closed_without_phase1_variance(self) -> None:
        sample = self.benchmark["sample_size"]
        self.assertFalse(sample["phase1_input"]["usable_task_level_variance"])
        self.assertEqual(sample["confirmatory_formula"]["minimum_task_pairs"], 30)
        self.assertEqual(sample["confirmatory_formula"]["maximum_task_pairs"], 200)
        self.assertEqual(
            self.benchmark["hypotheses"]["confirmatory"][1]["margin_absolute"], -0.05
        )
        self.assertIn(
            "Holm_Bonferroni",
            self.benchmark["analysis"]["multiplicity"]["confirmatory_economic_family"],
        )

    def test_six_kpis_match_the_public_schema_and_block_unbound_movement(self) -> None:
        expected = {
            "compand.p2.net_cost_per_verified_task_usd",
            "compand.p2.natural_eligible_spend_coverage_ratio",
            "compand.p2.task_outcome_noninferiority_rate",
            "compand.p2.gateway_added_latency_p95_ms",
            "compand.p2.reliable_request_rate",
            "compand.p2.exact_recovery_success_rate",
        }
        outputs = self.benchmark["kpis"]["outputs"]
        self.assertEqual({item["id"] for item in outputs}, expected)
        self.assertTrue(all(item["board_kpi_id"] is None for item in outputs))
        self.assertTrue(
            self.benchmark["kpis"]["missing_board_ids_block_value_index_movement"]
        )
        public_required = set(
            self.scorecard["properties"]["kpis"]["required"]
        )
        self.assertEqual(public_required, expected)

    def test_corpus_and_system_cards_do_not_pretend_to_be_ready(self) -> None:
        self.assertFalse(self.corpus["materialization"]["confirmatory_ready"])
        self.assertIsNone(self.corpus["materialization"]["corpus_root_sha256"])
        self.assertAlmostEqual(
            sum(part["target_fraction"] for part in self.corpus["partitions"]), 1.0
        )
        self.assertEqual(
            {part["id"] for part in self.corpus["partitions"]},
            {"development", "golden", "hidden_holdout"},
        )
        self.assertFalse(self.system["confirmatory_snapshot"]["confirmatory_ready"])
        self.assertTrue(
            any(
                value is None
                for key, value in self.system["confirmatory_snapshot"].items()
                if key not in {"confirmatory_ready", "freeze_rule"}
            )
        )

    def test_public_scorecard_separates_evidence_and_value_states(self) -> None:
        evidence = self.scorecard["properties"]["evidence"]["properties"]
        self.assertEqual(
            evidence["state"]["enum"],
            [
                "exploratory",
                "provisional",
                "verified",
                "independently_reproduced",
                "suspended",
            ],
        )
        self.assertEqual(
            evidence["value_states"]["items"]["enum"],
            ["projected", "measured", "verified", "market_validated", "ip_supported"],
        )

    def test_public_scorecard_rejects_unverified_or_unbound_kpi_movement(self) -> None:
        Draft202012Validator.check_schema(self.scorecard)
        validator = Draft202012Validator(self.scorecard)
        valid = self.valid_public_scorecard()
        self.assertEqual(list(validator.iter_errors(valid)), [])

        kpi_id = "compand.p2.net_cost_per_verified_task_usd"
        not_moving = copy.deepcopy(valid)
        not_moving["kpis"][kpi_id].update(
            evidence_state="projected", board_kpi_id=None, movement_allowed=False
        )
        self.assertEqual(list(validator.iter_errors(not_moving)), [])

        for evidence_state, board_kpi_id in (
            ("projected", "switchboard-kpi-1"),
            ("measured", "switchboard-kpi-1"),
            ("verified", None),
            ("verified", ""),
        ):
            with self.subTest(
                evidence_state=evidence_state, board_kpi_id=board_kpi_id
            ):
                invalid = copy.deepcopy(valid)
                invalid["kpis"][kpi_id]["evidence_state"] = evidence_state
                invalid["kpis"][kpi_id]["board_kpi_id"] = board_kpi_id
                self.assertTrue(list(validator.iter_errors(invalid)))

    def test_public_scorecard_rejects_empty_passing_evidence_and_grades(self) -> None:
        validator = Draft202012Validator(self.scorecard)
        valid = self.valid_public_scorecard()

        for evidence_ids in ([], [""]):
            with self.subTest(evidence_ids=evidence_ids):
                invalid = copy.deepcopy(valid)
                invalid["hard_gates"]["correctness"]["evidence_ids"] = evidence_ids
                self.assertTrue(list(validator.iter_errors(invalid)))

        invalid = copy.deepcopy(valid)
        invalid["grades"]["technical"]["components"] = {}
        self.assertTrue(list(validator.iter_errors(invalid)))

    def test_scorecard_hash_rule_excludes_its_own_member(self) -> None:
        rule = self.benchmark["publication"]["scorecard_sha256"]
        self.assertEqual(rule["algorithm"], "sha256")
        self.assertEqual(
            rule["canonicalization"], "RFC_8785_JSON_Canonicalization_Scheme"
        )
        self.assertEqual(
            rule["excluded_json_pointer"], "/trace/scorecard_sha256"
        )
        comment = self.scorecard["properties"]["trace"]["properties"][
            "scorecard_sha256"
        ]["$comment"]
        self.assertIn("RFC 8785", comment)
        self.assertIn("removing /trace/scorecard_sha256", comment)


if __name__ == "__main__":
    unittest.main()
