"""Consecutive repeated-line encoding with exact codec recovery."""

import hashlib

from ..shared import (
    DetectionContext,
    IsolatedTechnique,
    Proposal,
    ReasonCode,
    Recovery,
    TechniqueCandidate,
    decode_line_rle,
    encode_line_rle,
    fixture_scenario,
)


class LineRleTechnique(IsolatedTechnique):
    technique_id = "line-rle-v1"

    def propose(self, context: DetectionContext) -> Proposal | None:
        source = context.original
        wrapped = fixture_scenario(source, family="repeated_lines")
        if wrapped is not None:
            scenario, _ = wrapped
            value = scenario.get("output")
            if not isinstance(value, str):
                return None
            source = value.encode("utf-8")
        try:
            source_text = source.decode("utf-8")
        except UnicodeDecodeError:
            return None
        encoded, spans, repeated, removed = encode_line_rle(source_text)
        if not spans:
            return None
        proposed = encoded.encode("utf-8")
        if decode_line_rle(encoded).encode("utf-8") != source:
            return None
        return Proposal(
            proposed,
            {
                "repeated_span_count": spans,
                "repeated_line_count": repeated,
                "removed_line_count": removed,
            },
            admission_allowed=len(proposed) < len(source),
        )

    def recover(self, candidate: TechniqueCandidate) -> Recovery:
        try:
            decoded = decode_line_rle(candidate.proposed.decode("utf-8")).encode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION) from exc
        wrapped = fixture_scenario(candidate.original, family="repeated_lines")
        if wrapped is None:
            recovered = decoded
            recovery_kind = "line_rle_decoder"
            retained = False
        else:
            scenario, _ = wrapped
            source = scenario.get("output")
            if not isinstance(source, str) or decoded != source.encode("utf-8"):
                raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
            recovered = candidate.original
            recovery_kind = "line_rle_decoder_with_retained_fixture"
            retained = True
        return Recovery(
            recovered,
            {
                "recovery_kind": recovery_kind,
                "decoded_payload_hash": hashlib.sha256(decoded).hexdigest(),
                "source_artifact_retained": retained,
            },
        )


__all__ = ["LineRleTechnique"]
