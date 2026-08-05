"""Scoped exact duplicate reference against frozen provider-visible history."""

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
    sha256_evidence,
)


class ExactDuplicateReferenceTechnique(IsolatedTechnique):
    technique_id = "exact-duplicate-reference-v1"

    def propose(self, context: DetectionContext) -> Proposal | None:
        wrapped = fixture_scenario(context.original, family="exact_and_partial_overlap")
        if wrapped is None:
            return None
        scenario, _ = wrapped
        source = scenario.get("provider_visible_source")
        results = scenario.get("new_results")
        index = scenario.get("target_candidate_index")
        if (
            scenario.get("source_state") != "visible"
            or scenario.get("source_artifact_recoverable") is not True
            or scenario.get("source_scope") != scenario.get("requester_scope")
            or not isinstance(source, str)
            or not isinstance(results, list)
            or not isinstance(index, int)
            or index < 0
            or index >= len(results)
            or results[index] != source
        ):
            return None
        digest = sha256_evidence(source.encode()).removeprefix("sha256:")
        proposed = compact_json(
            {
                "version": self.technique_id,
                "scope": scenario["requester_scope"],
                "sha256": digest,
            }
        )
        if digest != sha256_evidence(results[index].encode()).removeprefix("sha256:"):
            return None
        return Proposal(proposed, {"exact_duplicate": True, "source_bytes": len(source.encode())})

    def recover(self, candidate: TechniqueCandidate) -> Recovery:
        wrapped = fixture_scenario(candidate.original, family="exact_and_partial_overlap")
        reference = decode_json_object(candidate.proposed)
        if wrapped is None or reference is None:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        scenario, _ = wrapped
        source = scenario.get("provider_visible_source")
        results = scenario.get("new_results")
        index = scenario.get("target_candidate_index")
        if (
            not isinstance(source, str)
            or not isinstance(results, list)
            or not isinstance(index, int)
            or not 0 <= index < len(results)
            or results[index] != source
            or reference.get("version") != self.technique_id
            or reference.get("scope") != scenario.get("requester_scope")
            or reference.get("sha256")
            != sha256_evidence(source.encode()).removeprefix("sha256:")
        ):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        return Recovery(
            candidate.original,
            {"recovery_kind": "content_addressed_source_reference", "source_artifact_retained": True},
        )


__all__ = ["ExactDuplicateReferenceTechnique"]
