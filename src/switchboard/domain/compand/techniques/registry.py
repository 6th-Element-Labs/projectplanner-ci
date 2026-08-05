"""Frozen Compand Phase 2 technique registry: plugins or explicit unsupported records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Callable, Mapping

from switchboard.domain.compand.lab import Technique

from .ansi_osc_strip_v1 import AnsiOscStripTechnique
from .command_aware_projection_v1 import CommandAwareProjectionTechnique
from .delta_reread_v1 import DeltaRereadTechnique
from .exact_duplicate_reference_v1 import ExactDuplicateReferenceTechnique
from .json_minify_v1 import JsonMinifyTechnique
from .line_ending_normalize_v1 import LineEndingNormalizeTechnique
from .line_rle_v1 import LineRleTechnique
from .parallel_overlap_dedup_v1 import ParallelOverlapDedupTechnique
from .prefix_cache_shaping_v1 import PrefixCacheShapingTechnique
from .structured_data_codec_v1 import StructuredDataCodecTechnique
from .subresult_chunk_dedup_v1 import SubresultChunkDedupTechnique
from .successful_check_projection_v1 import SuccessfulCheckProjectionTechnique
from .trailing_noise_trim_v1 import TrailingNoiseTrimTechnique
from .unchanged_file_identity_v1 import UnchangedFileIdentityTechnique


class TechniqueSupportStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class UnsupportedTechniqueRecord:
    technique_id: str
    technique_version: str
    cloud_eligibility: str
    guarantee: str
    unsupported_reason: str
    host_dependency: str
    status: TechniqueSupportStatus = TechniqueSupportStatus.UNSUPPORTED

    def as_dict(self) -> dict[str, str]:
        return {
            "status": self.status.value,
            "reason_code": "unsupported_technique",
            "technique_id": self.technique_id,
            "technique_version": self.technique_version,
            "cloud_eligibility": self.cloud_eligibility,
            "guarantee": self.guarantee,
            "unsupported_reason": self.unsupported_reason,
            "host_dependency": self.host_dependency,
        }


@dataclass(frozen=True)
class TechniqueRegistration:
    technique_id: str
    technique_version: str
    status: TechniqueSupportStatus
    factory: Callable[[], Technique] | None = None
    unsupported: UnsupportedTechniqueRecord | None = None

    def instantiate(self) -> Technique:
        if self.factory is None:
            if self.unsupported is None:
                raise RuntimeError("invalid technique registration")
            raise UnsupportedTechniqueError(self.unsupported)
        plugin = self.factory()
        if (
            plugin.technique_id != self.technique_id
            or plugin.technique_version != self.technique_version
        ):
            raise RuntimeError("technique factory attribution mismatch")
        return plugin


class UnsupportedTechniqueError(ValueError):
    def __init__(self, record: UnsupportedTechniqueRecord) -> None:
        super().__init__(record.unsupported_reason)
        self.record = record


def _supported(factory: Callable[[], Technique]) -> TechniqueRegistration:
    plugin = factory()
    return TechniqueRegistration(
        technique_id=plugin.technique_id,
        technique_version=plugin.technique_version,
        status=TechniqueSupportStatus.SUPPORTED,
        factory=factory,
    )


def _unsupported(
    technique_id: str,
    cloud_eligibility: str,
    guarantee: str,
    reason: str,
    host_dependency: str,
) -> TechniqueRegistration:
    record = UnsupportedTechniqueRecord(
        technique_id=technique_id,
        technique_version="1.0.0",
        cloud_eligibility=cloud_eligibility,
        guarantee=guarantee,
        unsupported_reason=reason,
        host_dependency=host_dependency,
    )
    return TechniqueRegistration(
        technique_id=technique_id,
        technique_version="1.0.0",
        status=TechniqueSupportStatus.UNSUPPORTED,
        unsupported=record,
    )


_REGISTRATIONS = (
    _supported(AnsiOscStripTechnique),
    _supported(LineEndingNormalizeTechnique),
    _supported(JsonMinifyTechnique),
    _supported(TrailingNoiseTrimTechnique),
    _supported(LineRleTechnique),
    _supported(ExactDuplicateReferenceTechnique),
    _supported(SubresultChunkDedupTechnique),
    _supported(ParallelOverlapDedupTechnique),
    _supported(StructuredDataCodecTechnique),
    _supported(DeltaRereadTechnique),
    _supported(UnchangedFileIdentityTechnique),
    _supported(CommandAwareProjectionTechnique),
    _supported(SuccessfulCheckProjectionTechnique),
    _unsupported(
        "context-paging-v1",
        "shadow_only_unless_cloud_context_epoch",
        "recoverable",
        "No generally certified cooperative cloud context-epoch seam is frozen for Phase 2 v1.",
        "cooperative_agent_or_provider_native_compaction",
    ),
    _supported(PrefixCacheShapingTechnique),
    _unsupported(
        "schema-deferral-v1",
        "shadow_only_unless_cloud_api_supports_tool_search",
        "semantic",
        "Generic transparent gateway cannot remove client-supplied schemas without a certified discovery loop.",
        "cooperative_agent_or_cloud_tool_search",
    ),
    _unsupported(
        "turn-elimination-v1",
        "shadow_only_unless_cloud_api_supports_inner_loop",
        "semantic",
        "Transparent provider gateway cannot decide agent wake policy or compose host tool calls.",
        "harness_or_cooperative_cloud_agent",
    ),
    _unsupported(
        "semantic-cache-v1",
        "shadow_only",
        "semantic",
        "Similarity reuse has a correctness cliff for coding traffic and lacks a Phase 2 safety contract.",
        "none",
    ),
    _unsupported(
        "agent-memory-summary-v1",
        "shadow_only",
        "semantic",
        "Lossy model-generated summaries need a separate safety and cooperative context contract.",
        "cooperative_agent_or_provider_native_compaction",
    ),
    _unsupported(
        "injected-efficiency-instructions-v1",
        "shadow_only",
        "semantic",
        "Changes agent behavior and requires model/dialect-specific safety evidence before enforcement.",
        "cloud_harness_or_supported_instruction_seam",
    ),
    _unsupported(
        "injected-text-hard-compression-v1",
        "shadow_only",
        "semantic",
        "No separate model-specific lossy safety contract is frozen.",
        "none",
    ),
    _unsupported(
        "output-shaping-v1",
        "shadow_only",
        "semantic",
        "Provider gateway cannot safely set task-class output policy without a certified caller contract.",
        "harness_or_cooperative_cloud_agent",
    ),
    _unsupported(
        "code-action-batching-v1",
        "shadow_only_unless_cloud_api_supports_inner_loop",
        "semantic",
        "Requires an agent or cloud harness inner loop; a transparent gateway may not originate or compose tools.",
        "harness_or_cooperative_cloud_agent",
    ),
    _unsupported(
        "lean-prompt-v1",
        "shadow_only_unless_cloud_prompt_contract",
        "semantic",
        "Transparent gateway cannot delete caller instructions without an explicit cloud prompt-ownership contract.",
        "cloud_harness_or_agent_prompt_owner",
    ),
    _unsupported(
        "vision-budget-v1",
        "shadow_only",
        "semantic",
        "No Phase 2 v1 visual corpus or non-inferiority contract is materialized.",
        "none",
    ),
    _unsupported(
        "routing-context-profile-v1",
        "out_of_scope_sibling",
        "semantic",
        "MODEL-CATALOG-ROUTING owns tier selection; a technique plugin may not own routing.",
        "dispatch_or_harness",
    ),
    _unsupported(
        "learned-soft-compression-v1",
        "provider_native_only",
        "semantic",
        "Requires model-weight or embedding-path access unavailable to the cloud gateway.",
        "provider_or_self_hosted_model",
    ),
    _unsupported(
        "provider-kv-reuse-v1",
        "provider_native_only",
        "provider_native",
        "Runs inside provider or self-hosted inference engine, not the black-box cloud gateway.",
        "provider_or_self_hosted_model",
    ),
    _unsupported(
        "speculative-decoding-v1",
        "provider_native_only",
        "provider_native",
        "Provider/model execution optimization affects latency rather than gateway-visible token content.",
        "provider_or_self_hosted_model",
    ),
    _unsupported(
        "transport-gzip-v1",
        "not_a_token_technique",
        "exact",
        "Saves bandwidth, not model tokens or whole-task provider cost; retained as an explicit negative control.",
        "none",
    ),
)


if len({item.technique_id for item in _REGISTRATIONS}) != len(_REGISTRATIONS):
    raise RuntimeError("duplicate Compand technique registration")

TECHNIQUE_REGISTRY: Mapping[str, TechniqueRegistration] = MappingProxyType(
    {item.technique_id: item for item in _REGISTRATIONS}
)
ALL_TECHNIQUE_IDS = tuple(TECHNIQUE_REGISTRY)
SUPPORTED_TECHNIQUE_IDS = tuple(
    key for key, value in TECHNIQUE_REGISTRY.items() if value.status is TechniqueSupportStatus.SUPPORTED
)
UNSUPPORTED_TECHNIQUE_IDS = tuple(
    key for key, value in TECHNIQUE_REGISTRY.items() if value.status is TechniqueSupportStatus.UNSUPPORTED
)


def get_registration(technique_id: str) -> TechniqueRegistration:
    try:
        return TECHNIQUE_REGISTRY[technique_id]
    except KeyError as exc:
        raise KeyError(f"unknown technique_id: {technique_id}") from exc


def resolve_technique(technique_id: str) -> Technique:
    return get_registration(technique_id).instantiate()


__all__ = [
    "ALL_TECHNIQUE_IDS",
    "SUPPORTED_TECHNIQUE_IDS",
    "TECHNIQUE_REGISTRY",
    "UNSUPPORTED_TECHNIQUE_IDS",
    "TechniqueRegistration",
    "TechniqueSupportStatus",
    "UnsupportedTechniqueError",
    "UnsupportedTechniqueRecord",
    "get_registration",
    "resolve_technique",
]
