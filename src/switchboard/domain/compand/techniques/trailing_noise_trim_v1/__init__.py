"""Deterministic final-frame projection for certified progress output."""

from ..shared import (
    DetectionContext,
    IsolatedTechnique,
    Proposal,
    ReasonCode,
    Recovery,
    TechniqueCandidate,
    fixture_scenario,
)


class TrailingNoiseTrimTechnique(IsolatedTechnique):
    technique_id = "trailing-noise-trim-v1"

    def propose(self, context: DetectionContext) -> Proposal | None:
        wrapped = fixture_scenario(context.original, family="terminal_and_progress")
        if wrapped is None:
            return None
        scenario, _ = wrapped
        if scenario.get("content_kind") != "terminal_display":
            return None
        source = scenario.get("terminal_bytes_utf8")
        if not isinstance(source, str) or "\r" not in source:
            return None
        lowered = source.lower()
        if any(word in lowered for word in ("warning", "error", "failed", "traceback")):
            return None
        visible_lines: list[str] = []
        for line in source.split("\n"):
            visible_lines.append(line.split("\r")[-1])
        transformed = "\n".join(visible_lines)
        if transformed == source:
            return None
        return Proposal(
            transformed.encode("utf-8"),
            {"progress_frames_removed": source.count("\r")},
        )

    def recover(self, candidate: TechniqueCandidate) -> Recovery:
        wrapped = fixture_scenario(candidate.original, family="terminal_and_progress")
        if wrapped is None:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        scenario, _ = wrapped
        source = scenario.get("terminal_bytes_utf8")
        if not isinstance(source, str):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        transformed = "\n".join(line.split("\r")[-1] for line in source.split("\n"))
        if transformed.encode() != candidate.proposed:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        return Recovery(
            candidate.original,
            {"recovery_kind": "retained_terminal_artifact", "source_artifact_retained": True},
        )


__all__ = ["TrailingNoiseTrimTechnique"]
