"""Immutable filesystem adapter for CES-1 ablation evidence releases."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from switchboard.application.commands.compand_ablation import (
    FrozenLabContract,
    MechanicalGrader,
    public_scorecard,
    verify_score_input_event,
)
from switchboard.domain.compand.grading import canonical_json_bytes, sha256_hex


_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _render_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )


def _write_exclusive(path: Path, value: bytes, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755 if executable else 0o644
    )
    try:
        written = os.write(descriptor, value)
        if written != len(value):
            raise OSError(f"short immutable evidence write: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    path.chmod(0o555 if executable else 0o444)


def _event_lines(events: Sequence[Mapping[str, Any]]) -> bytes:
    rendered: list[bytes] = []
    next_sequence: dict[str, int] = {}
    parent: dict[str, str | None] = {}
    for event in events:
        if event.get("schema") == "compand.ces1.score_input.v1":
            verify_score_input_event(event)
        elif not event.get("event_id"):
            raise ValueError("raw event is missing event_id")
        run_id = str(event["run_id"])
        expected_sequence = next_sequence.get(run_id, 1)
        if event["sequence"] != expected_sequence:
            raise ValueError(f"non-monotonic score input sequence for {run_id}")
        if event.get("parent_event_id") != parent.get(run_id):
            raise ValueError(f"broken score input parent chain for {run_id}")
        next_sequence[run_id] = expected_sequence + 1
        parent[run_id] = str(event["event_id"])
        rendered.append(canonical_json_bytes(event) + b"\n")
    return b"".join(rendered)


class CesEvidenceReleaseStore:
    """Create one immutable raw/normalized/published CES-1 release."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create_release(
        self,
        *,
        release_id: str,
        contract: FrozenLabContract,
        events: Sequence[Mapping[str, Any]],
        technique_id: str,
        technique_version: str,
        certification_tuple: Mapping[str, str],
        claim: str,
        clean_environment_regenerated: bool = False,
    ) -> dict[str, Any]:
        if not _RELEASE_ID.fullmatch(release_id):
            raise ValueError("release_id is not filesystem safe")
        release_dir = self.root / release_id
        release_dir.mkdir(parents=False, exist_ok=False)
        manifest = {
            "schema": "compand.ces1.evidence_release_manifest.v1",
            "release_id": release_id,
            "technique_id": technique_id,
            "technique_version": technique_version,
            "certification_tuple": dict(certification_tuple),
            "claim": claim,
            "config_fingerprint": contract.config_fingerprint,
            "contract_hashes": dict(contract.contract_hashes),
            "evidence_layers": ["raw", "normalized", "published"],
            "correction_policy": "new_release_and_changelog_only",
            "clean_environment_regenerated": bool(clean_environment_regenerated),
            "confirmatory_traffic_authorized": False,
            "production_promotion_authorized": False,
        }
        _write_exclusive(
            release_dir / "raw" / "release-manifest.json", _render_json(manifest)
        )
        _write_exclusive(release_dir / "raw" / "events.jsonl", _event_lines(events))
        compiled = self._compile(
            release_dir=release_dir,
            contract=contract,
            events=events,
            manifest=manifest,
            clean_environment_regenerated=clean_environment_regenerated,
        )
        return {
            "schema": "compand.ces1.evidence_release_result.v1",
            "release_id": release_id,
            "release_dir": str(release_dir),
            "scorecard_sha256": compiled["scorecard"]["trace"]["scorecard_sha256"],
            "hard_gate_grade": compiled["scorecard"]["grades"]["hard_gate_grade"],
            "failure_bundle": str(release_dir / "published" / "failure-bundle.json"),
            "reproduce": str(release_dir / "reproduce"),
            "checksums": str(release_dir / "CHECKSUMS"),
        }

    def reproduce(
        self,
        *,
        source_release: Path,
        contract: FrozenLabContract,
    ) -> dict[str, Any]:
        """Regenerate a release into this store and compare every public checksum."""

        source_release = source_release.resolve()
        manifest = json.loads(
            (source_release / "raw" / "release-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        if manifest.get("config_fingerprint") != contract.config_fingerprint:
            raise ValueError("release and frozen contract fingerprints differ")
        events = _read_events(source_release / "raw" / "events.jsonl")
        result = self.create_release(
            release_id=str(manifest["release_id"]),
            contract=contract,
            events=events,
            technique_id=str(manifest["technique_id"]),
            technique_version=str(manifest["technique_version"]),
            certification_tuple=manifest["certification_tuple"],
            claim=str(manifest["claim"]),
            clean_environment_regenerated=bool(
                manifest.get("clean_environment_regenerated", False)
            ),
        )
        reproduced_dir = Path(result["release_dir"])
        source_checksums = _read_checksums(source_release / "CHECKSUMS")
        reproduced_checksums = _read_checksums(reproduced_dir / "CHECKSUMS")
        matches = source_checksums == reproduced_checksums
        attestation = {
            "schema": "compand.ces1.clean_environment_reproduction.v1",
            "source_release": str(source_release),
            "reproduced_release": str(reproduced_dir),
            "config_fingerprint": contract.config_fingerprint,
            "checksums_match": matches,
            "source_checksums_sha256": sha256_hex(
                (source_release / "CHECKSUMS").read_bytes()
            ),
            "reproduced_checksums_sha256": sha256_hex(
                (reproduced_dir / "CHECKSUMS").read_bytes()
            ),
        }
        if not matches:
            raise ValueError("clean-environment publication checksums differ")
        return attestation

    def _compile(
        self,
        *,
        release_dir: Path,
        contract: FrozenLabContract,
        events: Sequence[Mapping[str, Any]],
        manifest: Mapping[str, Any],
        clean_environment_regenerated: bool,
    ) -> dict[str, Any]:
        grade = MechanicalGrader().grade(
            events,
            technique_id=str(manifest["technique_id"]),
            technique_version=str(manifest["technique_version"]),
        )
        scorecard = public_scorecard(
            grade,
            release_id=str(manifest["release_id"]),
            certification_tuple=manifest["certification_tuple"],
            contract=contract,
            claim=str(manifest["claim"]),
            clean_environment_regenerated=clean_environment_regenerated,
        )
        normalized = release_dir / "normalized"
        published = release_dir / "published"
        _write_exclusive(normalized / "mechanical-grade.json", _render_json(grade))
        _write_exclusive(
            normalized / "task-level.jsonl",
            b"".join(canonical_json_bytes(row) + b"\n" for row in grade["task_level"]),
        )
        _write_exclusive(
            published / "results" / "task-level.csv", _task_csv(grade["task_level"])
        )
        _write_exclusive(
            published / "results" / "scorecards.json", _render_json([scorecard])
        )
        _write_exclusive(
            published / "analysis" / "summary.json",
            _render_json(
                {
                    "schema": "compand.ces1.analysis_summary.v1",
                    "effects": grade["effects"],
                    "severe_tails": grade["severe_tails"],
                    "sample": grade["sample"],
                }
            ),
        )
        failure_bundle = _failure_bundle(grade, scorecard, manifest)
        _write_exclusive(
            published / "failure-bundle.json", _render_json(failure_bundle)
        )
        _write_exclusive(
            published / "LIMITATIONS.md",
            _limitations(scorecard, manifest).encode("utf-8"),
        )
        _write_exclusive(
            published / "SECURITY.md",
            (
                "# Security\n\nRaw customer content and credentials are forbidden from this "
                "release. Evidence uses content hashes and sanitized frozen fixtures.\n"
            ).encode("utf-8"),
        )
        _write_exclusive(
            release_dir / "CHANGELOG.md",
            (
                f"# Changelog\n\n- {manifest['release_id']}: initial immutable exploratory release.\n"
            ).encode("utf-8"),
        )
        _write_exclusive(
            release_dir / "reproduce",
            _reproduce_script(contract).encode("utf-8"),
            executable=True,
        )
        self._copy_contract(release_dir, contract)
        _write_exclusive(release_dir / "CHECKSUMS", _checksums(release_dir))
        return {
            "grade": grade,
            "scorecard": scorecard,
            "failure_bundle": failure_bundle,
        }

    @staticmethod
    def _copy_contract(release_dir: Path, contract: FrozenLabContract) -> None:
        for name in (
            "benchmark.yaml",
            "BENCHMARK-CARD.md",
            "corpus-manifest.json",
            "system-card.json",
            "public-scorecard.schema.json",
            "technique-catalog.json",
        ):
            _write_exclusive(
                release_dir / "published" / name,
                (contract.contract_root / name).read_bytes(),
            )


def _read_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError("raw event is not an object")
        events.append(event)
    _event_lines(events)
    return events


def _task_csv(rows: Sequence[Mapping[str, Any]]) -> bytes:
    if not rows:
        raise ValueError("task-level result table cannot be empty")
    fields = list(rows[0])
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        if set(row) != set(fields):
            raise ValueError("task-level rows have inconsistent fields")
        writer.writerow(row)
    return output.getvalue().encode("utf-8")


def _failure_bundle(
    grade: Mapping[str, Any],
    scorecard: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    failed = {
        gate_id: result
        for gate_id, result in scorecard["hard_gates"].items()
        if not result["passed"]
    }
    return {
        "schema": "compand.ces1.failure_bundle.v1",
        "release_id": manifest["release_id"],
        "grade": scorecard["grades"]["hard_gate_grade"],
        "failed_hard_gates": failed,
        "failing_task_rows": [
            row
            for row in grade["task_level"]
            if row["paired_cost_difference_usd"] is None
            or row["paired_success_difference"] < 0
            or row["paired_recovery_difference"] < 0
        ],
        "event_ids": grade["event_ids"],
        "deterministic_rerun": "./reproduce <empty-output-root>",
        "limitations": "published/LIMITATIONS.md",
    }


def _limitations(scorecard: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    failed = [
        gate_id
        for gate_id, result in scorecard["hard_gates"].items()
        if not result["passed"]
    ]
    missing = int(scorecard["sample"]["missing"])
    return (
        "# Limitations\n\n"
        f"Release `{manifest['release_id']}` is exploratory C1 mechanical evidence only. "
        "It does not authorize confirmatory traffic, production promotion, a verified Value "
        "Index movement, or a product-wide savings claim.\n\n"
        f"Failed hard gates: {', '.join(failed) if failed else 'none'}.\n\n"
        f"Task pairs with missing required economics or outcomes: {missing}.\n\n"
        "Provider usage remains authoritative when exposed; missing fields were not imputed as zero.\n"
    )


def _reproduce_script(contract: FrozenLabContract) -> str:
    repository_root = contract.contract_root.parents[2]
    try:
        contract_relative = contract.contract_root.relative_to(repository_root)
        corpus_relative = contract.corpus_root.relative_to(repository_root)
    except ValueError as exc:
        raise ValueError(
            "frozen contract and corpus must live under the repository root"
        ) from exc
    quoted_repository_root = shlex.quote(str(repository_root))
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "if [ \"$#\" -ne 1 ]; then echo 'usage: ./reproduce <empty-output-root>' >&2; exit 2; fi\n"
        'release_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        f"repo_root=${{COMPAND_REPO_ROOT:-{quoted_repository_root}}}\n"
        'exec python3 "$repo_root/scripts/compand_lab.py" regenerate "$release_dir" '
        f'--contract-root "$repo_root/{contract_relative.as_posix()}" '
        f'--corpus-root "$repo_root/{corpus_relative.as_posix()}" --output-root "$1"\n'
    )


def _checksums(release_dir: Path) -> bytes:
    lines: list[str] = []
    for path in sorted(release_dir.rglob("*")):
        if not path.is_file() or path.name == "CHECKSUMS":
            continue
        relative = path.relative_to(release_dir).as_posix()
        lines.append(f"{sha256_hex(path.read_bytes())}  {relative}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _read_checksums(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        result[relative] = digest
    return result


__all__ = ["CesEvidenceReleaseStore"]
