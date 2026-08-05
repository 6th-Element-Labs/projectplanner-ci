"""Order-preserving JSON whitespace minification with duplicate-key refusal."""

import json
from typing import Any

from ..shared import (
    DetectionContext,
    IsolatedTechnique,
    Proposal,
    ReasonCode,
    Recovery,
    TechniqueCandidate,
    fixture_scenario,
)


class _DuplicateKey(ValueError):
    pass


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey(key)
        value[key] = item
    return value


class JsonMinifyTechnique(IsolatedTechnique):
    technique_id = "json-minify-v1"

    def propose(self, context: DetectionContext) -> Proposal | None:
        wrapped = fixture_scenario(context.original, family="structured_json_and_tables")
        source = context.original
        if wrapped is not None:
            scenario, _ = wrapped
            if not isinstance(scenario.get("typed_json"), (dict, list)):
                return None
            if scenario.get("raw_json") is not None:
                return None
            typed = scenario.get("typed_json")
            if isinstance(typed, list) and typed and all(isinstance(row, dict) for row in typed):
                columns = list(typed[0])
                if any(list(row) != columns for row in typed):
                    return None
            value = scenario.get("source_json_utf8")
            if not isinstance(value, str):
                return None
            source = value.encode("utf-8")
        elif context.original.lstrip()[:1] not in {b"{", b"["}:
            return None
        try:
            decoded = json.loads(
                source.decode("utf-8"),
                object_pairs_hook=_object,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        proposed = json.dumps(
            decoded, ensure_ascii=False, separators=(",", ":"), sort_keys=False
        ).encode("utf-8")
        try:
            roundtrip = json.loads(proposed.decode("utf-8"), object_pairs_hook=_object)
        except (json.JSONDecodeError, ValueError):
            return None
        if roundtrip != decoded or proposed == source:
            return None
        return Proposal(proposed, {"json_value_verified": True})

    def recover(self, candidate: TechniqueCandidate) -> Recovery:
        wrapped = fixture_scenario(candidate.original, family="structured_json_and_tables")
        source = candidate.original
        if wrapped is not None:
            scenario, _ = wrapped
            value = scenario.get("source_json_utf8")
            if not isinstance(value, str):
                raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
            source = value.encode("utf-8")
        try:
            original_value = json.loads(source.decode("utf-8"), object_pairs_hook=_object)
            recovered_value = json.loads(
                candidate.proposed.decode("utf-8"), object_pairs_hook=_object
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION) from exc
        if recovered_value != original_value:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        return Recovery(
            candidate.original,
            {"recovery_kind": "json_value_plus_retained_bytes", "source_artifact_retained": True},
        )


__all__ = ["JsonMinifyTechnique"]
