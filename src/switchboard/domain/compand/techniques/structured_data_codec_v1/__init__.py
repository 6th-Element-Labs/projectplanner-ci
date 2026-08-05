"""Ordered homogeneous JSON row codec with typed exact reconstruction."""

import json

from ..shared import (
    DetectionContext,
    IsolatedTechnique,
    Proposal,
    ReasonCode,
    Recovery,
    TechniqueCandidate,
    compact_json,
    decode_json_object,
    fixture_scenario,
)


class StructuredDataCodecTechnique(IsolatedTechnique):
    technique_id = "structured-data-codec-v1"

    def propose(self, context: DetectionContext) -> Proposal | None:
        wrapped = fixture_scenario(context.original, family="structured_json_and_tables")
        if wrapped is None:
            return None
        scenario, _ = wrapped
        if not isinstance(scenario.get("typed_json"), list):
            return None
        if scenario.get("raw_json") is not None:
            return None
        source = scenario.get("source_json_utf8")
        if not isinstance(source, str):
            return None
        try:
            decoded = json.loads(source)
        except json.JSONDecodeError:
            return None
        if not isinstance(decoded, list) or len(decoded) < 2 or not all(isinstance(row, dict) for row in decoded):
            return None
        columns = list(decoded[0])
        if not columns or any(list(row) != columns for row in decoded):
            return None
        rows = [[row[column] for column in columns] for row in decoded]
        candidate = {"version": self.technique_id, "columns": columns, "rows": rows}
        reconstructed = [dict(zip(columns, row, strict=True)) for row in rows]
        if reconstructed != decoded:
            return None
        return Proposal(compact_json(candidate), {"column_count": len(columns), "row_count": len(rows)})

    def recover(self, candidate: TechniqueCandidate) -> Recovery:
        wrapped = fixture_scenario(candidate.original, family="structured_json_and_tables")
        encoded = decode_json_object(candidate.proposed)
        if wrapped is None or encoded is None or encoded.get("version") != self.technique_id:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        scenario, _ = wrapped
        source = scenario.get("source_json_utf8")
        columns = encoded.get("columns")
        rows = encoded.get("rows")
        if (
            not isinstance(source, str)
            or not isinstance(columns, list)
            or not columns
            or any(not isinstance(column, str) for column in columns)
            or not isinstance(rows, list)
            or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)
        ):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        try:
            source_value = json.loads(source)
            decoded_value = [dict(zip(columns, row, strict=True)) for row in rows]
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION) from exc
        if decoded_value != source_value:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        return Recovery(
            candidate.original,
            {"recovery_kind": "structured_rows_decoder", "source_artifact_retained": True},
        )


__all__ = ["StructuredDataCodecTechnique"]
