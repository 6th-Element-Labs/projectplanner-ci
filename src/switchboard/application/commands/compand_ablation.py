"""Deterministic CES-1 ablation planning, normalization, and grading.

This application module orchestrates existing removable technique plugins.  It
does not put persistence, publication, grading, retries, or lifecycle authority
inside those plugins.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from switchboard.domain.compand.grading import (
    AblationArm,
    GRADE_WEIGHTS,
    HARD_GATE_IDS,
    KPI_IDS,
    canonical_json_bytes,
    interval_95,
    percentile,
    scorecard_sha256,
    sha256_hex,
    sha256_json,
    weighted_grade,
)
from switchboard.application.commands.compand_lab import LabRunResult


_SCORE_EVENT_SCHEMA = "compand.ces1.score_input.v1"
_REQUIRED_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
    "provider_charge_usd",
    "compand_overhead_usd",
)


@dataclass(frozen=True)
class FrozenLabContract:
    contract_root: Path
    corpus_root: Path
    benchmark: Mapping[str, Any]
    catalog: Mapping[str, Any]
    corpus_manifest: Mapping[str, Any]
    system_card: Mapping[str, Any]
    index: Mapping[str, Any]
    contract_hashes: Mapping[str, str]
    fixture_paths: tuple[Path, ...]
    config_fingerprint: str

    @property
    def techniques(self) -> Mapping[str, Mapping[str, Any]]:
        return {str(item["id"]): item for item in self.catalog["techniques"]}


@dataclass(frozen=True)
class AblationPlanEntry:
    plan_id: str
    arm: AblationArm
    fixture_path: Path
    fixture_id: str
    record_id: str
    technique_ids: tuple[str, ...]
    repetition: int
    order_key: str

    def as_dict(self, *, corpus_root: Path) -> dict[str, object]:
        return {
            "schema": "compand.ces1.ablation_plan_entry.v1",
            "plan_id": self.plan_id,
            "arm": self.arm.value,
            "fixture_path": self.fixture_path.relative_to(corpus_root).as_posix(),
            "fixture_id": self.fixture_id,
            "record_id": self.record_id,
            "technique_ids": list(self.technique_ids),
            "repetition": self.repetition,
            "order_key": self.order_key,
        }


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid frozen JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"frozen JSON must be an object: {path}")
    return value


def _safe_relative(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"frozen manifest path escapes corpus root: {relative}"
        ) from exc
    return candidate


def validate_frozen_lab_contract(
    *, contract_root: Path, corpus_root: Path
) -> FrozenLabContract:
    """Fail closed unless the frozen benchmark and every corpus byte validate."""

    contract_root = contract_root.resolve()
    corpus_root = corpus_root.resolve()
    benchmark_path = contract_root / "benchmark.yaml"
    try:
        benchmark = yaml.safe_load(benchmark_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("invalid frozen benchmark.yaml") from exc
    if not isinstance(benchmark, dict):
        raise ValueError("benchmark.yaml must decode to an object")
    catalog = _load_json(contract_root / "technique-catalog.json")
    corpus_manifest = _load_json(contract_root / "corpus-manifest.json")
    system_card = _load_json(contract_root / "system-card.json")
    index = _load_json(corpus_root / "index.json")

    if benchmark.get("schema") != "compand.ces1.benchmark.v1":
        raise ValueError("unexpected benchmark schema")
    if benchmark.get("status") != "frozen_contract":
        raise ValueError("benchmark is not frozen")
    if set(benchmark.get("arms") or {}) != {arm.value for arm in AblationArm}:
        raise ValueError("benchmark arm vocabulary drifted")
    scope = benchmark.get("scope") or {}
    if scope.get("confirmatory_traffic_allowed") is not False:
        raise ValueError("QA-57 may not authorize confirmatory traffic")
    if scope.get("production_promotion_authorized") is not False:
        raise ValueError("QA-57 may not authorize production promotion")
    if not scope.get("confirmatory_blockers"):
        raise ValueError("frozen confirmatory blockers disappeared")

    techniques = catalog.get("techniques")
    if not isinstance(techniques, list) or not techniques:
        raise ValueError("technique catalog is empty")
    technique_ids = [str(item.get("id") or "") for item in techniques]
    if "" in technique_ids or len(technique_ids) != len(set(technique_ids)):
        raise ValueError("technique catalog IDs are missing or duplicated")

    checksum_path = corpus_root / "CHECKSUMS"
    expected_checksums: dict[str, str] = {}
    try:
        checksum_lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError("corpus CHECKSUMS is missing") from exc
    for line in checksum_lines:
        pieces = line.split("  ", 1)
        if len(pieces) != 2 or len(pieces[0]) != 64:
            raise ValueError("malformed corpus CHECKSUMS entry")
        digest, relative = pieces
        if relative in expected_checksums:
            raise ValueError(f"duplicate corpus checksum: {relative}")
        path = _safe_relative(corpus_root, relative)
        if not path.is_file() or sha256_hex(path.read_bytes()) != digest:
            raise ValueError(f"corpus checksum mismatch: {relative}")
        expected_checksums[relative] = digest
    actual_files = {
        path.relative_to(corpus_root).as_posix()
        for path in corpus_root.rglob("*")
        if path.is_file() and path.name != "CHECKSUMS"
    }
    if actual_files != set(expected_checksums):
        raise ValueError("corpus files and CHECKSUMS inventory differ")

    fixture_paths: list[Path] = []
    for partition_name in ("development", "golden"):
        partition = (index.get("partitions") or {}).get(partition_name) or {}
        fixtures = partition.get("fixtures")
        if not isinstance(fixtures, list):
            raise ValueError(f"corpus index partition is invalid: {partition_name}")
        for entry in fixtures:
            relative = str(entry.get("path") or "")
            path = _safe_relative(corpus_root, relative)
            digest = sha256_hex(path.read_bytes())
            if digest != entry.get("sha256") or digest != expected_checksums.get(
                relative
            ):
                raise ValueError(f"indexed fixture hash mismatch: {relative}")
            fixture = _load_json(path)
            if fixture.get("schema") != "compand.ces1.fixture.v2":
                raise ValueError(f"unexpected fixture schema: {relative}")
            if fixture.get("partition") != partition_name:
                raise ValueError(f"fixture partition mismatch: {relative}")
            if sha256_json(fixture.get("input")) != fixture.get("input_sha256"):
                raise ValueError(f"fixture input hash mismatch: {relative}")
            records = fixture.get("case_records")
            if not isinstance(records, list) or len(records) != fixture.get(
                "case_record_count"
            ):
                raise ValueError(f"fixture case record count mismatch: {relative}")
            for record in records:
                if record.get("schema") != "compand.ces1.case_record.v1":
                    raise ValueError(f"case record schema mismatch: {relative}")
                if sha256_json(record.get("input")) != record.get("input_sha256"):
                    raise ValueError(
                        f"case input hash mismatch: {record.get('record_id')}"
                    )
                if sha256_json(record.get("baseline_provider_view")) != record.get(
                    "baseline_provider_view_sha256"
                ):
                    raise ValueError(
                        f"case baseline hash mismatch: {record.get('record_id')}"
                    )
            fixture_paths.append(path)

    contract_hashes = {
        "benchmark_sha256": sha256_hex(benchmark_path.read_bytes()),
        "catalog_sha256": sha256_hex(
            (contract_root / "technique-catalog.json").read_bytes()
        ),
        "corpus_sha256": sha256_hex((corpus_root / "index.json").read_bytes()),
        "system_card_sha256": sha256_hex(
            (contract_root / "system-card.json").read_bytes()
        ),
    }
    fingerprint_material = {
        "authority": ["ADR-0026/Compand-evidence", "CES-1"],
        **contract_hashes,
        "corpus_root_sha256": index.get("corpus_root_sha256"),
        "confirmatory_traffic_allowed": False,
    }
    return FrozenLabContract(
        contract_root=contract_root,
        corpus_root=corpus_root,
        benchmark=benchmark,
        catalog=catalog,
        corpus_manifest=corpus_manifest,
        system_card=system_card,
        index=index,
        contract_hashes=contract_hashes,
        fixture_paths=tuple(sorted(fixture_paths)),
        config_fingerprint=f"sha256:{sha256_json(fingerprint_material)}",
    )


def _records(
    contract: FrozenLabContract,
) -> Iterable[tuple[Path, Mapping[str, Any], Mapping[str, Any]]]:
    for path in contract.fixture_paths:
        fixture = _load_json(path)
        for record in fixture["case_records"]:
            yield path, fixture, record


def build_ablation_plan(
    contract: FrozenLabContract,
    *,
    technique_ids: Sequence[str],
    repetitions: int = 1,
    combinations: Sequence[Sequence[str]] = (),
    passing_e1: Sequence[Mapping[str, Any]] = (),
) -> tuple[AblationPlanEntry, ...]:
    """Build a deterministic B0/S1/E1 plan and gated C1 extensions."""

    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    selected = tuple(dict.fromkeys(str(item) for item in technique_ids))
    unknown = set(selected) - set(contract.techniques)
    if unknown:
        raise ValueError(f"unknown techniques: {sorted(unknown)}")
    for technique_id in selected:
        if not contract.techniques[technique_id].get("cloud_gateway_enforceable"):
            raise ValueError(
                f"E1 is unsupported for host-only technique: {technique_id}"
            )

    passed = _validated_passing_e1(contract, passing_e1)
    normalized_combinations: list[tuple[str, ...]] = []
    for raw_members in combinations:
        members = tuple(dict.fromkeys(str(item) for item in raw_members))
        if len(members) < 2:
            raise ValueError("C1 requires at least two distinct techniques")
        if set(members) - set(selected):
            raise ValueError("C1 members must be selected E1 techniques")
        missing = set(members) - passed
        if missing:
            raise ValueError(f"C1 requires passing frozen E1 grades: {sorted(missing)}")
        normalized_combinations.append(members)

    entries: list[AblationPlanEntry] = []
    for path, fixture, record in _records(contract):
        dispositions = record.get("expected_disposition_by_technique") or {}
        record_id = str(record["record_id"])
        for technique_id in selected:
            if technique_id not in dispositions:
                continue
            for repetition in range(1, repetitions + 1):
                for arm in (
                    AblationArm.BASELINE,
                    AblationArm.SHADOW,
                    AblationArm.ENFORCED,
                ):
                    # B0 is unchanged, but retains the target technique identity so
                    # paired baselines cannot collide in multi-technique runs.
                    members = (technique_id,)
                    entries.append(
                        _plan_entry(
                            contract,
                            path,
                            fixture,
                            record_id,
                            arm,
                            members,
                            repetition,
                        )
                    )
        for members in normalized_combinations:
            if not set(members).issubset(dispositions):
                continue
            for repetition in range(1, repetitions + 1):
                entries.append(
                    _plan_entry(
                        contract,
                        path,
                        fixture,
                        record_id,
                        AblationArm.COMBINATION,
                        members,
                        repetition,
                    )
                )
    return tuple(sorted(entries, key=lambda item: (item.order_key, item.plan_id)))


def _validated_passing_e1(
    contract: FrozenLabContract,
    scorecards: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Return technique IDs backed by hash-valid, contract-bound E1 scorecards."""

    passed: set[str] = set()
    for scorecard in scorecards:
        if not isinstance(scorecard, Mapping):
            raise ValueError("C1 passing E1 evidence must be scorecards")
        if scorecard.get("schema") != "compand.ces1.public_scorecard.v1":
            raise ValueError("C1 passing E1 evidence has an unexpected schema")
        technique = scorecard.get("technique")
        if not isinstance(technique, Mapping):
            raise ValueError("C1 passing E1 scorecard is missing technique identity")
        technique_id = str(technique.get("id") or "")
        if technique_id not in contract.techniques:
            raise ValueError(f"C1 passing E1 scorecard names unknown technique: {technique_id}")
        expected_version = str(contract.techniques[technique_id].get("version") or "")
        if technique.get("arm") != AblationArm.ENFORCED.value:
            raise ValueError("C1 passing evidence must be an individual E1 scorecard")
        if str(technique.get("version") or "") != expected_version:
            raise ValueError(
                f"C1 E1 scorecard version drifted for technique: {technique_id}"
            )
        hard_gates = scorecard.get("hard_gates")
        if not isinstance(hard_gates, Mapping) or set(hard_gates) != set(HARD_GATE_IDS):
            raise ValueError("C1 E1 scorecard hard gates drifted")
        if not all(
            isinstance(result, Mapping) and result.get("passed") is True
            for result in hard_gates.values()
        ):
            raise ValueError(f"C1 E1 scorecard did not pass alone: {technique_id}")
        grades = scorecard.get("grades")
        if not isinstance(grades, Mapping) or grades.get("hard_gate_grade") != "pass":
            raise ValueError(f"C1 E1 scorecard has no passing grade: {technique_id}")
        trace = scorecard.get("trace")
        if not isinstance(trace, Mapping):
            raise ValueError("C1 E1 scorecard is missing frozen-contract trace")
        for field, expected in contract.contract_hashes.items():
            if trace.get(field) != expected:
                raise ValueError(
                    f"C1 E1 scorecard contract trace drifted for technique: {technique_id}"
                )
        if trace.get("scorecard_sha256") != scorecard_sha256(scorecard):
            raise ValueError(f"C1 E1 scorecard hash mismatch: {technique_id}")
        passed.add(technique_id)
    return passed


