"""Git merge and offline-evidence provenance rules."""
from __future__ import annotations

import re
from typing import Any, Mapping


EVIDENCE_HASH_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$", re.I)


def offline_evidence_from_state(git_state: Mapping[str, Any]) -> dict[str, Any]:
    evidence = git_state.get("evidence") or {}
    offline = evidence.get("offline_evidence") if isinstance(evidence, dict) else None
    return offline if isinstance(offline, dict) else {}


def valid_evidence_hash(value: str) -> bool:
    return bool(EVIDENCE_HASH_RE.fullmatch((value or "").strip()))


def has_done_provenance(git_state: Mapping[str, Any]) -> bool:
    return bool(git_state.get("merged_sha") or offline_evidence_from_state(git_state))


def provenance_summary(git_state: Mapping[str, Any]) -> dict[str, Any]:
    offline = offline_evidence_from_state(git_state)
    if offline:
        return {
            "type": "offline_evidence",
            "terminal": True,
            "label": "Offline evidence",
            "verifier": offline.get("verifier"),
            "reviewed_at": offline.get("reviewed_at"),
            "artifact_url": offline.get("artifact_url"),
            "evidence_hash": offline.get("evidence_hash"),
        }
    if git_state.get("merged_sha"):
        return {
            "type": "github_pr_merged" if git_state.get("pr_number") else "default_branch_commit",
            "terminal": True,
            "label": "Merged code",
            "merged_sha": git_state.get("merged_sha"),
            "pr_number": git_state.get("pr_number"),
            "pr_url": git_state.get("pr_url"),
        }
    if git_state.get("pr_number") or git_state.get("pr_url"):
        return {
            "type": "github_pr_open",
            "terminal": False,
            "label": "PR evidence",
            "pr_number": git_state.get("pr_number"),
            "pr_url": git_state.get("pr_url"),
        }
    if git_state.get("head_sha"):
        return {
            "type": "branch_head",
            "terminal": False,
            "label": "Branch evidence",
            "head_sha": git_state.get("head_sha"),
        }
    return {"type": None, "terminal": False, "label": "No provenance"}
