"""Frozen-prefix replay plus cache-aware candidate admission."""

from ..shared import (
    DetectionContext,
    IsolatedTechnique,
    Proposal,
    ReasonCode,
    Recovery,
    TechniqueCandidate,
    fixture_scenario,
)


class PrefixCacheShapingTechnique(IsolatedTechnique):
    technique_id = "prefix-cache-shaping-v1"

    def propose(self, context: DetectionContext) -> Proposal | None:
        wrapped = fixture_scenario(context.original, family="cache_sensitive_prefixes")
        if wrapped is None:
            return None
        scenario, _ = wrapped
        prefix = scenario.get("provider_visible_prefix_utf8")
        replayed = scenario.get("replayed_prefix_utf8")
        suffix = scenario.get("suffix")
        candidate = scenario.get("candidate_suffix_utf8")
        original_cost = scenario.get("projected_original_cost_microusd")
        candidate_cost = scenario.get("projected_candidate_cost_microusd")
        if (
            not isinstance(prefix, str)
            or replayed != prefix
            or scenario.get("cache_entry_expired") is True
            or not isinstance(suffix, str)
            or not isinstance(candidate, str)
            or isinstance(original_cost, bool)
            or isinstance(candidate_cost, bool)
            or not isinstance(original_cost, (int, float))
            or not isinstance(candidate_cost, (int, float))
        ):
            return None
        smaller = scenario.get("candidate_smaller") is True
        cache_safe = candidate_cost <= original_cost
        return Proposal(
            candidate.encode(),
            {
                "prefix_byte_identical": True,
                "candidate_suffix_smaller": smaller,
                "projected_original_cost_microusd": original_cost,
                "projected_candidate_cost_microusd": candidate_cost,
            },
            admission_allowed=smaller and cache_safe,
            decline_reason=ReasonCode.CACHE_ECONOMICS_VETO,
        )

    def recover(self, candidate: TechniqueCandidate) -> Recovery:
        wrapped = fixture_scenario(candidate.original, family="cache_sensitive_prefixes")
        if wrapped is None:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        scenario, _ = wrapped
        prefix = scenario.get("provider_visible_prefix_utf8")
        transformed = scenario.get("candidate_suffix_utf8")
        if (
            not isinstance(prefix, str)
            or scenario.get("replayed_prefix_utf8") != prefix
            or not isinstance(transformed, str)
            or transformed.encode() != candidate.proposed
        ):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        return Recovery(
            candidate.original,
            {"recovery_kind": "retained_cache_input", "source_artifact_retained": True},
        )


__all__ = ["PrefixCacheShapingTechnique"]