def _plan_entry(
    contract: FrozenLabContract,
    path: Path,
    fixture: Mapping[str, Any],
    record_id: str,
    arm: AblationArm,
    technique_ids: tuple[str, ...],
    repetition: int,
) -> AblationPlanEntry:
    semantic = {
        "config_fingerprint": contract.config_fingerprint,
        "fixture_id": fixture["fixture_id"],
        "record_id": record_id,
        "arm": arm.value,
        "technique_ids": list(technique_ids),
        "repetition": repetition,
    }
    digest = sha256_json(semantic)
    order_key = sha256_json(
        {
            "seed": contract.benchmark["design"]["order_seed"],
            "record_id": record_id,
            "repetition": repetition,
            "arm": arm.value,
            "technique_ids": list(technique_ids),
        }
    )
    return AblationPlanEntry(
        plan_id=f"plan-{digest}",
        arm=arm,
        fixture_path=path,
        fixture_id=str(fixture["fixture_id"]),
        record_id=record_id,
        technique_ids=technique_ids,
        repetition=repetition,
        order_key=order_key,
    )


def make_score_input_event(
    *,
    run_id: str,
    arm: AblationArm,
    technique_id: str,
    technique_version: str,
    candidate_id: str,
    input_hash: str,
    output_hash: str,
    parent_event_id: str | None,
    config_fingerprint: str,
    sequence: int,
    provider_usage: Mapping[str, int | float | None],
    evaluator_version: str,
    task: Mapping[str, Any],
    hard_gates: Mapping[str, bool],
    grade_components: Mapping[str, Mapping[str, int | float]],
) -> dict[str, Any]:
    """Create one fully attributable append-only score input event."""

    if sequence < 1:
        raise ValueError("score input sequence must be positive")
    missing_usage = set(_REQUIRED_USAGE_FIELDS) - set(provider_usage)
    if missing_usage:
        raise ValueError(f"provider usage fields missing: {sorted(missing_usage)}")
    if set(hard_gates) != set(HARD_GATE_IDS):
        raise ValueError("score input must name every frozen hard gate")
    if set(grade_components) != set(GRADE_WEIGHTS):
        raise ValueError("score input must name every separate grade")
    for name, values in grade_components.items():
        unknown = set(values) - set(GRADE_WEIGHTS[name])
        if unknown:
            raise ValueError(f"unknown {name} grade components: {sorted(unknown)}")
    semantic: dict[str, Any] = {
        "schema": _SCORE_EVENT_SCHEMA,
        "run_id": run_id,
        "arm": arm.value,
        "technique_id": technique_id,
        "technique_version": technique_version,
        "candidate_id": candidate_id,
        "input_hash": input_hash,
        "output_hash": output_hash,
        "parent_event_id": parent_event_id,
        "config_fingerprint": config_fingerprint,
        "sequence": sequence,
        "provider_usage": dict(provider_usage),
        "evaluator_version": evaluator_version,
        "task": dict(task),
        "hard_gates": dict(hard_gates),
        "grade_components": {
            name: dict(values) for name, values in grade_components.items()
        },
    }
    semantic["event_id"] = f"sha256:{sha256_json(semantic)}"
    return semantic


