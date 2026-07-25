"""Decision corpus domain (COORD-50) — registry, feature allowlist, signatures.

Pure. No I/O, no storage imports. ``docs/DECISION-CORPUS-SPEC.md`` is authoritative;
this package implements §4.1 (registry) and §4.2 (feature allowlist) so the ledger in
``switchboard.storage.repositories.decision_records`` has an owned vocabulary and a
materialized, export-safe projection.
"""
from __future__ import annotations

from switchboard.domain.decisions.features import (
    DECISION_FEATURES_SCHEMA,
    FEATURE_FIELDS,
    FEATURES_VERSION,
    project_features,
)
from switchboard.domain.decisions.reason_codes import (
    REASON_CODE_SCHEMA,
    REASON_CODES,
    ReasonCode,
    canonical_reason_code,
    get_reason_code,
    is_registered,
    spelling_key,
)

__all__ = [
    "DECISION_FEATURES_SCHEMA",
    "FEATURE_FIELDS",
    "FEATURES_VERSION",
    "REASON_CODES",
    "REASON_CODE_SCHEMA",
    "ReasonCode",
    "canonical_reason_code",
    "get_reason_code",
    "is_registered",
    "project_features",
    "spelling_key",
]
