"""Authoritative task execution-session projection.

The records behind this view (wake intents, claims, Work Sessions, runners and
task agent-state) are audit facts.  Consumers must not independently resolve
those facts into a second answer to "what is running?".
"""
from __future__ import annotations

from typing import Any, Optional

from switchboard.storage.repositories import coordination as coordination_repo
from switchboard.storage.repositories import deliverables as deliverables_repo
from switchboard.storage.repositories import runner as runner_repo
from switchboard.storage.repositories import tasks as tasks_repo


SCHEMA = "switchboard.task_session.v1"
TERMINAL_RUNNERS = {
    "completed", "failed", "cancelled", "expired", "lost", "killed",
    "exited", "stopped",
}


def _deliverable(task_id: str, project: str) -> Optional[dict[str, Any]]:
    links = deliverables_repo.list_task_deliverable_links(task_id, project=project)
    if not links:
        return None
    link = links[0]
    deliverable_id = str(link.get("deliverable_id") or "").strip()
    if not deliverable_id:
        return link
    return deliverables_repo.get_deliverable(deliverable_id, project=project) or link


def _wake_runner_id(wake: dict[str, Any]) -> str:
    result = wake.get("result") if isinstance(wake.get("result"), dict) else {}
    return str(result.get("runner_session_id") or wake.get("runner_session_id") or "").strip()


def _attempt(wake: Optional[dict[str, Any]], sessions: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not wake:
        return None
    wake_id = str(wake.get("wake_id") or "")
    launched_id = _wake_runner_id(wake)
    matching = [s for s in sessions if (
        str((s.get("metadata") or {}).get("wake_id") or "") == wake_id
        or (launched_id and str(s.get("runner_session_id") or "") == launched_id)
    )]
    runner = matching[0] if matching else None
    policy = wake.get("policy") if isinstance(wake.get("policy"), dict) else {}
    assignment = policy.get("assignment") if isinstance(policy.get("assignment"), dict) else {}
    selector = wake.get("selector") if isinstance(wake.get("selector"), dict) else {}
    return {
        "wake_id": wake_id,
        "status": wake.get("status"),
        "requested_at": wake.get("requested_at"),
        "claimed_at": wake.get("claimed_at"),
        "completed_at": wake.get("completed_at"),
        "host_id": wake.get("claimed_by_host") or (runner or {}).get("host_id") or None,
        "runner_session_id": (runner or {}).get("runner_session_id") or launched_id or None,
        "runner_status": (runner or {}).get("status"),
        "runner": runner,
        "role": assignment.get("role") or (policy.get("lifecycle") or {}).get("role"),
        "agent_id": (runner or {}).get("agent_id") or selector.get("agent_id"),
        "runtime": (runner or {}).get("runtime") or selector.get("runtime"),
        "execution_mode": policy.get("mode"),
        "result": wake.get("result") or {},
    }


def _phase(task: dict[str, Any], attempt: Optional[dict[str, Any]],
           active_runner: Optional[dict[str, Any]], outcome: dict[str, Any]) -> str:
    if active_runner:
        return "running"
    if attempt and str(attempt.get("status") or "") in {"pending", "claimed"}:
        if str(attempt.get("runner_status") or "").lower() in TERMINAL_RUNNERS:
            return "start_failed_retry"
        return "starting"
    if outcome:
        state = str(outcome.get("state") or "")
        return "starting" if state in {"queued", "dispatching"} else "start_failed_retry"
    status = str(task.get("status") or "").lower().replace(" ", "_")
    return {"in_review": "review", "done": "merged"}.get(status, status or "ready")


def execute_for(task_id: str, *, project: str) -> Optional[dict[str, Any]]:
    """Return the only public read model for one task's execution session."""
    task_id = str(task_id or "").strip().upper()
    task = tasks_repo.get_task(task_id, project=project)
    if not task:
        return None
    sessions = runner_repo.list_runner_sessions(
        task_id=task_id, include_stale=True, project=project)
    resolution = runner_repo.resolve_task_active_runner(
        task_id, agent_state=task.get("agent_state") or {}, sessions=sessions,
        project=project)
    active_runner = resolution.get("session") if resolution.get("active") else None
    wakes = coordination_repo.list_wake_intents(task_id=task_id, project=project, limit=100)
    wakes = sorted(wakes, key=lambda row: float(row.get("requested_at") or 0), reverse=True)
    latest_wake = wakes[0] if wakes else None
    attempt = _attempt(latest_wake, sessions)
    outcome = runner_repo.latest_dispatch_outcome(task_id, project=project)

    # A claimed wake plus an already-terminal runner is not "dispatching".  The
    # host has supplied the stronger fact, so preserve its own reason and expose
    # the explicit retry state.
    if (attempt and str(attempt.get("status") or "") in {"pending", "claimed"}
            and str(attempt.get("runner_status") or "").lower() in TERMINAL_RUNNERS):
        runner = next((s for s in sessions if s.get("runner_session_id")
                       == attempt.get("runner_session_id")), {})
        metadata = runner.get("metadata") if isinstance(runner.get("metadata"), dict) else {}
        reason = str(metadata.get("failure_reason") or "runner exited before start completed")
        outcome = {
            "state": "launch_failed", "wake_id": attempt.get("wake_id"),
            "wake_status": attempt.get("status"), "reason": reason,
            "message": f"The last dispatch failed: {reason}", "retry_available": True,
        }

    git = task.get("git_state") if isinstance(task.get("git_state"), dict) else {}
    metadata = active_runner.get("metadata") if active_runner and isinstance(
        active_runner.get("metadata"), dict) else {}
    host_id = ((active_runner or {}).get("host_id")
               or (attempt or {}).get("host_id") or None)
    return {
        "schema": SCHEMA,
        "project": project,
        "task": task,
        "deliverable": _deliverable(task_id, project),
        "lifecycle_phase": _phase(task, attempt, active_runner, outcome),
        "active_attempt": attempt,
        "active_host": {"host_id": host_id} if host_id else None,
        "active_runner": active_runner,
        "last_dispatch_outcome": outcome or None,
        "pr_head": ({"branch": git.get("branch"), "head_sha": git.get("head_sha"),
                     "pr_url": git.get("pr_url"), "pr_number": git.get("pr_number")}
                    if any(git.get(k) for k in ("branch", "head_sha", "pr_url", "pr_number"))
                    else None),
        "transcript_ref": (metadata.get("transcript_ref") or metadata.get("session_url")
                           or metadata.get("provider_session_id") or None),
    }
