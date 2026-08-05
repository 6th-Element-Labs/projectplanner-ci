"""Within-turn sibling span deduplication with exact reconstruction."""

import hashlib

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


class ParallelOverlapDedupTechnique(IsolatedTechnique):
    technique_id = "parallel-overlap-dedup-v1"

    def propose(self, context: DetectionContext) -> Proposal | None:
        wrapped = fixture_scenario(context.original, family="exact_and_partial_overlap")
        if wrapped is None:
            return None
        scenario, _ = wrapped
        results = scenario.get("new_results")
        index = scenario.get("target_candidate_index")
        source = scenario.get("provider_visible_source")
        if (
            scenario.get("source_state") != "visible"
            or scenario.get("source_artifact_recoverable") is not True
            or scenario.get("source_scope") != scenario.get("requester_scope")
            or not isinstance(results, list)
            or not isinstance(index, int)
            or not 0 <= index < len(results)
            or not isinstance(source, str)
            or results[index] != source
        ):
            return None
        canonical = min(i for i, value in enumerate(results) if value == source)
        digest = hashlib.sha256(source.encode()).hexdigest()
        reconstructed = list(results)
        reconstructed[index] = results[canonical]
        if reconstructed != results:
            return None
        proposed = compact_json(
            {
                "version": self.technique_id,
                "canonical_sibling": canonical,
                "referenced_sibling": index,
                "sha256": digest,
            }
        )
        return Proposal(proposed, {"sibling_count": len(results), "deduplicated_index": index})

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
        ):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        canonical = min((i for i, value in enumerate(results) if value == source), default=-1)
        if (
            canonical < 0
            or results[index] != results[canonical]
            or reference.get("version") != self.technique_id
            or reference.get("canonical_sibling") != canonical
            or reference.get("referenced_sibling") != index
            or reference.get("sha256") != hashlib.sha256(source.encode()).hexdigest()
        ):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        return Recovery(
            candidate.original,
            {"recovery_kind": "sibling_content_reference", "source_artifact_retained": True},
        )


__all__ = ["ParallelOverlapDedupTechnique"]
