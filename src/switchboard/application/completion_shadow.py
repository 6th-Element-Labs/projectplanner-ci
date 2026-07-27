"""Read-only comparison of the legacy completion plan and thin reducer.

Shadow observations are audit evidence only.  They never select or execute an
effect and therefore cannot become lifecycle authority.
"""
from __future__ import annotations

import os
import time
from typing import Any, Mapping

from switchboard.domain.completion.normalization_law import NormalizedAction


SCHEMA = "switchboard.completion_shadow_observation.v1"
DIVERGENCE_CLASSES = frozenset({"match", "safe_fail_closed", "unsafe"})
THIN_OBSERVATION_SCHEMA = "switchboard.completion_thin_observation.v1"

_LEGACY_ACTIONS = {
    "wait": NormalizedAction.WAIT.value,
    "attach_and_wait": NormalizedAction.WAIT.value,
    "none": NormalizedAction.WAIT.value,
    "ensure_review_generation": NormalizedAction.START.value,
    "start_remediation": NormalizedAction.START.value,
    "repair_dispatch": NormalizedAction.RETRY_CI.value,
    "mark_ready": NormalizedAction.MARK_READY.value,
    "enqueue": NormalizedAction.ARM_MERGE.value,
    "escalate_human": NormalizedAction.BLOCK.value,
    "fence_runner": NormalizedAction.BLOCK.value,
    "reconcile_provenance": NormalizedAction.MERGED.value,
}


def _timestamp(value: Any) -> float | None:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    return stamp if stamp >= 0 else None


