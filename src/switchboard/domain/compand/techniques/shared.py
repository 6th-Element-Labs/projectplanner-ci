"""Shared contract helpers; technique packages never import one another."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping

from switchboard.domain.compand.lab import (
    AppliedTransform,
    DetectionContext,
    EconomicsEstimate,
    ReasonCode,
    TechniqueCandidate,
    VerificationProof,
    candidate_id_for,
    sha256_evidence,
)
from switchboard.domain.compand.scan import decode_line_rle, encode_line_rle


JsonObject = dict[str, Any]
MetricValue = int | float | str | bool | None


@dataclass(frozen=True)
class Proposal:
    """A deterministic model-visible candidate and content-free measurements."""

    proposed: bytes
    metrics: Mapping[str, MetricValue]
    admission_allowed: bool = True
    decline_reason: ReasonCode = ReasonCode.CANDIDATE_NOT_SMALLER


@dataclass(frozen=True)
class Recovery:
    """Technique-owned exact recovery plus content-free provenance."""

    recovered: bytes
    metadata: Mapping[str, MetricValue]


def compact_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=False
    ).encode("utf-8")


def decode_json_object(value: bytes) -> JsonObject | None:
    try:
        decoded = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def fixture_scenario(
    original: bytes, *, family: str | None = None
) -> tuple[JsonObject, JsonObject] | None:
    """Return ``(scenario, scope)`` from a CES case or a direct typed envelope."""

    decoded = decode_json_object(original)
    if decoded is None:
        return None
    if decoded.get("schema") == "compand.ces1.case_record.v1":
        if family is not None:
            declared_family = decoded.get("fixture_family")
            record_id = str(decoded.get("record_id") or "")
            if declared_family not in {None, family} or (
                declared_family is None and not record_id.startswith(f"{family}-")
            ):
                return None
        case_input = decoded.get("input")
        if not isinstance(case_input, dict):
            return None
        scenario = case_input.get("scenario")
        scope = case_input.get("scope")
        if not isinstance(scenario, dict) or not isinstance(scope, dict):
            return None
        if decoded.get("oracle_class") not in {"positive", "boundary"}:
            return None
        return scenario, scope
    scenario = decoded.get("scenario", decoded)
    scope = decoded.get("scope", {})
    if not isinstance(scenario, dict) or not isinstance(scope, dict):
        return None
    if family is not None:
        declared_family = decoded.get("fixture_family") or scenario.get("fixture_family")
        if declared_family not in {None, family}:
            return None
    return scenario, scope


def scoped_identity(scope: Mapping[str, Any]) -> str:
    return "/".join(
        str(scope.get(key) or "") for key in ("tenant", "principal", "session")
    )


class IsolatedTechnique(ABC):
    """Common contract enforcement while each package owns its own proposal logic."""

    technique_id: str
    technique_version = "1.0.0"

    @abstractmethod
    def propose(self, context: DetectionContext) -> Proposal | None:
        """Return a verified candidate or decline without mutating the source."""

    @abstractmethod
    def recover(self, candidate: TechniqueCandidate) -> Recovery:
        """Decode or expand the candidate using this technique's own contract."""

    def detect(self, context: DetectionContext) -> tuple[TechniqueCandidate, ...]:
        proposal = self.propose(context)
        if proposal is None:
            return ()
        metrics = dict(proposal.metrics)
        metrics["admission_allowed"] = proposal.admission_allowed
        metrics["decline_reason"] = proposal.decline_reason.value
        return (
            TechniqueCandidate(
                candidate_id=candidate_id_for(
                    self.technique_id, self.technique_version, context.input_hash
                ),
                technique_id=self.technique_id,
                technique_version=self.technique_version,
                input_hash=context.input_hash,
                original=context.original,
                proposed=proposal.proposed,
                metrics=metrics,
            ),
        )

    def estimate(self, candidate: TechniqueCandidate) -> EconomicsEstimate:
        self._validate_candidate(candidate)
        input_bytes = len(candidate.original)
        output_bytes = len(candidate.proposed)
        smaller = output_bytes < input_bytes
        admission_allowed = candidate.metrics.get("admission_allowed") is not False
        should_apply = smaller and admission_allowed
        reason = ReasonCode.STAGE_SUCCEEDED
        if not admission_allowed:
            raw_reason = str(
                candidate.metrics.get("decline_reason")
                or ReasonCode.CACHE_ECONOMICS_VETO.value
            )
            reason = ReasonCode(raw_reason)
        elif not smaller:
            reason = ReasonCode.CANDIDATE_NOT_SMALLER
        return EconomicsEstimate(
            input_bytes=input_bytes,
            output_bytes=output_bytes,
            byte_delta=input_bytes - output_bytes,
            should_apply=should_apply,
            reason_code=reason,
        )

    def apply(self, candidate: TechniqueCandidate) -> AppliedTransform:
        self._validate_candidate(candidate)
        recovery = self.recover(candidate)
        if recovery.recovered != candidate.original:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        metadata: dict[str, MetricValue] = {
            "technique_id": self.technique_id,
            "technique_version": self.technique_version,
            "original_hash": sha256_evidence(candidate.original),
            "transformed_hash": sha256_evidence(candidate.proposed),
            "recovered_hash": sha256_evidence(recovery.recovered),
        }
        metadata.update(recovery.metadata)
        return AppliedTransform(
            transformed=candidate.proposed,
            recovered=recovery.recovered,
            recovery_metadata=metadata,
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
        expected_id = candidate_id_for(
            self.technique_id, self.technique_version, candidate.input_hash
        )
        if (
            candidate.technique_id != self.technique_id
            or candidate.technique_version != self.technique_version
            or candidate.input_hash != sha256_evidence(candidate.original)
            or candidate.candidate_id != expected_id
        ):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)


__all__ = [
    "DetectionContext",
    "IsolatedTechnique",
    "JsonObject",
    "Proposal",
    "Recovery",
    "ReasonCode",
    "TechniqueCandidate",
    "compact_json",
    "decode_json_object",
    "decode_line_rle",
    "encode_line_rle",
    "fixture_scenario",
    "scoped_identity",
    "sha256_evidence",
]
