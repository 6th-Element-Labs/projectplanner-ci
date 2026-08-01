"""Read-only v1 versus Mission Bot v4 shadow comparison.

The shadow evaluates both controllers over one hydrated authority snapshot.  It
has no Task Execution effect port and never advances the v4 journal cursor.
Persisting the returned observation to the ordinary activity audit is the only
permitted write; that record has no lifecycle authority.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from switchboard.application.mission_bot.driver import hydrate_mission_snapshot
from switchboard.domain.mission_bot import reduce_mission
from switchboard.domain.mission_bot_v4 import decide_mission_transition
from switchboard.storage.repositories import activity, autopilot_scopes
from switchboard.storage.repositories.mission_journal import (
    MissionJournalRepository,
    default_mission_journal_repository,
)


SHADOW_SCHEMA = "switchboard.mission_bot_v4.shadow_comparison.v1"
SHADOW_BATCH_SCHEMA = "switchboard.mission_bot_v4.shadow_batch.v1"
SHADOW_ACTIVITY_KIND = "mission_bot_v4.shadow_comparison"

_V1_PAGE_ROLES = {
    "START_IMPLEMENTATION": "implementation",
    "START_REMEDIATION": "remediation",
    "START_REVIEW": "review_merge",
    # V1 performs these provider effects itself.  V4 deliberately pages the
    # already-requested review role so the LLM performs them through the
    # existing fenced commands.
    "MARK_READY": "review_merge",
    "ARM_MERGE": "review_merge",
}
_V1_WAIT_REASONS_THAT_V4_MAY_PAGE = frozenset({
    "github_pr_fetch_unavailable",
    "required_exact_head_ci_pending",
    "no_actionable_mission",
})


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _human_request(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    work_session = _map(snapshot.get("work_session"))
    blocker = _map(_map(work_session.get("hygiene")).get("blocker"))
    if blocker:
        return blocker
    task = _map(snapshot.get("task"))
    return _map(
        task.get("human_blocker")
        or task.get("agent_requires_human")
        or snapshot.get("agent_requires_human")
        or snapshot.get("attention")
    )


def _v4_context(
    *,
    project: str,
    task_id: str,
    snapshot: Mapping[str, Any],
    mission: Mapping[str, Any],
    scope_verdict: Mapping[str, Any],
) -> dict[str, Any]:
    dependency_state = _map(
        snapshot.get("dependency_state")
        or _map(snapshot.get("task")).get("dependency_state")
    )
    terminal = (
        str(mission.get("state") or "").upper() == "DONE"
        and bool(mission.get("terminal_kind"))
        and bool(mission.get("terminal_ref"))
    )
    return {
        "project": project,
        "task_id": task_id,
        # The acting v4 worker receives an exact W2 authority token.  Never
        # infer that authority from the snapshot's latest task-scope row: a
        # deliverable scope legitimately covers many tasks and is absent from
        # that task-only projection.
        "scope_active": scope_verdict.get("allowed") is True,
        "scope_id": str(
            _map(scope_verdict.get("authority")).get("scope_id") or ""
        ),
        "terminal_provenance": terminal,
        "dependencies_satisfied": dependency_state.get("satisfied") is True,
        "mission_state": str(mission.get("state") or ""),
        # hydrate_mission_snapshot obtains this from the runner_sessions-backed
        # task-session query.  Both decisions therefore see the same bounded
        # liveness observation instead of racing two Capacity reads.
        "runner_live": bool(_map(snapshot.get("runner")).get("live")),
        "runner_liveness_source": "runner_sessions",
        "requested_role": str(mission.get("requested_role") or ""),
        "handled_through": int(mission.get("handled_through") or 0),
        "latest_sequence": int(mission.get("latest_sequence") or 0),
        "human_request": _human_request(snapshot),
    }


def _comparison(
    v1: Mapping[str, Any],
    v4: Mapping[str, Any],
    context: Mapping[str, Any],
) -> tuple[str, str]:
    output = str(v1.get("output") or "")
    action = str(v4.get("action") or "")
    v4_state = str(v4.get("state") or "").upper()
    v1_reason = str(v1.get("reason_code") or "")
    v4_role = str(v4.get("requested_role") or context.get("requested_role") or "")

    if action == "block_release":
        return "blocked", str(v4.get("reason") or "v4_release_blocked")

    if output == "OBSERVE_MERGED":
        if v4_state == "DONE":
            return "match", "terminal_provenance"
        return "divergence", "terminal_projection_missing"

    if output == "AGENT_REQUIRES_HUMAN":
        if v4_state == "HUMAN" and action == "wait":
            return "match", "authenticated_human_park"
        return "divergence", "human_park_missing"

    expected_role = _V1_PAGE_ROLES.get(output)
    if expected_role:
        if action != "start_task":
            return "divergence", "v4_failed_to_page"
        if v4_role != expected_role:
            return "divergence", "requested_role_mismatch"
        if output in {"MARK_READY", "ARM_MERGE"}:
            return "pager_equivalent", "v4_pages_review_role_for_provider_effect"
        return "match", "same_requested_role"

    if output != "WAIT":
        return "divergence", "unknown_v1_output"

    if action == "wait":
        return "match", "both_wait"

    if action == "start_task":
        scope_terminal = v1_reason.startswith("coordination_scope_")
        safety_wait = v1_reason in {"live_runner_in_progress", "unmet_dependencies"}
        queue_wait = v1_reason.startswith("merge_queue_")
        may_page = (
            v1_reason in _V1_WAIT_REASONS_THAT_V4_MAY_PAGE or queue_wait
        )
        if scope_terminal or safety_wait:
            return "divergence", "v4_pages_across_v1_safety_wait"
        if may_page and v4_role == "review_merge":
            return "pager_equivalent", "v4_pages_review_role_for_material_event"
        return "divergence", "unexpected_v4_page"

    return "divergence", "unknown_v4_action"


def compare_shadow_decisions(
    *,
    project: str,
    task_id: str,
    snapshot: Mapping[str, Any],
    mission: Mapping[str, Any] | None,
    scope_verdict: Mapping[str, Any] | None = None,
    observed_at: float | None = None,
) -> dict[str, Any]:
    """Compare both proposals without executing or persisting either one."""
    normalized_task = str(task_id or "").strip().upper()
    stamp = float(observed_at if observed_at is not None else time.time())
    v1 = reduce_mission(snapshot)
    effective_mission = dict(mission) if mission is not None else None
    merge_provenance = _map(snapshot.get("merge_provenance"))
    canonical_merge_ref = str(merge_provenance.get("merged_sha") or "").strip()
    terminal_projection_simulated = False
    if (
        effective_mission is not None
        and canonical_merge_ref
        and not (
            str(effective_mission.get("state") or "").upper() == "DONE"
            and effective_mission.get("terminal_kind") == "github_merge"
            and str(effective_mission.get("terminal_ref") or "").strip()
            == canonical_merge_ref
        )
    ):
        # The live v4 tick projects already-persisted canonical provenance
        # before deciding.  Shadow mode mirrors that state transition in memory
        # only; it never writes the journal or impersonates Done authority.
        effective_mission.update({
            "state": "DONE",
            "terminal_kind": "github_merge",
            "terminal_ref": canonical_merge_ref,
        })
        terminal_projection_simulated = True
    validated_scope = _map(scope_verdict)
    if validated_scope.get("allowed") is not True:
        comparison_class = "blocked"
        reason = str(
            validated_scope.get("error") or "scope_authority_required"
        )
        context = {}
        v4 = {
            "state": "UNAVAILABLE",
            "action": "block_release",
            "reason": reason,
        }
    elif effective_mission is None:
        comparison_class, reason = "blocked", "v4_mission_missing"
        context: dict[str, Any] = {}
        v4: dict[str, Any] = {
            "state": "UNAVAILABLE",
            "action": "block_release",
            "reason": reason,
        }
    elif "runner_sessions" not in _map(snapshot.get("source_observed_at")):
        comparison_class, reason = "blocked", "runner_liveness_source_missing"
        context = {}
        v4 = {
            "state": "UNAVAILABLE",
            "action": "block_release",
            "reason": reason,
        }
    else:
        context = _v4_context(
            project=project,
            task_id=normalized_task,
            snapshot=snapshot,
            mission=effective_mission,
            scope_verdict=validated_scope,
        )
        v4 = decide_mission_transition(context)
        comparison_class, reason = _comparison(v1, v4, context)

    input_identity = {
        "project": project,
        "task_id": normalized_task,
        "snapshot_id": str(snapshot.get("snapshot_id") or ""),
        "head_sha": str(snapshot.get("head_sha") or ""),
        "source_observed_at": _map(snapshot.get("source_observed_at")),
        "v4_context": context,
        "mission_version": int(_map(effective_mission).get("version") or 0),
        "v4_terminal_projection_simulated": terminal_projection_simulated,
        "scope_authority": {
            key: _map(validated_scope.get("authority")).get(key)
            for key in ("scope_id", "generation", "fence_epoch", "lease_id")
        },
    }
    fingerprint = hashlib.sha256(
        _canonical_json(input_identity).encode("utf-8")
    ).hexdigest()
    release_blocked = comparison_class in {"blocked", "divergence"}
    return {
        "schema": SHADOW_SCHEMA,
        "project": project,
        "task_id": normalized_task,
        "observed_at": stamp,
        "input_fingerprint": f"sha256:{fingerprint}",
        "snapshot_id": input_identity["snapshot_id"],
        "head_sha": input_identity["head_sha"],
        "source_observed_at": input_identity["source_observed_at"],
        "runner_liveness_source": "runner_sessions",
        "scope_authority_validated": validated_scope.get("allowed") is True,
        "scope_id": str(
            _map(validated_scope.get("authority")).get("scope_id") or ""
        ),
        "v1": {
            "authoritative": True,
            "output": v1.get("output"),
            "reason": v1.get("reason_code"),
            "role": v1.get("role"),
            "idempotency_key": v1.get("idempotency_key"),
        },
        "v4": {
            "shadow": True,
            "state": v4.get("state"),
            "action": v4.get("action"),
            "reason": v4.get("reason"),
            "role": v4.get("requested_role") or context.get("requested_role"),
            "event_pointer": v4.get("event_pointer"),
            "failure": v4.get("failure"),
        },
        "comparison_class": comparison_class,
        "comparison_reason": reason,
        "release_blocked": release_blocked,
        "cutover_compatible": not release_blocked,
        "cutover_authorized": False,
        "effect_port_bound": False,
        "shadow_is_lifecycle_authority": False,
        "v4_terminal_projection_simulated": terminal_projection_simulated,
        "permitted_write": "activity_audit_only",
    }


def record_shadow_observation(
    observation: Mapping[str, Any],
    *,
    project: str,
    actor: str,
) -> dict[str, Any]:
    """Persist one compact audit row without changing lifecycle state."""
    task_id = str(observation.get("task_id") or "").strip().upper()
    activity_id = activity.append_activity(
        SHADOW_ACTIVITY_KIND,
        actor,
        dict(observation),
        task_id=task_id,
        project=project,
    )
    return {
        "schema": "switchboard.mission_bot_v4.shadow_audit_receipt.v1",
        "activity_id": activity_id,
        "task_id": task_id,
        "recorded": True,
        "lifecycle_mutation": False,
    }


def run_shadow_comparison(
    task_id: str,
    *,
    project: str,
    actor: str,
    scope_authority: Mapping[str, Any] | None,
    scope_project: str = "",
    journal: MissionJournalRepository = default_mission_journal_repository,
    hydrator: Callable[..., Mapping[str, Any]] = hydrate_mission_snapshot,
    recorder: Callable[..., Mapping[str, Any]] = record_shadow_observation,
    scope_validator: Callable[..., Mapping[str, Any]] = (
        autopilot_scopes.validate_autopilot_scope_authority
    ),
) -> dict[str, Any]:
    """Read both paths, compare once, and require an auditable receipt."""
    snapshot = hydrator(task_id, project=project, actor=actor)
    mission = journal.get_item(task_id, project=project)
    scope_verdict = scope_validator(
        dict(scope_authority or {}),
        project=scope_project or project,
        task_project=project,
        task_id=str(task_id or "").strip().upper(),
    ) or {}
    observation = compare_shadow_decisions(
        project=project,
        task_id=task_id,
        snapshot=snapshot,
        mission=mission,
        scope_verdict=scope_verdict,
    )
    receipt = recorder(observation, project=project, actor=actor)
    if receipt.get("recorded") is not True:
        raise RuntimeError("shadow_observation_not_recorded")
    return {**observation, "audit_receipt": dict(receipt)}


def run_shadow_batch(
    task_ids: Iterable[str],
    *,
    project: str,
    actor: str,
    scope_authority: Mapping[str, Any] | None,
    scope_project: str = "",
    journal: MissionJournalRepository = default_mission_journal_repository,
    hydrator: Callable[..., Mapping[str, Any]] = hydrate_mission_snapshot,
    recorder: Callable[..., Mapping[str, Any]] = record_shadow_observation,
    scope_validator: Callable[..., Mapping[str, Any]] = (
        autopilot_scopes.validate_autopilot_scope_authority
    ),
) -> dict[str, Any]:
    """Run one bounded comparison per supplied task and summarize truthfully."""
    rows = [
        run_shadow_comparison(
            task_id,
            project=project,
            actor=actor,
            scope_authority=scope_authority,
            scope_project=scope_project,
            journal=journal,
            hydrator=hydrator,
            recorder=recorder,
            scope_validator=scope_validator,
        )
        for task_id in dict.fromkeys(
            str(value or "").strip().upper() for value in task_ids
            if str(value or "").strip()
        )
    ]
    blockers = [row for row in rows if row["release_blocked"]]
    return {
        "schema": SHADOW_BATCH_SCHEMA,
        "project": project,
        "observation_count": len(rows),
        "comparison_counts": {
            name: sum(row["comparison_class"] == name for row in rows)
            for name in ("match", "pager_equivalent", "blocked", "divergence")
        },
        "passed": bool(rows) and not blockers,
        "release_blocked": bool(blockers) or not rows,
        "cutover_authorized": False,
        "blocker_count": len(blockers) + (1 if not rows else 0),
        "blocker_task_ids": [row["task_id"] for row in blockers],
        "observations": rows,
    }


__all__ = [
    "SHADOW_ACTIVITY_KIND",
    "SHADOW_BATCH_SCHEMA",
    "SHADOW_SCHEMA",
    "compare_shadow_decisions",
    "record_shadow_observation",
    "run_shadow_batch",
    "run_shadow_comparison",
]
