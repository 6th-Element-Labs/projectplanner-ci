"""Thin CLI adapter for the deterministic single-technique Compand lab."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from switchboard.application.commands.compand_ablation import (
    build_ablation_plan,
    fixture_oracle_score_event,
    load_plan_case,
    validate_frozen_lab_contract,
)
from switchboard.application.commands.compand_lab import (
    fingerprint_label,
    run_single_technique,
)
from switchboard.domain.compand.grading import AblationArm
from switchboard.domain.compand.lab import LabArm, Technique
from switchboard.domain.compand.techniques import ALL_TECHNIQUE_IDS, resolve_technique
from switchboard.domain.compand.techniques.registry import UnsupportedTechniqueError
from switchboard.storage.compand_evidence import CesEvidenceReleaseStore
from switchboard.storage.compand_lab import ContentAddressedLabStore


def _technique(technique_id: str) -> Technique:
    return resolve_technique(technique_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay one fixture through one deterministic Compand technique."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    replay = subparsers.add_parser("replay")
    replay.add_argument("fixture", type=Path)
    replay.add_argument("--output-root", type=Path, required=True)
    replay.add_argument("--technique", choices=ALL_TECHNIQUE_IDS, required=True)
    replay.add_argument(
        "--arm", choices=[arm.value for arm in LabArm], default=LabArm.ENFORCED
    )
    replay.add_argument("--model-id", required=True)
    replay.add_argument("--config-id", required=True)
    replay.add_argument("--fixture-id")

    ablate = subparsers.add_parser(
        "ablate",
        description="Run frozen development B0/S1/E1 ablations and grade them.",
    )
    ablate.add_argument("--contract-root", type=Path, required=True)
    ablate.add_argument("--corpus-root", type=Path, required=True)
    ablate.add_argument("--output-root", type=Path, required=True)
    ablate.add_argument("--release-id", required=True)
    ablate.add_argument("--technique", choices=ALL_TECHNIQUE_IDS, required=True)
    ablate.add_argument("--repetitions", type=int, default=1)
    ablate.add_argument("--model-id", required=True)
    ablate.add_argument("--config-id", required=True)
    ablate.add_argument("--agent", default="development-fixture-agent")
    ablate.add_argument("--dialect", default="responses-fixture-v1")
    ablate.add_argument("--model-provider-snapshot", default="offline-fixture")
    ablate.add_argument("--workload-revision", default="phase2-corpus-v1")

    regenerate = subparsers.add_parser(
        "regenerate", description="Regenerate a release from immutable raw evidence."
    )
    regenerate.add_argument("source_release", type=Path)
    regenerate.add_argument("--contract-root", type=Path, required=True)
    regenerate.add_argument("--corpus-root", type=Path, required=True)
    regenerate.add_argument("--output-root", type=Path, required=True)

    failure = subparsers.add_parser(
        "failure-bundle", description="Print a release's one-command failure bundle."
    )
    failure.add_argument("source_release", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "failure-bundle":
        try:
            payload = json.loads(
                (args.source_release / "published" / "failure-bundle.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            parser.error(f"{type(exc).__name__}: {exc}")
        print(json.dumps(payload, sort_keys=True))
        return 1 if payload.get("grade") == "F" else 0
    if args.command == "regenerate":
        try:
            contract = validate_frozen_lab_contract(
                contract_root=args.contract_root, corpus_root=args.corpus_root
            )
            attestation = CesEvidenceReleaseStore(args.output_root).reproduce(
                source_release=args.source_release, contract=contract
            )
        except (OSError, ValueError) as exc:
            parser.error(f"{type(exc).__name__}: {exc}")
        print(json.dumps(attestation, sort_keys=True))
        return 0
    if args.command == "ablate":
        return _ablate(args, parser)
    try:
        technique = _technique(args.technique)
        fixture = args.fixture.read_bytes()
        store = ContentAddressedLabStore(args.output_root)
        result = run_single_technique(
            fixture=fixture,
            fixture_id=args.fixture_id or "content-addressed-fixture",
            arm=LabArm(args.arm),
            technique=technique,
            model_fingerprint=fingerprint_label(args.model_id),
            config_fingerprint=fingerprint_label(args.config_id),
            evidence_store=store,
        )
    except UnsupportedTechniqueError as exc:
        print(json.dumps(exc.record.as_dict(), sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        parser.error(f"{type(exc).__name__}: {exc}")
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 1 if result.status == "failed" else 0


def _ablate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        contract = validate_frozen_lab_contract(
            contract_root=args.contract_root, corpus_root=args.corpus_root
        )
        plan = build_ablation_plan(
            contract,
            technique_ids=[args.technique],
            repetitions=args.repetitions,
        )
        technique = resolve_technique(args.technique)
        run_config_fingerprint = fingerprint_label(
            f"{contract.config_fingerprint}\0{args.config_id}"
        )
        raw_events: list[dict[str, object]] = []
        work_root = args.output_root / "run-evidence" / args.release_id
        for entry in plan:
            fixture, _record = load_plan_case(entry)
            arm = {
                AblationArm.BASELINE: LabArm.BASELINE,
                AblationArm.SHADOW: LabArm.SHADOW,
                AblationArm.ENFORCED: LabArm.ENFORCED,
            }.get(entry.arm)
            if arm is None:
                raise ValueError(
                    "C1 execution requires a separately frozen combination plan"
                )
            result = run_single_technique(
                fixture=fixture,
                fixture_id=entry.fixture_id,
                arm=arm,
                technique=technique,
                model_fingerprint=fingerprint_label(args.model_id),
                config_fingerprint=run_config_fingerprint,
                evidence_store=ContentAddressedLabStore(work_root),
                run_id_factory=lambda plan_id=entry.plan_id: plan_id.replace(
                    "plan-", "run-"
                ),
            )
            replay_events = [
                json.loads(line)
                for line in (Path(result.run_location) / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            score_event = fixture_oracle_score_event(
                contract=contract,
                entry=entry,
                result=result,
                replay_events=replay_events,
                technique_version=technique.technique_version,
                config_fingerprint=run_config_fingerprint,
            )
            raw_events.extend(replay_events)
            raw_events.append(score_event)
        release = CesEvidenceReleaseStore(args.output_root / "releases").create_release(
            release_id=args.release_id,
            contract=contract,
            events=raw_events,
            technique_id=args.technique,
            technique_version=technique.technique_version,
            certification_tuple={
                "agent": args.agent,
                "dialect": args.dialect,
                "model_provider_snapshot": args.model_provider_snapshot,
                "workload_revision": args.workload_revision,
                "ces_release": "CES-1",
            },
            claim="Exploratory frozen-fixture mechanical result only.",
        )
    except (OSError, UnsupportedTechniqueError, ValueError) as exc:
        parser.error(f"{type(exc).__name__}: {exc}")
    print(json.dumps(release, sort_keys=True))
    return 1 if release["hard_gate_grade"] == "F" else 0


if __name__ == "__main__":
    raise SystemExit(main())
