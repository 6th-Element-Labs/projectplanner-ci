"""Isolated Compand Phase 2 technique plugins and their frozen registry."""

from .registry import (
    ALL_TECHNIQUE_IDS,
    SUPPORTED_TECHNIQUE_IDS,
    TECHNIQUE_REGISTRY,
    UNSUPPORTED_TECHNIQUE_IDS,
    TechniqueRegistration,
    TechniqueSupportStatus,
    UnsupportedTechniqueRecord,
    get_registration,
    resolve_technique,
)

__all__ = [
    "ALL_TECHNIQUE_IDS",
    "SUPPORTED_TECHNIQUE_IDS",
    "TECHNIQUE_REGISTRY",
    "UNSUPPORTED_TECHNIQUE_IDS",
    "TechniqueRegistration",
    "TechniqueSupportStatus",
    "UnsupportedTechniqueRecord",
    "get_registration",
    "resolve_technique",
]