def _duration(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or end < start:
        return None
    return end - start


def compare_shadow_actions(
    *,
    task_id: str,
    snapshot: Mapping[str, Any],
    decision: Mapping[str, Any],
    legacy_plan: Mapping[str, Any],
    normalized: Mapping[str, Any],
    legacy_build_sha: str = "",
    new_build_sha: str = "",
    now: float | None = None,
    fix_landed_at: float | None = None,
    deployed_at: float | None = None,
) -> dict[str, Any]:
    """Compare both controllers over the exact same authoritative snapshot."""
    observed_at = _timestamp(snapshot.get("observed_at"))
    stamp = float(now if now is not None else time.time())
    effect = str(legacy_plan.get("effect") or "").strip().lower()
    legacy_action = _LEGACY_ACTIONS.get(effect, "")
    thin_action = str(normalized.get("action") or "").strip().upper()
    if not legacy_action or not thin_action:
        divergence = "unsafe"
    elif legacy_action == thin_action:
        divergence = "match"
    elif thin_action == NormalizedAction.BLOCK.value:
        # The normalization law may turn an under-evidenced legacy mutation or
        # an expired WAIT into BLOCK.  This is a visible fail-closed difference.
        divergence = "safe_fail_closed"
    else:
        divergence = "unsafe"

    source_times = [
        _timestamp(value)
        for value in dict(snapshot.get("source_observed_at") or {}).values()
    ]
    source_times = [value for value in source_times if value is not None]
    evidence_age = max((stamp - value for value in source_times), default=None)
    fix_landed = _timestamp(
        fix_landed_at if fix_landed_at is not None
        else os.environ.get("SWITCHBOARD_FIX_LANDED_AT")
    )
    deployed = _timestamp(
        deployed_at if deployed_at is not None
        else os.environ.get("SWITCHBOARD_DEPLOYED_AT")
    )
    behavior_changed_at = stamp if legacy_action != thin_action else None

    observation = {
        "schema": SCHEMA,
        "task_id": str(task_id or "").strip().upper(),
        "snapshot_id": str(snapshot.get("snapshot_id") or ""),
        "head_sha": str(snapshot.get("head_sha") or ""),
        "observed_at": observed_at,
        "recorded_at": stamp,
        "legacy_controller_build_sha": (
            legacy_build_sha
            or os.environ.get("SWITCHBOARD_LEGACY_BUILD_SHA")
            or "unavailable"
        ),
        "new_controller_build_sha": (
            new_build_sha
            or str(normalized.get("controller_build_sha") or "")
            or "unavailable"
        ),
        "table_version": str(normalized.get("table_version") or ""),
        "evidence_age_seconds": evidence_age,
        "legacy_action": legacy_action,
        "thin_action": thin_action,
        "divergence_class": divergence,
        "route": str(decision.get("route") or ""),
        "effect": effect,
        "shadow_is_lifecycle_authority": False,
        "scar_candidate": divergence == "unsafe",
        "snapshot_retained": divergence == "unsafe",
        "metrics": {
            "fix_landing_to_deployment_seconds": _duration(fix_landed, deployed),
            "deployment_to_first_fresh_tick_seconds": _duration(
                deployed, observed_at),
            "deployment_to_behavior_change_seconds": _duration(
                deployed, behavior_changed_at),
        },
    }
    # The existing scar-lockdown path consumes retained decision episodes.  An
    # unsafe divergence therefore carries the exact fresh snapshot that caused
    # it; ordinary matches stay compact.
    if divergence == "unsafe":
        observation["snapshot"] = dict(snapshot)
    return observation


def build_thin_observation(
    *,
    task_id: str,
    snapshot: Mapping[str, Any],
    decision: Mapping[str, Any],
    normalized: Mapping[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    """Describe one production tick without introducing another authority."""
    stamp = float(now if now is not None else time.time())
    source_times = [
        _timestamp(value)
        for value in dict(snapshot.get("source_observed_at") or {}).values()
    ]
    source_times = [value for value in source_times if value is not None]
    evidence_age = max((stamp - value for value in source_times), default=None)
    return {
        "schema": THIN_OBSERVATION_SCHEMA,
        "task_id": str(task_id or "").strip().upper(),
        "snapshot_id": str(snapshot.get("snapshot_id") or ""),
        "head_sha": str(snapshot.get("head_sha") or ""),
        "observed_at": _timestamp(snapshot.get("observed_at")),
        "recorded_at": stamp,
        "controller_build_sha": (
            str(normalized.get("controller_build_sha") or "")
            or "unavailable"
        ),
        "table_version": str(normalized.get("table_version") or ""),
        "evidence_age_seconds": evidence_age,
        "action": str(normalized.get("action") or ""),
        "route": str(decision.get("route") or ""),
        "reason_code": str(normalized.get("reason_code") or ""),
        "observation_is_lifecycle_authority": False,
    }


def explain_fresh_snapshot(
    snapshot: Mapping[str, Any],
    normalized: Mapping[str, Any],
    *,
    max_evidence: int = 8,
) -> dict[str, Any]:
    """Bounded task explanation derived only from the current tick snapshot."""
    block = dict(normalized.get("block") or {})
    wait = dict(normalized.get("wait") or {})
    evidence = list(block.get("live_evidence") or wait.get("evidence") or [])
    explanation = {
        "schema": "switchboard.task_block_explanation.v2",
        "task_id": str(snapshot.get("task_id") or "").strip().upper(),
        "snapshot_id": str(snapshot.get("snapshot_id") or ""),
        "head_sha": str(snapshot.get("head_sha") or ""),
        "observed_at": snapshot.get("observed_at"),
        "action": normalized.get("action"),
        "reason_code": normalized.get("reason_code"),
        "blocked": str(normalized.get("action") or "") == "BLOCK",
        "owner": block.get("owner") or wait.get("owner") or normalized.get("owner"),
        "deadline_at": wait.get("deadline_at"),
        "next_observation": wait.get("next_observation"),
        "minimum_repair": block.get("minimum_repair"),
        "live_evidence": evidence[:max(0, int(max_evidence))],
        "derived_from_cached_verdict": False,
    }
    return explanation


__all__ = [
    "DIVERGENCE_CLASSES",
    "SCHEMA",
    "THIN_OBSERVATION_SCHEMA",
    "build_thin_observation",
    "compare_shadow_actions",
    "explain_fresh_snapshot",
]
