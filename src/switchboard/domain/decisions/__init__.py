"""Decision corpus domain (COORD-50) — registry, feature allowlist, signatures.

Pure. No I/O, no storage imports. ``docs/DECISION-CORPUS-SPEC.md`` is authoritative;
this package implements §4.1 (registry) and §4.2 (feature allowlist) so the ledger in
``switchboard.storage.repositories.decision_records`` has an owned vocabulary and a
materialized, export-safe projection.
"""
from __future__ import annotations

from switchboard.domain.decisions.features import (
    DECISION_FEATURES_SCHEMA,
    DIAGNOSTIC_FIELDS,
    FEATURE_FIELDS,
    FEATURES_VERSION,
    project_diagnostics,
    project_features,
    strip_diagnostics,
)
from switchboard.domain.decisions.outcomes import (
    DECISION_OUTCOMES,
    DECISION_OUTCOME_SCHEMA,
    OUTCOMES,
    TERMINAL_OUTCOMES,
    DecisionOutcome,
    get_outcome,
    is_registered_outcome,
    is_terminal_outcome,
)
from switchboard.domain.decisions.reason_codes import (
    REASON_CODE_SCHEMA,
    REASON_CODES,
    RETRY_BUDGET_EXHAUSTED_CODES,
    ReasonCode,
    canonical_reason_code,
    get_reason_code,
    is_registered,
    is_retry_budget_exhausted,
    spelling_key,
)

__all__ = [
    "DECISION_FEATURES_SCHEMA",
    "DECISION_OUTCOMES",
    "DECISION_OUTCOME_SCHEMA",
    "DIAGNOSTIC_FIELDS",
    "FEATURE_FIELDS",
    "FEATURES_VERSION",
    "OUTCOMES",
    "REASON_CODES",
    "REASON_CODE_SCHEMA",
    "RETRY_BUDGET_EXHAUSTED_CODES",
    "TERMINAL_OUTCOMES",
    "DecisionOutcome",
    "ReasonCode",
    "canonical_reason_code",
    "get_outcome",
    "get_reason_code",
    "is_registered",
    "is_registered_outcome",
    "is_retry_budget_exhausted",
    "is_terminal_outcome",
    "project_diagnostics",
    "project_features",
    "spelling_key",
    "strip_diagnostics",
]
