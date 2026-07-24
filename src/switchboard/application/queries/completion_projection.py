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
        task["completion_projection"] = projection
    return task


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
