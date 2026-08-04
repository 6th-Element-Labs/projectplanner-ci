"""Pure technique contracts for deterministic Compand lab replays."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Protocol, runtime_checkable

from .scan import decode_line_rle, encode_line_rle


class LabArm(StrEnum):
    """Single-technique arms supported by the development replay wire."""

    BASELINE = "B0"
    SHADOW = "S1"
    ENFORCED = "E1"


class LabStage(StrEnum):
    DETECT = "detect"
    ESTIMATE = "estimate"
    APPLY = "apply"
    VERIFY = "verify"


class StageStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    DECLINED = "declined"
    FAILED = "failed"


class ReasonCode(StrEnum):
    STAGE_STARTED = "stage_started"
    STAGE_SUCCEEDED = "stage_succeeded"
    BASELINE_ARM = "baseline_arm"
    SHADOW_ARM = "shadow_arm"
    NO_CANDIDATE = "no_candidate"
    CANDIDATE_NOT_SMALLER = "candidate_not_smaller"
    TECHNIQUE_CONTRACT_VIOLATION = "technique_contract_violation"
    TECHNIQUE_FAILURE = "technique_failure"
    EVIDENCE_WRITE_FAILED = "evidence_write_failed"
    VERIFICATION_FAILED = "verification_failed"


@dataclass(frozen=True)
class DetectionContext:
    """One immutable fixture supplied to a technique detector."""

    fixture_id: str
    input_hash: str
    original: bytes = field(repr=False)


@dataclass(frozen=True)
class TechniqueCandidate:
    """One whole-fixture candidate; byte payloads are intentionally repr-free."""

    candidate_id: str
    technique_id: str
    technique_version: str
    input_hash: str
    original: bytes = field(repr=False)
    proposed: bytes = field(repr=False)
    metrics: Mapping[str, int | float | str | bool | None] = field(default_factory=dict)


@dataclass(frozen=True)
class EconomicsEstimate:
    """Content-free local economics used only for replay admission."""

    input_bytes: int
    output_bytes: int
    byte_delta: int
    should_apply: bool
    reason_code: ReasonCode


@dataclass(frozen=True)
class AppliedTransform:
    """Transformed bytes plus the recovered bytes and content-free metadata."""

    transformed: bytes = field(repr=False)
    recovered: bytes = field(repr=False)
    recovery_metadata: Mapping[str, int | float | str | bool | None] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class VerificationProof:
    """Exact verification result without raw fixture content."""

    passed: bool
    original_hash: str
    transformed_hash: str
    recovered_hash: str
    reason_code: ReasonCode


@runtime_checkable
class Technique(Protocol):
    """The complete, removable technique plugin interface."""

    technique_id: str
    technique_version: str

    def detect(self, context: DetectionContext) -> tuple[TechniqueCandidate, ...]: ...

    def estimate(self, candidate: TechniqueCandidate) -> EconomicsEstimate: ...

    def apply(self, candidate: TechniqueCandidate) -> AppliedTransform: ...

    def verify(
        self, original: bytes, transformed: bytes, recovered: bytes
    ) -> VerificationProof: ...


def sha256_evidence(value: bytes) -> str:
    """Return the canonical content evidence used throughout the lab."""

    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def candidate_id_for(technique_id: str, technique_version: str, input_hash: str) -> str:
    """Derive a stable candidate identity without inspecting or exposing content."""

    material = f"{technique_id}\0{technique_version}\0{input_hash}".encode("utf-8")
    return f"candidate-{hashlib.sha256(material).hexdigest()}"


class LineRleTechnique:
    """Exact whole-fixture adapter around the existing ``line-rle-v1`` codec."""

    technique_id = "line-rle-v1"
    technique_version = "1.0.0"

    def detect(self, context: DetectionContext) -> tuple[TechniqueCandidate, ...]:
        try:
            original_text = context.original.decode("utf-8")
        except UnicodeDecodeError:
            return ()
        encoded, spans, repeated, removed = encode_line_rle(original_text)
        if spans == 0:
            return ()
        proposed = encoded.encode("utf-8")
        return (
            TechniqueCandidate(
                candidate_id=candidate_id_for(
                    self.technique_id, self.technique_version, context.input_hash
                ),
                technique_id=self.technique_id,
                technique_version=self.technique_version,
                input_hash=context.input_hash,
                original=context.original,
                proposed=proposed,
                metrics={
                    "repeated_span_count": spans,
                    "repeated_line_count": repeated,
                    "removed_line_count": removed,
                },
            ),
        )

    def estimate(self, candidate: TechniqueCandidate) -> EconomicsEstimate:
        self._validate_candidate(candidate)
        input_bytes = len(candidate.original)
        output_bytes = len(candidate.proposed)
        should_apply = output_bytes < input_bytes
        return EconomicsEstimate(
            input_bytes=input_bytes,
            output_bytes=output_bytes,
            byte_delta=input_bytes - output_bytes,
            should_apply=should_apply,
            reason_code=(
                ReasonCode.STAGE_SUCCEEDED
                if should_apply
                else ReasonCode.CANDIDATE_NOT_SMALLER
            ),
        )

    def apply(self, candidate: TechniqueCandidate) -> AppliedTransform:
        self._validate_candidate(candidate)
        transformed = candidate.proposed
        recovered = decode_line_rle(transformed.decode("utf-8")).encode("utf-8")
        return AppliedTransform(
            transformed=transformed,
            recovered=recovered,
            recovery_metadata={
                "codec": self.technique_id,
                "codec_version": self.technique_version,
                "original_hash": sha256_evidence(candidate.original),
                "transformed_hash": sha256_evidence(transformed),
                "recovered_hash": sha256_evidence(recovered),
            },
        )

    def verify(
        self, original: bytes, transformed: bytes, recovered: bytes
    ) -> VerificationProof:
        original_hash = sha256_evidence(original)
        transformed_hash = sha256_evidence(transformed)
        recovered_hash = sha256_evidence(recovered)
        passed = recovered == original and recovered_hash == original_hash
        return VerificationProof(
            passed=passed,
            original_hash=original_hash,
            transformed_hash=transformed_hash,
            recovered_hash=recovered_hash,
            reason_code=(
                ReasonCode.STAGE_SUCCEEDED if passed else ReasonCode.VERIFICATION_FAILED
            ),
        )

    def _validate_candidate(self, candidate: TechniqueCandidate) -> None:
        if (
            candidate.technique_id != self.technique_id
            or candidate.technique_version != self.technique_version
            or candidate.input_hash != sha256_evidence(candidate.original)
            or candidate.candidate_id
            != candidate_id_for(
                self.technique_id,
                self.technique_version,
                candidate.input_hash,
            )
        ):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
