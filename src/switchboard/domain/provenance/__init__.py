"""Provenance domain — git/offline Done authority without persistence."""
from .git import (
    EVIDENCE_HASH_RE,
    has_done_provenance,
    offline_evidence_from_state,
    provenance_summary,
    valid_evidence_hash,
)

__all__ = [
    "EVIDENCE_HASH_RE",
    "has_done_provenance",
    "offline_evidence_from_state",
    "provenance_summary",
    "valid_evidence_hash",
]
