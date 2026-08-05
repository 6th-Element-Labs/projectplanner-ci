"""Bounded presentation-only line-ending normalization."""

from ..shared import (
    DetectionContext,
    IsolatedTechnique,
    Proposal,
    ReasonCode,
    Recovery,
    TechniqueCandidate,
    fixture_scenario,
)


class LineEndingNormalizeTechnique(IsolatedTechnique):
    technique_id = "line-ending-normalize-v1"

    def propose(self, context: DetectionContext) -> Proposal | None:
        wrapped = fixture_scenario(context.original, family="terminal_and_progress")
        if wrapped is None:
            return None
        scenario, _ = wrapped
        if scenario.get("presentation_only") is not True:
            return None
        if scenario.get("byte_sensitive") or scenario.get("integrity_protected"):
            return None
        source = scenario.get("terminal_bytes_utf8") or scenario.get("text_utf8")
        if not isinstance(source, str):
            return None
        normalized = source.replace("\r\n", "\n").replace("\r", "\n")
        if normalized == source:
            return None
        return Proposal(
            normalized.encode("utf-8"),
            {"normalized_line_endings": source.count("\r")},
        )

    def recover(self, candidate: TechniqueCandidate) -> Recovery:
        wrapped = fixture_scenario(candidate.original, family="terminal_and_progress")
        if wrapped is None:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        scenario, _ = wrapped
        source = scenario.get("terminal_bytes_utf8") or scenario.get("text_utf8")
        if not isinstance(source, str):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        normalized = source.replace("\r\n", "\n").replace("\r", "\n").encode()
        if normalized != candidate.proposed:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        return Recovery(
            candidate.original,
            {"recovery_kind": "retained_presentation_artifact", "source_artifact_retained": True},
        )


__all__ = ["LineEndingNormalizeTechnique"]
