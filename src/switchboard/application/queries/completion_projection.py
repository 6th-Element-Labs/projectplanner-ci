"""Shared operator projection for durable completion runs.

GitHub, CI, review, runner, and merge-gate facts are inputs to the completion
owner.  UI surfaces consume its persisted decision instead of independently
reclassifying those inputs.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from switchboard.storage.repositories import completion_runs


SCHEMA = "switchboard.completion_projection.v1"

_ROUTE_OWNER = {
    "wait": "coordinator",
    "review_merge": "review/merge coordinator",
    "remediation": "remediation agent",
    "coordination_retry": "coordinator",
    "human": "operator",
    "reconcile": "reconciler",
    "none": "provenance",
}

_ROUTE_EFFECT = {
    "wait": "wait",
    "review_merge": "start review/merge",
    "remediation": "start remediation",
    "coordination_retry": "retry coordination",
    "human": "await human decision",
    "reconcile": "reconcile merge",
    "none": "none",
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def project_completion(
        run: Optional[Mapping[str, Any]],
        *,
        task: Optional[Mapping[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Return the single UI/API projection for one completion run.

    Canonical task merge provenance wins over an older active run.  Runner
    liveness is intentionally absent: this projection answers what completion
    is doing, not whether a process is alive.
    """
    run = _mapping(run)
    task = _mapping(task)
    git = _mapping(task.get("git_state"))
    merged_sha = str(git.get("merged_sha") or "").strip()
    if not run and not merged_sha:
        return None

    decision = _mapping(_mapping(run.get("evidence_refs")).get("decision"))
    route = str(run.get("route") or "none").strip().lower()
    state = str(run.get("state") or "").strip().lower()
    board_status = str(run.get("board_status") or task.get("status") or "").strip()
    current_effect = str(
        decision.get("effect") or _ROUTE_EFFECT.get(route) or route
    ).strip()

    if merged_sha:
        route = "none"
        state = "done"
        board_status = "Done"
        current_effect = "none"

    return {
        "schema": SCHEMA,
        "task_id": str(run.get("task_id") or task.get("task_id") or ""),
        "pr_number": int(run.get("pr_number") or git.get("pr_number") or 0),
        "head_sha": str(run.get("head_sha") or git.get("head_sha") or ""),
        "state": state or None,
        "route": route,
        "reason_code": str(run.get("reason_code") or ""),
        "route_owner": _ROUTE_OWNER.get(route, "coordinator"),
        "desired_role": str(run.get("desired_role") or ""),
        "desired_head": str(run.get("head_sha") or git.get("head_sha") or ""),
        "retry_deadline": run.get("next_retry_at"),
        "current_effect": current_effect,
        "board_status": board_status or None,
        "attempt": int(run.get("attempt") or 0),
        "state_version": int(run.get("state_version") or 0),
        "merged_sha": merged_sha or None,
        "terminal": bool(merged_sha or state == "done"),
    }


def attach_completion_projection(
        task: Optional[dict[str, Any]],
        *,
        project: str,
        run: Optional[Mapping[str, Any]] = None) -> Optional[dict[str, Any]]:
    if not task:
        return task
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        return task
    try:
        if run is None and "completion_run" in task:
            run = task.get("completion_run")
        elif run is None:
            run = completion_runs.get_active_completion_run(task_id, project=project)
    except Exception:
        # Injected repositories, partial test databases, and rolling deploys can
        # legitimately lack the completion table. This is an additive read
        # projection, so task retrieval must remain available while it catches up.
        return task
    projection = project_completion(run, task=task)
    if run:
        task["completion_run"] = dict(run)
    if projection:
        _attach_blocked_reason(projection, task_id, project=project)
        task["completion_projection"] = projection
    return task


def _attach_blocked_reason(
        projection: dict[str, Any], task_id: str, *, project: str,
        runner_sessions_provider=None) -> None:
    """Name the effect-level failure the run is stalled on, if there is one.

    BUG-189: ``retry_deadline`` said *when* the next attempt was due and nothing
    said *why* the last one failed, so a completion stuck on a config-level
    refusal was indistinguishable from one merely waiting. The external-effect
    ledger holds ``last_error`` the whole time; surface it next to the deadline.

    WATCH-19: a live runner with a silent PTY is also a blocked_reason — progress
    stalled even while heartbeats renew.

    Best-effort by construction: this is an additive read on a projection that
    several surfaces poll, so any failure leaves the projection exactly as it
    was rather than degrading task retrieval.
    """
    if projection.get("terminal"):
        return
    if _attach_progress_fault_blocked_reason(
            projection, task_id, project=project,
            runner_sessions_provider=runner_sessions_provider):
        return
    try:
        from switchboard.storage.repositories import external_effects

        rows = [
            row for row in external_effects.list_external_effects(
                project=project, task_id=task_id, status="failed") or []
            if _mapping(row).get("effect_type") == "completion_effect"
        ]
    except Exception:
        return
    if not rows:
        return
    newest = max(rows, key=lambda row: float(_mapping(row).get("updated_at") or 0))
    newest = _mapping(newest)
    if not newest.get("last_error"):
        return
    projection["blocked_reason"] = str(newest.get("last_error"))
    projection["blocked_effect"] = str(newest.get("resource") or "")
    try:
        projection["blocked_retry_count"] = int(newest.get("retry_count") or 0)
    except (TypeError, ValueError):
        projection["blocked_retry_count"] = 0


def _attach_progress_fault_blocked_reason(
        projection: dict[str, Any], task_id: str, *, project: str,
        runner_sessions_provider=None) -> bool:
    """Surface WATCH-19 progress faults next to completion blocked_reason."""
    try:
        if runner_sessions_provider is None:
            from switchboard.storage.repositories import runner as runner_repo
            rows = runner_repo.list_runner_sessions(
                task_id=task_id, include_stale=False, project=project) or []
        else:
            rows = runner_sessions_provider(
                task_id=task_id, project=project) or []
    except Exception:
        return False
    faulted = []
    for row in rows:
        mapping = _mapping(row)
        if str(mapping.get("status") or "").lower() not in {"ready", "running"}:
            continue
        if mapping.get("stale") is True:
            continue
        metadata = mapping.get("metadata") if isinstance(
            mapping.get("metadata"), dict) else {}
        fault = metadata.get("progress_fault")
        if isinstance(fault, dict) and fault.get("kind") == "runner_progress_stalled":
            faulted.append((mapping, fault))
    if not faulted:
        return False
    session, fault = max(
        faulted,
        key=lambda item: float(item[1].get("raised_at") or 0),
    )
    message = str(fault.get("message") or "").strip()
    age = fault.get("output_age_s")
    projection["blocked_reason"] = (
        message or f"runner_progress_stalled output_age_s={age}"
    )
    if not projection["blocked_reason"].startswith("runner_progress_stalled"):
        projection["blocked_reason"] = (
            f"runner_progress_stalled: {projection['blocked_reason']}"
        )
    projection["blocked_effect"] = str(session.get("runner_session_id") or "")
    projection["blocked_retry_count"] = 0
    projection["progress_fault"] = dict(fault)
    return True


def attach_many(
        tasks: list[dict[str, Any]],
        *,
        project: str) -> list[dict[str, Any]]:
    """Attach projections with one completion-runs query."""
    runs = completion_runs.list_active_completion_runs(
        [row.get("task_id") for row in tasks], project=project)
    for task in tasks:
        task_id = str(task.get("task_id") or "").strip().upper()
        task["completion_run"] = runs.get(task_id)
        attach_completion_projection(task, project=project, run=runs.get(task_id))
    return tasks
