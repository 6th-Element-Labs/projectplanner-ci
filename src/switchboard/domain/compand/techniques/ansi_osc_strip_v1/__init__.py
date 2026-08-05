"""Certified ANSI SGR and OSC-8 presentation stripping."""

import re

from ..shared import (
    DetectionContext,
    IsolatedTechnique,
    Proposal,
    ReasonCode,
    Recovery,
    TechniqueCandidate,
    fixture_scenario,
)


_SGR = re.compile(r"\x1b\[[0-9;]*m")
_OSC8 = re.compile(r"\x1b\]8;;([^\x07\x1b]*)\x07(.*?)\x1b\]8;;\x07", re.DOTALL)


class AnsiOscStripTechnique(IsolatedTechnique):
    technique_id = "ansi-osc-strip-v1"

    def propose(self, context: DetectionContext) -> Proposal | None:
        wrapped = fixture_scenario(context.original, family="terminal_and_progress")
        if wrapped is None:
            return None
        scenario, _ = wrapped
        if scenario.get("known_control_sequence") is not True:
            return None
        source = scenario.get("terminal_bytes_utf8")
        if not isinstance(source, str):
            return None
        transformed = _OSC8.sub(lambda m: f"{m.group(2)} ({m.group(1)})", source)
        transformed = _SGR.sub("", transformed)
        if "\x1b" in transformed or transformed == source:
            return None
        return Proposal(
            transformed.encode("utf-8"),
            {"source_bytes": len(source.encode()), "visible_bytes": len(transformed.encode())},
        )

    def recover(self, candidate: TechniqueCandidate) -> Recovery:
        wrapped = fixture_scenario(candidate.original, family="terminal_and_progress")
        if wrapped is None:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        scenario, _ = wrapped
        source = scenario.get("terminal_bytes_utf8")
        if not isinstance(source, str):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        transformed = _OSC8.sub(lambda match: f"{match.group(2)} ({match.group(1)})", source)
        transformed = _SGR.sub("", transformed).encode("utf-8")
        if transformed != candidate.proposed:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        return Recovery(
            candidate.original,
            {"recovery_kind": "retained_terminal_artifact", "source_artifact_retained": True},
        )


__all__ = ["AnsiOscStripTechnique"]
