"""Trusted unchanged-file identity references scoped to provider-visible history."""

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


class UnchangedFileIdentityTechnique(IsolatedTechnique):
    technique_id = "unchanged-file-identity-v1"

    def propose(self, context: DetectionContext) -> Proposal | None:
        wrapped = fixture_scenario(context.original, family="file_rereads_and_diffs")
        if wrapped is None:
            return None
        scenario, _ = wrapped
        receipt = scenario.get("typed_reread_receipt")
        current = scenario.get("current")
        source = scenario.get("provider_visible_source_utf8")
        if (
            scenario.get("binary") is True
            or scenario.get("trusted_file_identity") is not True
            or scenario.get("history_state") != "current"
            or scenario.get("provider_visible_source_scope") != scenario.get("requester_scope")
            or not isinstance(receipt, dict)
            or receipt.get("trusted_adapter") is not True
            or receipt.get("truncated") is not False
            or not isinstance(current, str)
            or source != current
        ):
            return None
        digest = hashlib.sha256(current.encode()).hexdigest()
        if receipt.get("output_sha256") != digest or scenario.get("current_sha256") != digest:
            return None
        proposed = compact_json(
            {
                "version": self.technique_id,
                "scope": scenario["requester_scope"],
                "path": scenario.get("path"),
                "artifact_sha256": digest,
            }
        )
        return Proposal(proposed, {"unchanged_file": True, "source_bytes": len(current.encode())})

    def recover(self, candidate: TechniqueCandidate) -> Recovery:
        wrapped = fixture_scenario(candidate.original, family="file_rereads_and_diffs")
        reference = decode_json_object(candidate.proposed)
        if wrapped is None or reference is None:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        scenario, _ = wrapped
        current = scenario.get("current")
        source = scenario.get("provider_visible_source_utf8")
        if (
            not isinstance(current, str)
            or source != current
            or reference.get("version") != self.technique_id
            or reference.get("scope") != scenario.get("requester_scope")
            or reference.get("path") != scenario.get("path")
            or reference.get("artifact_sha256")
            != hashlib.sha256(current.encode()).hexdigest()
        ):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        return Recovery(
            candidate.original,
            {"recovery_kind": "content_addressed_file_reference", "source_artifact_retained": True},
        )


__all__ = ["UnchangedFileIdentityTechnique"]
