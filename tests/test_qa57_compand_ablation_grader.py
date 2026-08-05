"""QA-57: deterministic ablation, mechanical grading, and evidence compiler."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from path_setup import ROOT
from switchboard.application.commands.compand_ablation import (
    MechanicalGrader,
    build_ablation_plan,
    make_score_input_event,
    public_scorecard,
    validate_frozen_lab_contract,
)
from switchboard.domain.compand.grading import (
    AblationArm,
    GRADE_WEIGHTS,
    HARD_GATE_IDS,
    jcs_canonical_json_bytes,
    scorecard_sha256,
)
from switchboard.storage.compand_evidence import CesEvidenceReleaseStore


CONTRACT = ROOT / "docs" / "compand" / "phase2"
CORPUS = ROOT / "fixtures" / "compand" / "phase2-technique-corpus" / "v1"


def components(value: float = 1.0) -> dict[str, dict[str, float]]:
    return {
        grade: {name: value for name in weights}
        for grade, weights in GRADE_WEIGHTS.items()
    }


def score_event(
    *,
    run_id: str,
    pair_id: str,
    arm: AblationArm,
    provider_cost: float,
    overhead: float,
    technique_id: str = "line-rle-v1",
    success: bool = True,
    latency_ms: float = 100.0,
) -> dict:
    return make_score_input_event(
        run_id=run_id,
        arm=arm,
        technique_id=technique_id,
        technique_version="1.0.0",
        candidate_id=f"candidate-{run_id}",
        input_hash="sha256:" + "a" * 64,
        output_hash="sha256:" + ("a" if arm is AblationArm.BASELINE else "b") * 64,
        parent_event_id=None,
        config_fingerprint="sha256:" + "c" * 64,
        sequence=1,
        provider_usage={
            "input_tokens": 100,
            "cached_input_tokens": None,
            "cache_write_tokens": None,
            "output_tokens": 10,
            "reasoning_tokens": None,
            "provider_charge_usd": provider_cost,
            "compand_overhead_usd": overhead,
        },
        evaluator_version="fixture-evaluator-v1",
        task={
            "task_id": pair_id,
            "pair_id": pair_id,
            "repetition": 1,
            "verified_completed": True,
            "verified_task_success": success,
            "latency_ms": latency_ms,
            "reliable": True,
            "exact_recovery": True,
            "infrastructure_invalid": False,
        },
        hard_gates={name: True for name in HARD_GATE_IDS},
        grade_components=components(),
    )


class Qa57CompandAblationGraderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = validate_frozen_lab_contract(
            contract_root=CONTRACT, corpus_root=CORPUS
        )

    def test_frozen_manifest_is_validated_before_deterministic_arm_planning(
        self,
    ) -> None:
        first = build_ablation_plan(
            self.contract,
            technique_ids=["line-rle-v1", "json-minify-v1"],
            repetitions=2,
        )
        second = build_ablation_plan(
            self.contract,
            technique_ids=["line-rle-v1", "json-minify-v1"],
            repetitions=2,
        )
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertEqual(
            {entry.arm for entry in first},
            {AblationArm.BASELINE, AblationArm.SHADOW, AblationArm.ENFORCED},
        )
        self.assertTrue(all(len(entry.technique_ids) == 1 for entry in first))
        self.assertEqual(len({entry.plan_id for entry in first}), len(first))

        with self.assertRaisesRegex(ValueError, "passing frozen E1"):
            build_ablation_plan(
                self.contract,
                technique_ids=["line-rle-v1", "json-minify-v1"],
                combinations=[["line-rle-v1", "json-minify-v1"]],
            )
        scorecards = []
        for technique_id in ("line-rle-v1", "json-minify-v1"):
            grade = MechanicalGrader().grade(
                [
                    score_event(
                        run_id=f"run-{technique_id}-b0",
                        pair_id=f"pair-{technique_id}",
                        arm=AblationArm.BASELINE,
                        provider_cost=1.0,
                        overhead=0.0,
                        technique_id=technique_id,
                    ),
                    score_event(
                        run_id=f"run-{technique_id}-e1",
                        pair_id=f"pair-{technique_id}",
                        arm=AblationArm.ENFORCED,
                        provider_cost=0.5,
                        overhead=0.1,
                        technique_id=technique_id,
                    ),
                ],
                technique_id=technique_id,
                technique_version="1.0.0",
            )
            scorecards.append(
                public_scorecard(
                    grade,
                    release_id=f"passing-{technique_id}",
                    certification_tuple={
                        "agent": "fixture-agent",
                        "dialect": "fixture-dialect",
                        "model_provider_snapshot": "fixture-provider",
                        "workload_revision": "phase2-corpus-v1",
                        "ces_release": "CES-1",
                    },
                    contract=self.contract,
                    claim="Passing frozen E1 fixture evidence.",
                    clean_environment_regenerated=True,
                )
            )
        combined = build_ablation_plan(
            self.contract,
            technique_ids=["line-rle-v1", "json-minify-v1"],
            combinations=[["line-rle-v1", "json-minify-v1"]],
            passing_e1=scorecards,
        )
        self.assertTrue(any(entry.arm is AblationArm.COMBINATION for entry in combined))

        tampered = json.loads(json.dumps(scorecards))
        tampered[0]["technique"]["version"] = "forged-version"
        with self.assertRaisesRegex(ValueError, "version drifted"):
            build_ablation_plan(
                self.contract,
                technique_ids=["line-rle-v1", "json-minify-v1"],
                combinations=[["line-rle-v1", "json-minify-v1"]],
                passing_e1=tampered,
            )

    def test_manifest_tampering_fails_closed_before_any_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qa57-tamper-") as temp:
            copied = Path(temp) / "corpus"
            shutil.copytree(CORPUS, copied)
            fixture = next((copied / "visible" / "development").glob("*.json"))
            fixture.write_bytes(fixture.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                validate_frozen_lab_contract(contract_root=CONTRACT, corpus_root=copied)

    def test_score_inputs_require_full_trace_provider_usage_and_evaluator(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider usage fields missing"):
            make_score_input_event(
                run_id="run-bad",
                arm=AblationArm.ENFORCED,
                technique_id="line-rle-v1",
                technique_version="1.0.0",
                candidate_id="candidate-bad",
                input_hash="sha256:" + "a" * 64,
                output_hash="sha256:" + "b" * 64,
                parent_event_id=None,
                config_fingerprint="sha256:" + "c" * 64,
                sequence=1,
                provider_usage={"input_tokens": 1},
                evaluator_version="fixture-evaluator-v1",
                task={"pair_id": "pair-bad"},
                hard_gates={name: True for name in HARD_GATE_IDS},
                grade_components=components(),
            )

    def test_scorecard_hash_uses_rfc8785_number_and_utf16_key_canonicalization(
        self,
    ) -> None:
        value = {
            "numbers": [333333333.33333329, 1e30, 4.50, 1.0, 2e-3, 1e-27],
            "\U0001f600": "astral sorts by UTF-16",
            "\u20ac": "euro",
        }
        self.assertEqual(
            jcs_canonical_json_bytes(value),
            (
                '{"numbers":[333333333.3333333,1e+30,4.5,1,0.002,1e-27],'
                '"€":"euro","😀":"astral sorts by UTF-16"}'
            ).encode("utf-8"),
        )

    def test_hard_gates_precede_four_mechanical_grades_and_whole_task_economics(
        self,
    ) -> None:
        events = [
            score_event(
                run_id="run-b0-a",
                pair_id="pair-a",
                arm=AblationArm.BASELINE,
                provider_cost=1.00,
                overhead=0.00,
            ),
            score_event(
                run_id="run-e1-a",
                pair_id="pair-a",
                arm=AblationArm.ENFORCED,
                provider_cost=0.70,
                overhead=0.05,
            ),
            score_event(
                run_id="run-b0-b",
                pair_id="pair-b",
                arm=AblationArm.BASELINE,
                provider_cost=2.00,
                overhead=0.00,
            ),
            score_event(
                run_id="run-e1-b",
                pair_id="pair-b",
                arm=AblationArm.ENFORCED,
                provider_cost=1.50,
                overhead=0.10,
            ),
        ]
        grade = MechanicalGrader().grade(
            events, technique_id="line-rle-v1", technique_version="1.0.0"
        )
        self.assertTrue(grade["all_hard_gates_passed"])
        self.assertEqual(grade["grades"]["hard_gate_grade"], "pass")
        self.assertEqual(set(grade["grades"]) - {"hard_gate_grade"}, set(GRADE_WEIGHTS))
        self.assertLess(grade["effects"]["net_cost_per_verified_task"]["estimate"], 0)
        self.assertEqual(grade["sample"]["task_pairs"], 2)
        self.assertIsNotNone(grade["severe_tails"]["treated_cost_p95_usd"])

        missing_usage = json.loads(json.dumps(events))
        missing_usage[1]["provider_usage"]["provider_charge_usd"] = None
        # Recompute the immutable event ID after intentionally changing its evidence.
        changed = missing_usage[1]
        semantic = dict(changed)
        semantic.pop("event_id")
        from switchboard.domain.compand.grading import sha256_json

        changed["event_id"] = "sha256:" + sha256_json(semantic)
        red = MechanicalGrader().grade(
            missing_usage, technique_id="line-rle-v1", technique_version="1.0.0"
        )
        self.assertFalse(red["hard_gates"]["whole_task_economics"]["passed"])
        self.assertEqual(red["grades"]["hard_gate_grade"], "F")
        self.assertTrue(
            all(red["grades"][name]["band"] == "F" for name in GRADE_WEIGHTS)
        )

    def test_compiler_emits_immutable_layers_failure_bundle_and_exact_rerun(
        self,
    ) -> None:
        events = [
            score_event(
                run_id="run-b0",
                pair_id="pair-release",
                arm=AblationArm.BASELINE,
                provider_cost=1.00,
                overhead=0.00,
            ),
            score_event(
                run_id="run-e1",
                pair_id="pair-release",
                arm=AblationArm.ENFORCED,
                provider_cost=0.70,
                overhead=0.05,
            ),
        ]
        certification = {
            "agent": "fixture-agent",
            "dialect": "responses-fixture-v1",
            "model_provider_snapshot": "offline-fixture",
            "workload_revision": "phase2-corpus-v1",
            "ces_release": "CES-1",
        }
        with tempfile.TemporaryDirectory(prefix="qa57-release-") as temp:
            root = Path(temp)
            result = CesEvidenceReleaseStore(root / "source").create_release(
                release_id="qa57-release",
                contract=self.contract,
                events=events,
                technique_id="line-rle-v1",
                technique_version="1.0.0",
                certification_tuple=certification,
                claim="Named frozen-fixture result only.",
            )
            release = Path(result["release_dir"])
            self.assertEqual(result["hard_gate_grade"], "F")
            for layer in ("raw", "normalized", "published"):
                self.assertTrue((release / layer).is_dir())
            self.assertTrue(
                (release / "published" / "results" / "task-level.csv").is_file()
            )
            self.assertTrue(
                (release / "published" / "analysis" / "summary.json").is_file()
            )
            failure = json.loads(
                (release / "published" / "failure-bundle.json").read_text()
            )
            self.assertIn(
                "clean_environment_regeneration", failure["failed_hard_gates"]
            )
            self.assertEqual(
                failure["deterministic_rerun"], "./reproduce <empty-output-root>"
            )
            limitations = (release / "published" / "LIMITATIONS.md").read_text()
            self.assertIn("clean_environment_regeneration", limitations)
            reproduce_script = (release / "reproduce").read_text()
            self.assertIn(
                '--contract-root "$repo_root/docs/compand/phase2"',
                reproduce_script,
            )
            self.assertIn(
                '--corpus-root "$repo_root/fixtures/compand/phase2-technique-corpus/v1"',
                reproduce_script,
            )
            self.assertNotIn(str(CONTRACT), reproduce_script)
            self.assertNotIn(str(CORPUS), reproduce_script)
            scorecard = json.loads(
                (release / "published" / "results" / "scorecards.json").read_text()
            )[0]
            schema = json.loads((CONTRACT / "public-scorecard.schema.json").read_text())
            self.assertEqual(
                [], list(Draft202012Validator(schema).iter_errors(scorecard))
            )
            self.assertEqual(
                scorecard["trace"]["scorecard_sha256"], scorecard_sha256(scorecard)
            )
            self.assertTrue(
                all(not item["movement_allowed"] for item in scorecard["kpis"].values())
            )

            attestation = CesEvidenceReleaseStore(root / "reproduced").reproduce(
                source_release=release, contract=self.contract
            )
            self.assertTrue(attestation["checksums_match"])
            reproduced = Path(attestation["reproduced_release"])
            self.assertEqual(
                (release / "CHECKSUMS").read_bytes(),
                (reproduced / "CHECKSUMS").read_bytes(),
            )

            clean_result = CesEvidenceReleaseStore(root / "clean-source").create_release(
                release_id="qa57-clean-release",
                contract=self.contract,
                events=events,
                technique_id="line-rle-v1",
                technique_version="1.0.0",
                certification_tuple=certification,
                claim="Named clean-environment fixture result only.",
                clean_environment_regenerated=True,
            )
            self.assertEqual(clean_result["hard_gate_grade"], "pass")
            clean_release = Path(clean_result["release_dir"])
            clean_attestation = CesEvidenceReleaseStore(
                root / "clean-reproduced"
            ).reproduce(source_release=clean_release, contract=self.contract)
            self.assertTrue(clean_attestation["checksums_match"])
            clean_reproduced = Path(clean_attestation["reproduced_release"])
            self.assertEqual(
                (clean_release / "CHECKSUMS").read_bytes(),
                (clean_reproduced / "CHECKSUMS").read_bytes(),
            )
            with self.assertRaises(FileExistsError):
                CesEvidenceReleaseStore(root / "source").create_release(
                    release_id="qa57-release",
                    contract=self.contract,
                    events=events,
                    technique_id="line-rle-v1",
                    technique_version="1.0.0",
                    certification_tuple=certification,
                    claim="Replacement is forbidden.",
                )


if __name__ == "__main__":
    unittest.main()
