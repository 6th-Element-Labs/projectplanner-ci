"""PROTO-10 isolated Compand technique catalog and plugin conformance."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401 - adds src/ to sys.path
from switchboard.application.commands.compand_lab import (
    fingerprint_label,
    run_single_technique,
)
from switchboard.domain.compand.lab import (
    DetectionContext,
    LabArm,
    Technique,
    sha256_evidence,
)
from switchboard.domain.compand.techniques import (
    ALL_TECHNIQUE_IDS,
    SUPPORTED_TECHNIQUE_IDS,
    TECHNIQUE_REGISTRY,
    UNSUPPORTED_TECHNIQUE_IDS,
    TechniqueSupportStatus,
    get_registration,
    resolve_technique,
)
from switchboard.domain.compand.techniques.registry import UnsupportedTechniqueError
from switchboard.domain.compand.scan import decode_line_rle
from switchboard.services.compand.lab_cli import build_parser
from switchboard.storage.compand_lab import ContentAddressedLabStore


CATALOG = ROOT / "docs" / "compand" / "phase2" / "technique-catalog.json"
CORPUS = ROOT / "fixtures" / "compand" / "phase2-technique-corpus" / "v1"
PLUGIN_ROOT = ROOT / "src" / "switchboard" / "domain" / "compand" / "techniques"


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def candidate_for(plugin: Technique, fixture_id: str, fixture: bytes):
    context = DetectionContext(
        fixture_id=fixture_id,
        input_hash=sha256_evidence(fixture),
        original=fixture,
    )
    candidates = plugin.detect(context)
    if not candidates:
        return None, None
    candidate = candidates[0]
    return candidate, plugin.estimate(candidate)


class Proto10TechniquePluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

    def positive_candidate(self, technique_id: str):
        for path in sorted(
            (CORPUS / "visible" / "development").glob("*-positive-v1.json")
        ):
            bundle = json.loads(path.read_text(encoding="utf-8"))
            for record in bundle["case_records"]:
                if (
                    record["expected_disposition_by_technique"].get(technique_id)
                    != "transform"
                ):
                    continue
                fixture = canonical_bytes(record)
                plugin = resolve_technique(technique_id)
                candidate, estimate = candidate_for(
                    plugin, record["record_id"], fixture
                )
                if candidate is not None and estimate.should_apply:
                    return plugin, candidate
        self.fail(f"no positive candidate for {technique_id}")

    def test_registry_is_exactly_the_frozen_catalog_surface(self) -> None:
        catalog_items = self.catalog["techniques"]
        catalog_ids = tuple(item["id"] for item in catalog_items)
        enforceable = tuple(
            item["id"] for item in catalog_items if item["cloud_gateway_enforceable"]
        )
        unsupported = tuple(
            item["id"] for item in catalog_items if not item["cloud_gateway_enforceable"]
        )
        self.assertEqual(30, len(catalog_ids))
        self.assertEqual(catalog_ids, ALL_TECHNIQUE_IDS)
        self.assertEqual(enforceable, SUPPORTED_TECHNIQUE_IDS)
        self.assertEqual(unsupported, UNSUPPORTED_TECHNIQUE_IDS)
        self.assertEqual(set(catalog_ids), set(TECHNIQUE_REGISTRY))
        self.assertEqual(14, len(SUPPORTED_TECHNIQUE_IDS))
        self.assertEqual(16, len(UNSUPPORTED_TECHNIQUE_IDS))

    def test_supported_plugins_conform_and_are_one_package_each(self) -> None:
        modules: set[str] = set()
        for technique_id in SUPPORTED_TECHNIQUE_IDS:
            with self.subTest(technique_id=technique_id):
                registration = get_registration(technique_id)
                plugin = registration.instantiate()
                self.assertIs(registration.status, TechniqueSupportStatus.SUPPORTED)
                self.assertIsInstance(plugin, Technique)
                self.assertEqual(technique_id, plugin.technique_id)
                self.assertEqual("1.0.0", plugin.technique_version)
                modules.add(type(plugin).__module__)
                package = PLUGIN_ROOT / technique_id.replace("-", "_")
                self.assertTrue((package / "__init__.py").is_file(), package)
        self.assertEqual(14, len(modules))

    def test_plugin_packages_do_not_import_one_another(self) -> None:
        plugin_names = {item.replace("-", "_") for item in SUPPORTED_TECHNIQUE_IDS}
        for technique_id in SUPPORTED_TECHNIQUE_IDS:
            path = PLUGIN_ROOT / technique_id.replace("-", "_") / "__init__.py"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = ""
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                elif isinstance(node, ast.Import):
                    module = ".".join(alias.name for alias in node.names)
                imported_plugin = next(
                    (name for name in plugin_names if name in module), None
                )
                self.assertIsNone(
                    imported_plugin,
                    f"{technique_id} imports sibling plugin {imported_plugin}",
                )

    def test_every_frozen_positive_transform_executes_and_recovers_exactly(self) -> None:
        observed: set[str] = set()
        for path in sorted((CORPUS / "visible" / "development").glob("*-positive-v1.json")):
            bundle = json.loads(path.read_text(encoding="utf-8"))
            for record in bundle["case_records"]:
                fixture = canonical_bytes(record)
                expected = {
                    key
                    for key, disposition in record[
                        "expected_disposition_by_technique"
                    ].items()
                    if disposition == "transform" and key in SUPPORTED_TECHNIQUE_IDS
                }
                for technique_id in expected:
                    with self.subTest(technique_id=technique_id, case_id=record["case_id"]):
                        plugin = resolve_technique(technique_id)
                        candidate, estimate = candidate_for(
                            plugin, record["record_id"], fixture
                        )
                        self.assertIsNotNone(candidate)
                        self.assertTrue(estimate.should_apply)
                        applied = plugin.apply(candidate)
                        proof = plugin.verify(
                            fixture, applied.transformed, applied.recovered
                        )
                        self.assertTrue(proof.passed)
                        self.assertEqual(fixture, applied.recovered)
                        observed.add(technique_id)

        # The frozen corpus deliberately has no presentation-only CRLF positive case.
        line_ending_fixture = canonical_bytes(
            {
                "fixture_family": "terminal_and_progress",
                "scenario": {
                    "presentation_only": True,
                    "byte_sensitive": False,
                    "integrity_protected": False,
                    "terminal_bytes_utf8": "one\r\ntwo\r\n",
                },
            }
        )
        line_ending = resolve_technique("line-ending-normalize-v1")
        candidate, estimate = candidate_for(
            line_ending, "line-ending-positive", line_ending_fixture
        )
        self.assertIsNotNone(candidate)
        self.assertTrue(estimate.should_apply)
        applied = line_ending.apply(candidate)
        self.assertTrue(
            line_ending.verify(
                line_ending_fixture, applied.transformed, applied.recovered
            ).passed
        )
        observed.add("line-ending-normalize-v1")
        self.assertEqual(set(SUPPORTED_TECHNIQUE_IDS), observed)

    def test_frozen_boundary_cases_never_enforce(self) -> None:
        supported = set(SUPPORTED_TECHNIQUE_IDS)
        for path in sorted((CORPUS / "visible" / "development").glob("*-boundary-v1.json")):
            bundle = json.loads(path.read_text(encoding="utf-8"))
            for record in bundle["case_records"]:
                fixture = canonical_bytes(record)
                for technique_id, disposition in record[
                    "expected_disposition_by_technique"
                ].items():
                    if technique_id not in supported or disposition == "transform":
                        continue
                    with self.subTest(technique_id=technique_id, case_id=record["case_id"]):
                        plugin = resolve_technique(technique_id)
                        candidate, estimate = candidate_for(
                            plugin, record["record_id"], fixture
                        )
                        self.assertTrue(
                            candidate is None or estimate.should_apply is False,
                            disposition,
                        )

    def test_every_plugin_rejects_corrupt_transformed_bytes(self) -> None:
        for technique_id in SUPPORTED_TECHNIQUE_IDS:
            if technique_id == "line-ending-normalize-v1":
                fixture = canonical_bytes(
                    {
                        "fixture_family": "terminal_and_progress",
                        "scenario": {
                            "presentation_only": True,
                            "byte_sensitive": False,
                            "integrity_protected": False,
                            "terminal_bytes_utf8": "one\r\ntwo\r\n",
                        },
                    }
                )
                plugin = resolve_technique(technique_id)
                candidate, estimate = candidate_for(
                    plugin, "line-ending-corruption", fixture
                )
                self.assertTrue(estimate.should_apply)
            else:
                plugin, candidate = self.positive_candidate(technique_id)
            with self.subTest(technique_id=technique_id):
                corrupted = replace(candidate, proposed=candidate.proposed + b"\x00")
                with self.assertRaises(ValueError):
                    plugin.apply(corrupted)

    def test_line_rle_apply_uses_decoder_output(self) -> None:
        original = b"decoder-owned line\n" * 24
        plugin = resolve_technique("line-rle-v1")
        candidate, estimate = candidate_for(plugin, "direct-line-rle", original)
        self.assertIsNotNone(candidate)
        self.assertTrue(estimate.should_apply)
        with patch.object(plugin, "propose", side_effect=AssertionError("not recovery")):
            applied = plugin.apply(candidate)
        decoded = decode_line_rle(candidate.proposed.decode("utf-8")).encode("utf-8")
        self.assertEqual(decoded, applied.recovered)
        self.assertEqual("line_rle_decoder", applied.recovery_metadata["recovery_kind"])
        self.assertFalse(applied.recovery_metadata["source_artifact_retained"])

    def test_content_addressed_reference_rejects_hash_tampering(self) -> None:
        plugin, candidate = self.positive_candidate("exact-duplicate-reference-v1")
        reference = json.loads(candidate.proposed)
        reference["sha256"] = "0" * 64
        corrupted = replace(candidate, proposed=canonical_bytes(reference))
        with self.assertRaises(ValueError):
            plugin.apply(corrupted)

    def test_unsupported_records_match_catalog_and_never_instantiate(self) -> None:
        catalog_by_id = {item["id"]: item for item in self.catalog["techniques"]}
        for technique_id in UNSUPPORTED_TECHNIQUE_IDS:
            with self.subTest(technique_id=technique_id):
                registration = get_registration(technique_id)
                record = registration.unsupported
                expected = catalog_by_id[technique_id]
                self.assertIs(registration.status, TechniqueSupportStatus.UNSUPPORTED)
                self.assertIsNotNone(record)
                self.assertEqual(expected["version"], record.technique_version)
                self.assertEqual(expected["cloud_eligibility"], record.cloud_eligibility)
                self.assertEqual(expected["guarantee"], record.guarantee)
                self.assertEqual(expected["unsupported_reason"], record.unsupported_reason)
                self.assertEqual(expected["host_dependency"], record.host_dependency)
                with self.assertRaises(UnsupportedTechniqueError):
                    resolve_technique(technique_id)

    def test_cli_exposes_all_ids_and_distinguishes_unsupported_from_no_candidate(self) -> None:
        parser = build_parser()
        replay = next(
            action.choices["replay"]
            for action in parser._actions
            if getattr(action, "choices", None) and "replay" in action.choices
        )
        technique_action = next(
            action for action in replay._actions if action.dest == "technique"
        )
        self.assertEqual(ALL_TECHNIQUE_IDS, tuple(technique_action.choices))

        with tempfile.TemporaryDirectory(prefix="proto10-cli-") as temp:
            root = Path(temp)
            fixture = root / "fixture.txt"
            fixture.write_text("ordinary text\n", encoding="utf-8")
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "src")])
            unsupported = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "compand_lab.py"),
                    "replay",
                    str(fixture),
                    "--output-root",
                    str(root / "unsupported"),
                    "--technique",
                    "context-paging-v1",
                    "--arm",
                    "E1",
                    "--model-id",
                    "offline-model",
                    "--config-id",
                    "unsupported-config",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(0, unsupported.returncode, unsupported.stderr)
            unsupported_payload = json.loads(unsupported.stdout)
            self.assertEqual("unsupported", unsupported_payload["status"])
            self.assertEqual(
                "unsupported_technique", unsupported_payload["reason_code"]
            )
            self.assertFalse((root / "unsupported").exists())

            result = run_single_technique(
                fixture=fixture.read_bytes(),
                fixture_id="ordinary-text",
                arm=LabArm.ENFORCED,
                technique=resolve_technique("ansi-osc-strip-v1"),
                model_fingerprint=fingerprint_label("offline-model"),
                config_fingerprint=fingerprint_label("ansi-config"),
                evidence_store=ContentAddressedLabStore(root / "no-candidate"),
            )
            self.assertEqual("declined", result.status)
            self.assertEqual("no_candidate", result.reason_code)


if __name__ == "__main__":
    unittest.main()
