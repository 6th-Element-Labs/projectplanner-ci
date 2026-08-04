"""Run one deterministic Compand technique against one immutable fixture."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Protocol

from switchboard.domain.compand.lab import (
    AppliedTransform,
    DetectionContext,
    EconomicsEstimate,
    LabArm,
    LabStage,
    ReasonCode,
    StageStatus,
    Technique,
    TechniqueCandidate,
    VerificationProof,
    candidate_id_for,
    sha256_evidence,
)


_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")


class LabRunWriter(Protocol):
    run_location: str

    def append_event(self, event: Mapping[str, object]) -> None: ...


class LabEvidenceStore(Protocol):
    def put_object(self, value: bytes) -> str: ...

    def begin_run(
        self, run_id: str, manifest: Mapping[str, object]
    ) -> LabRunWriter: ...


class LabClock(Protocol):
    def recorded_at(self) -> str: ...

    def monotonic_ns(self) -> int: ...


class SystemLabClock:
    def recorded_at(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


@dataclass(frozen=True)
class LabRunResult:
    run_id: str
    status: str
    reason_code: str
    arm: str
    technique_id: str
    technique_version: str
    input_hash: str
    output_hash: str
    run_location: str

    def as_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "arm": self.arm,
            "technique_id": self.technique_id,
            "technique_version": self.technique_version,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "run_location": self.run_location,
        }


class _EventRecorder:
    def __init__(
        self,
        *,
        writer: LabRunWriter,
        clock: LabClock,
        run_id: str,
        arm: LabArm,
        technique: Technique,
        candidate_id: str,
        input_hash: str,
        model_fingerprint: str,
        config_fingerprint: str,
    ) -> None:
        self.writer = writer
        self.clock = clock
        self.run_id = run_id
        self.arm = arm
        self.technique = technique
        self.candidate_id = candidate_id
        self.input_hash = input_hash
        self.model_fingerprint = model_fingerprint
        self.config_fingerprint = config_fingerprint
        self.sequence = 0
        self.parent_event_id: str | None = None

    def record(
        self,
        *,
        stage: LabStage,
        status: StageStatus,
        reason_code: ReasonCode,
        output_hash: str,
        elapsed_ms: float,
        details: Mapping[str, int | float | str | bool | None] | None = None,
        error_type: str | None = None,
    ) -> None:
        self.sequence += 1
        semantic = {
            "sequence": self.sequence,
            "arm": self.arm.value,
            "technique_id": self.technique.technique_id,
            "technique_version": self.technique.technique_version,
            "candidate_id": self.candidate_id,
            "input_hash": self.input_hash,
            "output_hash": output_hash,
            "parent_event_id": self.parent_event_id,
            "model_fingerprint": self.model_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "stage": stage.value,
            "status": status.value,
            "reason_code": reason_code.value,
            "details": dict(details or {}),
            "error_type": error_type,
        }
        encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        event_id = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        event = {
            "schema": "compand.lab.event.v1",
            "event_id": event_id,
            "run_id": self.run_id,
            **semantic,
            "elapsed_ms": round(elapsed_ms, 3),
            "recorded_at": self.clock.recorded_at(),
            "severity": (
                "red"
                if status is StageStatus.FAILED
                else "green"
                if status is StageStatus.SUCCEEDED
                else "gray"
            ),
        }
        self.writer.append_event(event)
        self.parent_event_id = event_id


def fingerprint_label(value: str) -> str:
    """Hash one declared model/config label so evidence does not retain its text."""

    if not isinstance(value, str) or not value:
        raise ValueError("fingerprint label must be non-empty")
    return sha256_evidence(value.encode("utf-8"))


def run_single_technique(
    *,
    fixture: bytes,
    fixture_id: str,
    arm: LabArm,
    technique: Technique,
    model_fingerprint: str,
    config_fingerprint: str,
    evidence_store: LabEvidenceStore,
    clock: LabClock | None = None,
    run_id_factory: Callable[[], str] | None = None,
) -> LabRunResult:
    """Execute one plugin with no retries and persist only content-free events."""

    for name, value in (
        ("model_fingerprint", model_fingerprint),
        ("config_fingerprint", config_fingerprint),
    ):
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ValueError(f"{name} must be canonical sha256 evidence")
    if not fixture_id:
        raise ValueError("fixture_id is required")
    clock = clock or SystemLabClock()
    run_id_factory = run_id_factory or (lambda: f"run-{uuid.uuid4().hex}")
    run_id = run_id_factory()
    input_hash = sha256_evidence(fixture)
    baseline_object_hash = evidence_store.put_object(fixture)
    if baseline_object_hash != input_hash:
        raise ValueError("content store returned a mismatched baseline hash")
    candidate_id = candidate_id_for(
        technique.technique_id, technique.technique_version, input_hash
    )
    manifest = {
        "schema": "compand.lab.run_manifest.v1",
        "run_id": run_id,
        "fixture_id": fixture_id,
        "arm": arm.value,
        "technique": {
            "id": technique.technique_id,
            "version": technique.technique_version,
            "candidate_id": candidate_id,
        },
        "input_hash": input_hash,
        "baseline_object_hash": baseline_object_hash,
        "model_fingerprint": model_fingerprint,
        "config_fingerprint": config_fingerprint,
        "created_at": clock.recorded_at(),
        "comparison_exclusions": ["run_id", "recorded_at", "elapsed_ms"],
        "authority": {
            "evidence_tier_ceiling": "C1",
            "evidence_state": "exploratory",
            "confirmatory_traffic_authorized": False,
            "production_promotion_authorized": False,
        },
    }
    writer = evidence_store.begin_run(run_id, manifest)
    recorder = _EventRecorder(
        writer=writer,
        clock=clock,
        run_id=run_id,
        arm=arm,
        technique=technique,
        candidate_id=candidate_id,
        input_hash=input_hash,
        model_fingerprint=model_fingerprint,
        config_fingerprint=config_fingerprint,
    )

    if arm is LabArm.BASELINE:
        for stage in LabStage:
            started = clock.monotonic_ns()
            recorder.record(
                stage=stage,
                status=StageStatus.STARTED,
                reason_code=ReasonCode.STAGE_STARTED,
                output_hash=input_hash,
                elapsed_ms=0,
            )
            recorder.record(
                stage=stage,
                status=StageStatus.DECLINED,
                reason_code=ReasonCode.BASELINE_ARM,
                output_hash=input_hash,
                elapsed_ms=_elapsed_ms(started, clock),
            )
        return _result(
            writer,
            run_id,
            arm,
            technique,
            input_hash,
            input_hash,
            "succeeded",
            ReasonCode.BASELINE_ARM,
        )

    context = DetectionContext(
        fixture_id=fixture_id, input_hash=input_hash, original=fixture
    )
    started = clock.monotonic_ns()
    recorder.record(
        stage=LabStage.DETECT,
        status=StageStatus.STARTED,
        reason_code=ReasonCode.STAGE_STARTED,
        output_hash=input_hash,
        elapsed_ms=0,
    )
    try:
        candidates = tuple(technique.detect(context))
    except Exception as exc:  # plugin boundary: failure is evidence, never a retry
        recorder.record(
            stage=LabStage.DETECT,
            status=StageStatus.FAILED,
            reason_code=ReasonCode.TECHNIQUE_FAILURE,
            output_hash=input_hash,
            elapsed_ms=_elapsed_ms(started, clock),
            error_type=type(exc).__name__,
        )
        return _result(
            writer,
            run_id,
            arm,
            technique,
            input_hash,
            input_hash,
            "failed",
            ReasonCode.TECHNIQUE_FAILURE,
        )
    if not candidates:
        recorder.record(
            stage=LabStage.DETECT,
            status=StageStatus.DECLINED,
            reason_code=ReasonCode.NO_CANDIDATE,
            output_hash=input_hash,
            elapsed_ms=_elapsed_ms(started, clock),
            details={"candidate_count": 0},
        )
        return _result(
            writer,
            run_id,
            arm,
            technique,
            input_hash,
            input_hash,
            "declined",
            ReasonCode.NO_CANDIDATE,
        )
    try:
        if not all(isinstance(item, TechniqueCandidate) for item in candidates):
            raise TypeError("detector returned a non-candidate")
        candidate = sorted(candidates, key=lambda item: item.candidate_id)[0]
        if (
            candidate.technique_id != technique.technique_id
            or candidate.technique_version != technique.technique_version
            or candidate.input_hash != input_hash
        ):
            raise ValueError("candidate attribution does not match the run")
    except (AttributeError, TypeError, ValueError) as exc:
        recorder.record(
            stage=LabStage.DETECT,
            status=StageStatus.FAILED,
            reason_code=ReasonCode.TECHNIQUE_CONTRACT_VIOLATION,
            output_hash=input_hash,
            elapsed_ms=_elapsed_ms(started, clock),
            error_type=type(exc).__name__,
        )
        return _result(
            writer,
            run_id,
            arm,
            technique,
            input_hash,
            input_hash,
            "failed",
            ReasonCode.TECHNIQUE_CONTRACT_VIOLATION,
        )
    recorder.candidate_id = candidate.candidate_id
    recorder.record(
        stage=LabStage.DETECT,
        status=StageStatus.SUCCEEDED,
        reason_code=ReasonCode.STAGE_SUCCEEDED,
        output_hash=input_hash,
        elapsed_ms=_elapsed_ms(started, clock),
        details={"candidate_count": len(candidates)},
    )

    started = clock.monotonic_ns()
    recorder.record(
        stage=LabStage.ESTIMATE,
        status=StageStatus.STARTED,
        reason_code=ReasonCode.STAGE_STARTED,
        output_hash=input_hash,
        elapsed_ms=0,
    )
    try:
        estimate = technique.estimate(candidate)
        if not isinstance(estimate, EconomicsEstimate):
            raise TypeError("estimator returned an invalid contract")
        if (
            estimate.input_bytes < 0
            or estimate.output_bytes < 0
            or estimate.byte_delta != estimate.input_bytes - estimate.output_bytes
            or not isinstance(estimate.reason_code, ReasonCode)
        ):
            raise ValueError("estimator returned inconsistent economics")
    except Exception as exc:
        recorder.record(
            stage=LabStage.ESTIMATE,
            status=StageStatus.FAILED,
            reason_code=(
                ReasonCode.TECHNIQUE_CONTRACT_VIOLATION
                if isinstance(exc, (TypeError, ValueError))
                else ReasonCode.TECHNIQUE_FAILURE
            ),
            output_hash=input_hash,
            elapsed_ms=_elapsed_ms(started, clock),
            error_type=type(exc).__name__,
        )
        return _result(
            writer,
            run_id,
            arm,
            technique,
            input_hash,
            input_hash,
            "failed",
            ReasonCode.TECHNIQUE_CONTRACT_VIOLATION
            if isinstance(exc, (TypeError, ValueError))
            else ReasonCode.TECHNIQUE_FAILURE,
        )
    estimate_status = (
        StageStatus.SUCCEEDED if estimate.should_apply else StageStatus.DECLINED
    )
    recorder.record(
        stage=LabStage.ESTIMATE,
        status=estimate_status,
        reason_code=estimate.reason_code,
        output_hash=input_hash,
        elapsed_ms=_elapsed_ms(started, clock),
        details={
            "input_bytes": estimate.input_bytes,
            "output_bytes": estimate.output_bytes,
            "byte_delta": estimate.byte_delta,
            "should_apply": estimate.should_apply,
        },
    )
    if not estimate.should_apply:
        return _result(
            writer,
            run_id,
            arm,
            technique,
            input_hash,
            input_hash,
            "declined",
            estimate.reason_code,
        )

    if arm is LabArm.SHADOW:
        for stage in (LabStage.APPLY, LabStage.VERIFY):
            started = clock.monotonic_ns()
            recorder.record(
                stage=stage,
                status=StageStatus.STARTED,
                reason_code=ReasonCode.STAGE_STARTED,
                output_hash=input_hash,
                elapsed_ms=0,
            )
            recorder.record(
                stage=stage,
                status=StageStatus.DECLINED,
                reason_code=ReasonCode.SHADOW_ARM,
                output_hash=input_hash,
                elapsed_ms=_elapsed_ms(started, clock),
            )
        return _result(
            writer,
            run_id,
            arm,
            technique,
            input_hash,
            input_hash,
            "succeeded",
            ReasonCode.SHADOW_ARM,
        )

    started = clock.monotonic_ns()
    recorder.record(
        stage=LabStage.APPLY,
        status=StageStatus.STARTED,
        reason_code=ReasonCode.STAGE_STARTED,
        output_hash=input_hash,
        elapsed_ms=0,
    )
    try:
        applied = technique.apply(candidate)
        if not isinstance(applied, AppliedTransform):
            raise TypeError("apply returned an invalid contract")
        if not isinstance(applied.transformed, bytes) or not isinstance(
            applied.recovered, bytes
        ):
            raise TypeError("apply byte outputs are invalid")
    except Exception as exc:
        recorder.record(
            stage=LabStage.APPLY,
            status=StageStatus.FAILED,
            reason_code=(
                ReasonCode.TECHNIQUE_CONTRACT_VIOLATION
                if isinstance(exc, TypeError)
                else ReasonCode.TECHNIQUE_FAILURE
            ),
            output_hash=input_hash,
            elapsed_ms=_elapsed_ms(started, clock),
            error_type=type(exc).__name__,
        )
        return _result(
            writer,
            run_id,
            arm,
            technique,
            input_hash,
            input_hash,
            "failed",
            ReasonCode.TECHNIQUE_CONTRACT_VIOLATION
            if isinstance(exc, TypeError)
            else ReasonCode.TECHNIQUE_FAILURE,
        )
    transformed_hash = sha256_evidence(applied.transformed)
    try:
        stored_hash = evidence_store.put_object(applied.transformed)
    except Exception as exc:
        recorder.record(
            stage=LabStage.APPLY,
            status=StageStatus.FAILED,
            reason_code=ReasonCode.EVIDENCE_WRITE_FAILED,
            output_hash=transformed_hash,
            elapsed_ms=_elapsed_ms(started, clock),
            error_type=type(exc).__name__,
        )
        return _result(
            writer,
            run_id,
            arm,
            technique,
            input_hash,
            input_hash,
            "failed",
            ReasonCode.EVIDENCE_WRITE_FAILED,
        )
    if stored_hash != transformed_hash:
        recorder.record(
            stage=LabStage.APPLY,
            status=StageStatus.FAILED,
            reason_code=ReasonCode.EVIDENCE_WRITE_FAILED,
            output_hash=transformed_hash,
            elapsed_ms=_elapsed_ms(started, clock),
        )
        return _result(
            writer,
            run_id,
            arm,
            technique,
            input_hash,
            input_hash,
            "failed",
            ReasonCode.EVIDENCE_WRITE_FAILED,
        )
    recorder.record(
        stage=LabStage.APPLY,
        status=StageStatus.SUCCEEDED,
        reason_code=ReasonCode.STAGE_SUCCEEDED,
        output_hash=transformed_hash,
        elapsed_ms=_elapsed_ms(started, clock),
        details={"output_bytes": len(applied.transformed)},
    )

    started = clock.monotonic_ns()
    recorder.record(
        stage=LabStage.VERIFY,
        status=StageStatus.STARTED,
        reason_code=ReasonCode.STAGE_STARTED,
        output_hash=transformed_hash,
        elapsed_ms=0,
    )
    try:
        proof = technique.verify(fixture, applied.transformed, applied.recovered)
        if not isinstance(proof, VerificationProof):
            raise TypeError("verify returned an invalid contract")
    except Exception as exc:
        recorder.record(
            stage=LabStage.VERIFY,
            status=StageStatus.FAILED,
            reason_code=ReasonCode.TECHNIQUE_FAILURE,
            output_hash=transformed_hash,
            elapsed_ms=_elapsed_ms(started, clock),
            error_type=type(exc).__name__,
        )
        return _result(
            writer,
            run_id,
            arm,
            technique,
            input_hash,
            transformed_hash,
            "failed",
            ReasonCode.TECHNIQUE_FAILURE,
        )
    observed_recovered_hash = sha256_evidence(applied.recovered)
    verified = bool(
        proof.passed
        and applied.recovered == fixture
        and proof.original_hash == input_hash
        and proof.transformed_hash == transformed_hash
        and proof.recovered_hash == observed_recovered_hash == input_hash
    )
    verification_reason = (
        ReasonCode.STAGE_SUCCEEDED if verified else ReasonCode.VERIFICATION_FAILED
    )
    recorder.record(
        stage=LabStage.VERIFY,
        status=(StageStatus.SUCCEEDED if verified else StageStatus.FAILED),
        reason_code=verification_reason,
        output_hash=observed_recovered_hash,
        elapsed_ms=_elapsed_ms(started, clock),
        details={
            "original_hash": input_hash,
            "transformed_hash": transformed_hash,
            "recovered_hash": observed_recovered_hash,
            "passed": verified,
        },
    )
    return _result(
        writer,
        run_id,
        arm,
        technique,
        input_hash,
        transformed_hash,
        "succeeded" if verified else "failed",
        verification_reason,
    )


def _elapsed_ms(started_ns: int, clock: LabClock) -> float:
    return max(0.0, (clock.monotonic_ns() - started_ns) / 1_000_000)


def _result(
    writer: LabRunWriter,
    run_id: str,
    arm: LabArm,
    technique: Technique,
    input_hash: str,
    output_hash: str,
    status: str,
    reason_code: ReasonCode,
) -> LabRunResult:
    return LabRunResult(
        run_id=run_id,
        status=status,
        reason_code=reason_code.value,
        arm=arm.value,
        technique_id=technique.technique_id,
        technique_version=technique.technique_version,
        input_hash=input_hash,
        output_hash=output_hash,
        run_location=writer.run_location,
    )
