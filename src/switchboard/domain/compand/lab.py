"""Pure technique contracts for deterministic Compand lab replays."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Protocol, runtime_checkable

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
    CACHE_ECONOMICS_VETO = "cache_economics_veto"
    UNSUPPORTED_TECHNIQUE = "unsupported_technique"
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



# Compatibility export.  The implementation lives in its isolated plugin package.
from .techniques.line_rle_v1 import LineRleTechnique  # noqa: E402,F401