def load_plan_case(entry: AblationPlanEntry) -> tuple[bytes, Mapping[str, Any]]:
    """Load exactly one frozen case record as deterministic plugin input."""

    fixture = _load_json(entry.fixture_path)
    matches = [
        record
        for record in fixture.get("case_records") or []
        if record.get("record_id") == entry.record_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"plan case record is missing or duplicated: {entry.record_id}"
        )
    record = matches[0]
    return canonical_json_bytes(record), record


def fixture_oracle_score_event(
    *,
    contract: FrozenLabContract,
    entry: AblationPlanEntry,
    result: LabRunResult,
    replay_events: Sequence[Mapping[str, Any]],
    technique_version: str,
    evaluator_version: str = "compand-fixture-oracle-v1",
    config_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Turn one replay and frozen case oracle into an attributable score input.

    Frozen fixtures do not contain provider billing truth.  Usage fields therefore
    remain ``None`` and the economic gate stays red instead of inventing zero cost.
    """

    _, record = load_plan_case(entry)
    technique_id = entry.technique_ids[0]
    expected = str(
        (record.get("expected_disposition_by_technique") or {}).get(technique_id) or ""
    )
    if not expected:
        raise ValueError("plan technique has no frozen case oracle")
    transformed = result.output_hash != result.input_hash
    if entry.arm is AblationArm.BASELINE:
        correctness = result.status == "succeeded" and not transformed
    elif entry.arm is AblationArm.SHADOW:
        correctness = result.status in {"succeeded", "declined"} and not transformed
    elif expected == "transform":
        correctness = result.status == "succeeded" and transformed
    elif expected.startswith("decline_") or expected == "no_material_opportunity":
        correctness = result.status == "declined" and not transformed
    else:
        correctness = result.status in {"declined", "failed"}
    negative_oracle = expected in {
        "detect_corruption",
        "fail_recovery",
        "reject_cross_scope_access",
    }
    expected_outcome = record.get("expected_outcome") or {}
    verified_success = bool(expected_outcome.get("verified_task_success"))
    output_is_baseline = result.output_hash == result.input_hash
    hard_gates = {
        "correctness": correctness,
        "attribution": len(entry.technique_ids) == 1,
        "isolation": (not negative_oracle) or result.status != "succeeded",
        "reproducibility": result.status != "failed",
        "protocol_safety": result.status != "failed" or output_is_baseline,
        "exact_recovery": result.status != "failed" or output_is_baseline,
        "fail_open": result.status != "failed" or output_is_baseline,
        "whole_task_economics": False,
        "quality_noninferiority": True,
        "clean_environment_regeneration": False,
    }
    success_ratio = 1.0 if correctness else 0.0
    transparency_ratio = 1.0 if replay_events else 0.0
    grade_components = {
        "technical": {
            "correctness": success_ratio,
            "reproducibility": float(hard_gates["reproducibility"]),
            "failure_transparency": transparency_ratio,
            "latency_reliability": float(result.status != "failed"),
            "simplicity": 1.0,
        },
        "user_value": {
            "net_cost_per_verified_task": 0.0,
            "natural_eligible_spend_coverage": 0.0,
            "outcome_noninferiority": 1.0,
            "latency_friction": 0.0,
        },
        "company_value": {
            "defensible_evidence": transparency_ratio,
            "margin_potential": 0.0,
            "cross_lane_applicability": 0.0,
            "operations_burden": 1.0,
            "ip_design_around_evidence": 0.0,
        },
        "asset_value": {
            "certified_reusable_profile": 0.0,
            "failure_and_rollback_corpus": float(negative_oracle),
            "cross_lane_calibration": 0.0,
            "reproducible_evidence_compiler": 1.0,
            "ip_prior_art_record": 0.0,
        },
    }
    pair_id = f"pair-{sha256_json({'record_id': entry.record_id, 'technique_id': technique_id, 'repetition': entry.repetition})}"
    parent_event_id = str(replay_events[-1]["event_id"]) if replay_events else None
    candidate_id = ""
    if replay_events:
        candidate_id = str(replay_events[-1].get("candidate_id") or "")
    return make_score_input_event(
        run_id=result.run_id,
        arm=entry.arm,
        technique_id=technique_id,
        technique_version=technique_version,
        candidate_id=candidate_id,
        input_hash=result.input_hash,
        output_hash=result.output_hash,
        parent_event_id=parent_event_id,
        config_fingerprint=config_fingerprint or contract.config_fingerprint,
        sequence=len(replay_events) + 1,
        provider_usage={field: None for field in _REQUIRED_USAGE_FIELDS},
        evaluator_version=evaluator_version,
        task={
            "task_id": entry.record_id,
            "pair_id": pair_id,
            "repetition": entry.repetition,
            "verified_completed": expected_outcome.get("status")
            == "verified_completed",
            "verified_task_success": verified_success,
            "latency_ms": None,
            "reliable": result.status != "failed",
            "exact_recovery": bool(hard_gates["exact_recovery"]),
            "infrastructure_invalid": False,
            "expected_disposition": expected,
            "observed_disposition": result.reason_code,
        },
        hard_gates=hard_gates,
        grade_components=grade_components,
    )


def verify_score_input_event(event: Mapping[str, Any]) -> None:
    if event.get("schema") != _SCORE_EVENT_SCHEMA:
        raise ValueError("unexpected score input schema")
    event_id = event.get("event_id")
    semantic = dict(event)
    semantic.pop("event_id", None)
    if event_id != f"sha256:{sha256_json(semantic)}":
        raise ValueError("score input event hash mismatch")
    for field in (
        "run_id",
        "arm",
        "technique_id",
        "technique_version",
        "candidate_id",
        "input_hash",
        "output_hash",
        "config_fingerprint",
        "sequence",
        "provider_usage",
        "evaluator_version",
    ):
        if field not in event:
            raise ValueError(f"score input trace is missing {field}")
    usage = event.get("provider_usage")
    if not isinstance(usage, dict) or set(_REQUIRED_USAGE_FIELDS) - set(usage):
        raise ValueError("score input provider usage is incomplete")
    if set(event.get("hard_gates") or {}) != set(HARD_GATE_IDS):
        raise ValueError("score input hard gates drifted")


class MechanicalGrader:
    """Derive hard gates, paired effects, tails, and four grades from events."""

    def grade(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        technique_id: str,
        technique_version: str,
        arm: AblationArm = AblationArm.ENFORCED,
    ) -> dict[str, Any]:
        score_events = [
            dict(event)
            for event in events
            if event.get("schema") == _SCORE_EVENT_SCHEMA
        ]
        if not score_events:
            raise ValueError("no score input events")
        for event in score_events:
            verify_score_input_event(event)

        baseline_by_pair: dict[str, dict[str, Any]] = {}
        treated_by_pair: dict[str, dict[str, Any]] = {}
        for event in score_events:
            task = event.get("task") or {}
            pair_id = str(task.get("pair_id") or "")
            if not pair_id:
                raise ValueError("score input task.pair_id is required")
            if event["arm"] == AblationArm.BASELINE.value:
                if pair_id in baseline_by_pair:
                    raise ValueError(f"duplicate B0 pair: {pair_id}")
                baseline_by_pair[pair_id] = event
            elif (
                event["arm"] == arm.value
                and event["technique_id"] == technique_id
                and event["technique_version"] == technique_version
            ):
                if pair_id in treated_by_pair:
                    raise ValueError(f"duplicate {arm.value} pair: {pair_id}")
                treated_by_pair[pair_id] = event
        pair_ids = sorted(set(baseline_by_pair) & set(treated_by_pair))
        if not pair_ids:
            raise ValueError("no complete B0/treated task pairs")

        task_rows: list[dict[str, Any]] = []
        cost_differences: list[float] = []
        quality_differences: list[float] = []
        latency_differences: list[float] = []
        reliability_differences: list[float] = []
        recovery_differences: list[float] = []
        treated_costs: list[float] = []
        for pair_id in pair_ids:
            baseline = baseline_by_pair[pair_id]
            treated = treated_by_pair[pair_id]
            row = self._task_row(pair_id, baseline, treated)
            task_rows.append(row)
            if row["paired_cost_difference_usd"] is not None:
                cost_differences.append(row["paired_cost_difference_usd"])
            if row["treated_cost_usd"] is not None:
                treated_costs.append(row["treated_cost_usd"])
            quality_differences.append(row["paired_success_difference"])
            if row["paired_latency_difference_ms"] is not None:
                latency_differences.append(row["paired_latency_difference_ms"])
            reliability_differences.append(row["paired_reliability_difference"])
            recovery_differences.append(row["paired_recovery_difference"])

        evidence_ids = sorted(str(event["event_id"]) for event in score_events)
        gate_results: dict[str, dict[str, Any]] = {}
        treated_events = [treated_by_pair[pair_id] for pair_id in pair_ids]
        for gate_id in HARD_GATE_IDS:
            passed = all(
                event["hard_gates"].get(gate_id) is True for event in treated_events
            )
            reason: str | None = None
            if gate_id == "whole_task_economics":
                complete_economics = len(cost_differences) == len(pair_ids)
                passed = complete_economics and statistics.fmean(cost_differences) < 0
                if not complete_economics:
                    reason = "provider usage or complete-task cost is missing"
                elif not passed:
                    reason = "paired whole-task economics are non-positive"
            elif gate_id == "quality_noninferiority":
                _, lower, _ = interval_95(quality_differences)
                passed = lower is not None and lower >= -0.05
                if not passed:
                    reason = "paired quality lower bound is below -0.05"
            elif not passed:
                reason = (
                    "one or more attributable score inputs failed or omitted the gate"
                )
            gate_results[gate_id] = {
                "passed": passed,
                "evidence_ids": evidence_ids,
                "reason": reason,
            }
        all_gates_pass = all(item["passed"] for item in gate_results.values())

        component_ratios: dict[str, dict[str, float]] = {}
        for grade_name, weights in GRADE_WEIGHTS.items():
            component_ratios[grade_name] = {
                component: statistics.fmean(
                    float(
                        event["grade_components"]
                        .get(grade_name, {})
                        .get(component, 0.0)
                    )
                    for event in treated_events
                )
                for component in weights
            }
        grades = {
            name: weighted_grade(name, values, hard_gate_passed=all_gates_pass)
            for name, values in component_ratios.items()
        }
        grades["hard_gate_grade"] = "pass" if all_gates_pass else "F"

        effects = {
            "net_cost_per_verified_task": _effect(
                cost_differences, "usd_per_verified_task"
            ),
            "verified_task_success": _effect(quality_differences, "ratio"),
            "latency_p95": _effect(latency_differences, "milliseconds"),
            "reliability": _effect(reliability_differences, "ratio"),
            "exact_recovery": _effect(recovery_differences, "ratio"),
        }
        failures = sum(
            not bool(event.get("task", {}).get("verified_task_success"))
            for event in treated_events
        )
        missing = sum(
            self._total_cost(event) is None
            or event.get("task", {}).get("verified_completed") is None
            for event in treated_events
        )
        return {
            "schema": "compand.ces1.mechanical_grade.v1",
            "technique": {
                "id": technique_id,
                "version": technique_version,
                "arm": arm.value,
            },
            "sample": {
                "task_pairs": len(pair_ids),
                "independent_repetitions": len(
                    {
                        event.get("task", {}).get("repetition")
                        for event in treated_events
                    }
                ),
                "failures": failures,
                "invalid_attempts": sum(
                    bool(event.get("task", {}).get("infrastructure_invalid"))
                    for event in treated_events
                ),
                "exclusions": 0,
                "missing": missing,
            },
            "effects": effects,
            "severe_tails": {
                "treated_cost_p95_usd": percentile(treated_costs, 0.95),
                "treated_cost_max_usd": max(treated_costs) if treated_costs else None,
                "paired_cost_difference_p95_usd": percentile(cost_differences, 0.95),
                "paired_latency_difference_p95_ms": percentile(
                    latency_differences, 0.95
                ),
            },
            "hard_gates": gate_results,
            "grades": grades,
            "task_level": task_rows,
            "event_ids": evidence_ids,
            "run_ids": sorted({str(event["run_id"]) for event in score_events}),
            "event_root_sha256": sha256_json(evidence_ids),
            "all_hard_gates_passed": all_gates_pass,
        }

    @staticmethod
    def _total_cost(event: Mapping[str, Any]) -> float | None:
        usage = event.get("provider_usage") or {}
        provider = usage.get("provider_charge_usd")
        overhead = usage.get("compand_overhead_usd")
        if provider is None or overhead is None:
            return None
        total = float(provider) + float(overhead)
        return total if math.isfinite(total) and total >= 0 else None

    def _task_row(
        self,
        pair_id: str,
        baseline: Mapping[str, Any],
        treated: Mapping[str, Any],
    ) -> dict[str, Any]:
        baseline_task = baseline.get("task") or {}
        treated_task = treated.get("task") or {}
        baseline_cost = self._total_cost(baseline)
        treated_cost = self._total_cost(treated)
        baseline_success = bool(baseline_task.get("verified_task_success"))
        treated_success = bool(treated_task.get("verified_task_success"))
        baseline_latency = baseline_task.get("latency_ms")
        treated_latency = treated_task.get("latency_ms")
        latency_difference = None
        if baseline_latency is not None and treated_latency is not None:
            latency_difference = float(treated_latency) - float(baseline_latency)
        return {
            "pair_id": pair_id,
            "task_id": str(
                treated_task.get("task_id") or baseline_task.get("task_id") or ""
            ),
            "repetition": treated_task.get("repetition"),
            "baseline_event_id": baseline["event_id"],
            "treated_event_id": treated["event_id"],
            "baseline_cost_usd": baseline_cost,
            "treated_cost_usd": treated_cost,
            "paired_cost_difference_usd": (
                None
                if baseline_cost is None or treated_cost is None
                else treated_cost - baseline_cost
            ),
            "paired_success_difference": float(treated_success)
            - float(baseline_success),
            "paired_latency_difference_ms": latency_difference,
            "paired_reliability_difference": float(bool(treated_task.get("reliable")))
            - float(bool(baseline_task.get("reliable"))),
            "paired_recovery_difference": float(
                bool(treated_task.get("exact_recovery"))
            )
            - float(bool(baseline_task.get("exact_recovery"))),
        }


def _effect(values: Sequence[float], unit: str) -> dict[str, int | float | None | str]:
    estimate, lower, upper = interval_95(values)
    return {
        "estimate": estimate,
        "lower_95": lower,
        "upper_95": upper,
        "unit": unit,
        "numerator": float(sum(values)),
        "denominator": len(values),
    }


def public_scorecard(
    grade: Mapping[str, Any],
    *,
    release_id: str,
    certification_tuple: Mapping[str, str],
    contract: FrozenLabContract,
    claim: str,
    clean_environment_regenerated: bool,
) -> dict[str, Any]:
    """Compile one schema-shaped scorecard without overstating evidence."""

    hard_gates = json.loads(json.dumps(grade["hard_gates"]))
    if not clean_environment_regenerated:
        hard_gates["clean_environment_regeneration"] = {
            "passed": False,
            "evidence_ids": list(grade["event_ids"]),
            "reason": "clean-environment regeneration has not been attested",
        }
    passed = all(item["passed"] for item in hard_gates.values())
    grades = json.loads(json.dumps(grade["grades"]))
    if not passed:
        grades["hard_gate_grade"] = "F"
        for name in GRADE_WEIGHTS:
            grades[name]["band"] = "F"
    scorecard: dict[str, Any] = {
        "schema": "compand.ces1.public_scorecard.v1",
        "release_id": release_id,
        "technique": dict(grade["technique"]),
        "certification_tuple": dict(certification_tuple),
        "evidence": {
            "tier": "C1",
            "state": "exploratory",
            "value_states": ["measured"],
            "claim": claim,
        },
        "sample": dict(grade["sample"]),
        "effects": dict(grade["effects"]),
        "hard_gates": hard_gates,
        "grades": grades,
        "kpis": {
            kpi_id: {
                "value": _kpi_value(kpi_id, grade),
                "unit": _kpi_unit(kpi_id),
                "evidence_state": "measured",
                "board_kpi_id": None,
                "movement_allowed": False,
            }
            for kpi_id in KPI_IDS
        },
        "disposition": "shadow_only" if passed else "diagnostic_only",
        "trace": {
            **contract.contract_hashes,
            "run_ids": list(grade["run_ids"]),
            "event_root_sha256": grade["event_root_sha256"],
            "scorecard_sha256": "",
        },
    }
    scorecard["trace"]["scorecard_sha256"] = scorecard_sha256(scorecard)
    return scorecard


def _kpi_value(kpi_id: str, grade: Mapping[str, Any]) -> float | None:
    effects = grade["effects"]
    mapping = {
        KPI_IDS[0]: effects["net_cost_per_verified_task"]["estimate"],
        KPI_IDS[1]: None,
        KPI_IDS[2]: effects["verified_task_success"]["estimate"],
        KPI_IDS[3]: grade["severe_tails"]["paired_latency_difference_p95_ms"],
        KPI_IDS[4]: effects["reliability"]["estimate"],
        KPI_IDS[5]: effects["exact_recovery"]["estimate"],
    }
    return mapping[kpi_id]


def _kpi_unit(kpi_id: str) -> str:
    return {
        KPI_IDS[0]: "usd_per_verified_task",
        KPI_IDS[1]: "ratio",
        KPI_IDS[2]: "ratio",
        KPI_IDS[3]: "milliseconds",
        KPI_IDS[4]: "ratio",
        KPI_IDS[5]: "ratio",
    }[kpi_id]


__all__ = [
    "AblationPlanEntry",
    "FrozenLabContract",
    "MechanicalGrader",
    "build_ablation_plan",
    "make_score_input_event",
    "fixture_oracle_score_event",
    "load_plan_case",
    "public_scorecard",
    "validate_frozen_lab_contract",
    "verify_score_input_event",
]
