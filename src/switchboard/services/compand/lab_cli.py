"""Thin CLI adapter for the deterministic single-technique Compand lab."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from switchboard.application.commands.compand_lab import (
    fingerprint_label,
    run_single_technique,
)
from switchboard.domain.compand.lab import LabArm, LineRleTechnique, Technique
from switchboard.storage.compand_lab import ContentAddressedLabStore


def _technique(technique_id: str) -> Technique:
    if technique_id == LineRleTechnique.technique_id:
        return LineRleTechnique()
    raise ValueError("unsupported technique_id")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay one fixture through one deterministic Compand technique."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    replay = subparsers.add_parser("replay")
    replay.add_argument("fixture", type=Path)
    replay.add_argument("--output-root", type=Path, required=True)
    replay.add_argument(
        "--technique", choices=[LineRleTechnique.technique_id], required=True
    )
    replay.add_argument(
        "--arm", choices=[arm.value for arm in LabArm], default=LabArm.ENFORCED
    )
    replay.add_argument("--model-id", required=True)
    replay.add_argument("--config-id", required=True)
    replay.add_argument("--fixture-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        fixture = args.fixture.read_bytes()
        store = ContentAddressedLabStore(args.output_root)
        result = run_single_technique(
            fixture=fixture,
            fixture_id=args.fixture_id or "content-addressed-fixture",
            arm=LabArm(args.arm),
            technique=_technique(args.technique),
            model_fingerprint=fingerprint_label(args.model_id),
            config_fingerprint=fingerprint_label(args.config_id),
            evidence_store=store,
        )
    except (OSError, ValueError) as exc:
        parser.error(f"{type(exc).__name__}: {exc}")
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 1 if result.status == "failed" else 0
