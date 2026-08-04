"""ADAPTER-41 deterministic single-technique replay wire tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from path_setup import ROOT  # noqa: F401 - adds src/ to sys.path
from switchboard.application.commands.compand_lab import (
    fingerprint_label,
    run_single_technique,
)
from switchboard.domain.compand.lab import (
    LabArm,
    LineRleTechnique,
    TechniqueCandidate,
)
from switchboard.storage.compand_lab import ContentAddressedLabStore


PRIVATE_LINE = b"private-credential-shaped-content\n"
FIXTURE = PRIVATE_LINE * 4 + b"complete\n"


class DeterministicClock:
    def __init__(self) -> None:
        self.tick = 0

    def recorded_at(self) -> str:
        return "2026-08-04T00:00:00Z"

    def monotonic_ns(self) -> int:
        self.tick += 1_000_000
        return self.tick


class ExplodingLineRleTechnique(LineRleTechnique):
    def apply(self, candidate: TechniqueCandidate):
        raise RuntimeError("private-credential-shaped-content")


def events_for(result) -> list[dict[str, object]]:
    path = Path(result.run_location) / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def normalized_events(events: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized = []
    for event in events:
        normalized.append(
            {
                key: value
                for key, value in event.items()
                if key not in {"run_id", "recorded_at", "elapsed_ms"}
            }
        )
    return normalized


class CompandLabTests(unittest.TestCase):
    def run_lab(self, root: Path, run_id: str, technique=None):
        store = ContentAddressedLabStore(root)
        result = run_single_technique(
            fixture=FIXTURE,
            fixture_id="fixture-repeated-lines",
            arm=LabArm.ENFORCED,
            technique=technique or LineRleTechnique(),
            model_fingerprint=fingerprint_label("offline-model-fixture"),
            config_fingerprint=fingerprint_label("line-rle-default"),
            evidence_store=store,
            clock=DeterministicClock(),
            run_id_factory=lambda: run_id,
        )
        return store, result

    def test_replay_traces_every_derived_byte_without_logging_content(self) -> None:
        with tempfile.TemporaryDirectory(prefix="adapter41-success-") as temp:
            store, result = self.run_lab(Path(temp), "run-success")
            self.assertEqual("succeeded", result.status)
            self.assertNotEqual(result.input_hash, result.output_hash)
            self.assertEqual(FIXTURE, store.object_path(result.input_hash).read_bytes())
            transformed = store.object_path(result.output_hash).read_bytes()
            self.assertLess(len(transformed), len(FIXTURE))

            run_dir = Path(result.run_location)
            manifest = json.loads(
                (run_dir / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result.input_hash, manifest["baseline_object_hash"])
            self.assertFalse(manifest["authority"]["confirmatory_traffic_authorized"])
            events = events_for(result)
            self.assertEqual(
                list(range(1, len(events) + 1)), [e["sequence"] for e in events]
            )
            for index, event in enumerate(events):
                for field in (
                    "run_id",
                    "arm",
                    "technique_id",
                    "technique_version",
                    "candidate_id",
                    "input_hash",
                    "output_hash",
                    "parent_event_id",
                    "model_fingerprint",
                    "config_fingerprint",
                    "sequence",
                    "elapsed_ms",
                    "reason_code",
                ):
                    self.assertIn(field, event)
                if index:
                    self.assertEqual(
                        events[index - 1]["event_id"], event["parent_event_id"]
                    )
            self.assertEqual(
                [(event["stage"], event["status"]) for event in events],
                [
                    ("detect", "started"),
                    ("detect", "succeeded"),
                    ("estimate", "started"),
                    ("estimate", "succeeded"),
                    ("apply", "started"),
                    ("apply", "succeeded"),
                    ("verify", "started"),
                    ("verify", "succeeded"),
                ],
            )
            evidence_text = (run_dir / "run_manifest.json").read_bytes() + (
                run_dir / "events.jsonl"
            ).read_bytes()
            self.assertNotIn(PRIVATE_LINE.strip(), evidence_text)

    def test_plugin_failure_preserves_baseline_and_emits_red_event(self) -> None:
        with tempfile.TemporaryDirectory(prefix="adapter41-failure-") as temp:
            store, result = self.run_lab(
                Path(temp), "run-failure", ExplodingLineRleTechnique()
            )
            baseline_path = store.object_path(result.input_hash)
            baseline_before = baseline_path.read_bytes()
            self.assertEqual("failed", result.status)
            self.assertEqual(result.input_hash, result.output_hash)
            self.assertEqual(baseline_before, baseline_path.read_bytes())
            events = events_for(result)
            failed = [event for event in events if event["status"] == "failed"]
            self.assertEqual(1, len(failed))
            self.assertEqual("red", failed[0]["severity"])
            self.assertEqual("technique_failure", failed[0]["reason_code"])
            self.assertEqual("RuntimeError", failed[0]["error_type"])
            self.assertNotIn(
                PRIVATE_LINE.strip(),
                (Path(result.run_location) / "events.jsonl").read_bytes(),
            )

    def test_reruns_have_new_ids_and_deterministic_semantic_events(self) -> None:
        with tempfile.TemporaryDirectory(prefix="adapter41-determinism-") as temp:
            root = Path(temp)
            _, first = self.run_lab(root / "first", "run-first")
            _, second = self.run_lab(root / "second", "run-second")
            self.assertNotEqual(first.run_id, second.run_id)
            self.assertEqual(first.input_hash, second.input_hash)
            self.assertEqual(first.output_hash, second.output_hash)
            self.assertEqual(
                normalized_events(events_for(first)),
                normalized_events(events_for(second)),
            )

    def test_baseline_and_shadow_never_write_a_transformed_object(self) -> None:
        with tempfile.TemporaryDirectory(prefix="adapter41-arms-") as temp:
            root = Path(temp)
            for arm in (LabArm.BASELINE, LabArm.SHADOW):
                store = ContentAddressedLabStore(root / arm.value)
                result = run_single_technique(
                    fixture=FIXTURE,
                    fixture_id="fixture-repeated-lines",
                    arm=arm,
                    technique=LineRleTechnique(),
                    model_fingerprint=fingerprint_label("offline-model-fixture"),
                    config_fingerprint=fingerprint_label("line-rle-default"),
                    evidence_store=store,
                    clock=DeterministicClock(),
                    run_id_factory=lambda arm=arm: f"run-{arm.value}",
                )
                self.assertEqual("succeeded", result.status)
                self.assertEqual(result.input_hash, result.output_hash)
                object_files = [
                    path for path in store.objects_root.rglob("*") if path.is_file()
                ]
                self.assertEqual([store.object_path(result.input_hash)], object_files)
                self.assertFalse(
                    any(event["status"] == "failed" for event in events_for(result))
                )

    def test_manifest_refuses_overwrite_and_cli_replays_in_one_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="adapter41-cli-") as temp:
            root = Path(temp)
            store, _ = self.run_lab(root / "exclusive", "run-exclusive")
            with self.assertRaises(FileExistsError):
                self.run_lab(root / "exclusive", "run-exclusive")
            self.assertTrue(
                store.object_path(
                    "sha256:" + hashlib.sha256(FIXTURE).hexdigest()
                ).is_file()
            )

            fixture = root / "fixture.txt"
            fixture.write_bytes(FIXTURE)
            output_root = root / "cli-output"
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "src")])
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "compand_lab.py"),
                    "replay",
                    str(fixture),
                    "--output-root",
                    str(output_root),
                    "--technique",
                    "line-rle-v1",
                    "--arm",
                    "E1",
                    "--model-id",
                    "offline-model-fixture",
                    "--config-id",
                    "line-rle-default",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual("succeeded", payload["status"])
            self.assertTrue((Path(payload["run_location"]) / "events.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
