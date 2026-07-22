"""Durable adapter from task Start to the content-blind Connect plane.

Task Execution decides *that* a task needs a process.  This adapter records the
opaque assignment and asks the existing durable wake substrate to deliver it.
It deliberately does not create prompts, credentials, workflow roles, claims,
Work Sessions, review policy, or completion instructions.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
from typing import Any

from switchboard.connect import Assignment, ResourceLimits
from switchboard.storage.repositories import coordination as coordination_repo


CONNECT_WAKE_MODE = "connect"
_RUNTIMES = {
    "codex": ("codex", "openai"),
    "openai": ("codex", "openai"),
    "claude": ("claude-code", "anthropic"),
    "claude-code": ("claude-code", "anthropic"),
    "anthropic": ("claude-code", "anthropic"),
    "cursor": ("cursor", "cursor"),
}


def _runtime(value: str) -> tuple[str, str]:
    selected = _RUNTIMES.get(str(value or "codex").strip().lower())
    if not selected:
        raise ValueError("unsupported_runtime")
    return selected


def _assignment_id(project: str, task_id: str, runtime: str, predecessor: str) -> str:
    source = f"{project}:{task_id}:{runtime}:{predecessor or 'initial'}"
    return "assignment-" + hashlib.sha256(source.encode()).hexdigest()[:24]


def _queued_at(task: dict[str, Any], assignment_id: str) -> float:
    """Return a stable sequence timestamp for an idempotent assignment payload.

    Task rows carry durable update/create timestamps. Using that snapshot makes
    concurrent Start calls byte-identical; a wall-clock value here would reuse
    one idempotency key with two different request hashes. The digest fallback
    exists only for adapters/tests that provide a partial task row.
    """

    for field in ("updated_at", "created_at"):
        try:
            value = float(task.get(field) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    offset = int(hashlib.sha256(assignment_id.encode()).hexdigest()[:8], 16)
    return float(1_700_000_000 + (offset % 100_000_000))


#: Wake states that end a dispatch generation; a new Start must chain past them.
_TERMINAL_WAKE_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _latest_terminal_wake_id(task_id: str, project: str) -> str:
    """The newest wake for this task when it is terminal, else "".

    BUG-133: callers resolve their predecessor from ``last_dispatch_outcome``,
    which only surfaces *failed* dispatches. A wake that COMPLETED (the runner
    started, ran, and exited) leaves no predecessor there, so a resume replayed
    the ``initial`` idempotency key -- and any ordinary task edit since the
    first start had changed the payload hash, turning the replay into a raw
    "idempotency conflict" instead of a replacement runner. Chaining past any
    terminal newest wake mints a fresh generation for every restart.
    """
    try:
        rows = coordination_repo.list_wake_intents(
            task_id=task_id, project=project, newest_first=True, limit=1)
    except Exception:
        # Best-effort read: an environment without a wake store simply has no
        # predecessor to chain past; behave exactly as before this lookup existed.
        return ""
    if rows and str(rows[0].get("status") or "") in _TERMINAL_WAKE_STATUSES:
        return str(rows[0].get("wake_id") or "")
    return ""


def enqueue_task(
    task: dict[str, Any],
    *,
    project: str,
    actor: str,
    runtime: str = "codex",
    predecessor_wake_id: str = "",
) -> dict[str, Any]:
    """Persist one provider-neutral assignment for any Start surface."""

    task_id = str(task.get("task_id") or "").strip().upper()
    if not task_id:
        return {"dispatched": False, "error": "task_id_required"}
    try:
        runtime_name, provider = _runtime(runtime)
    except ValueError as exc:
        return {"dispatched": False, "error": str(exc), "runtime": runtime}
    if not predecessor_wake_id:
        predecessor_wake_id = _latest_terminal_wake_id(task_id, project)
    lane = str(task.get("_wsId") or task.get("workstream") or "").strip()
    assignment_id = _assignment_id(
        project, task_id, runtime_name, str(predecessor_wake_id or ""))
    assignment = Assignment(
        assignment_id=assignment_id,
        principal_ref=f"agent/{runtime_name}/{task_id.lower()}",
        work_ref=f"task:{project}:{task_id}",
        runtime=runtime_name,
        provider=provider,
        workspace_ref="repo:canonical",
        limits=ResourceLimits(
            max_runtime_seconds=int(os.environ.get("PM_CONNECT_MAX_RUNTIME_SECONDS", "7200")),
            spend_limit_microunits=int(
                os.environ.get("PM_CONNECT_SPEND_LIMIT_MICROUNITS", "0")),
            memory_limit_bytes=int(os.environ.get("PM_CONNECT_MEMORY_LIMIT_BYTES", "0")),
        ),
        queued_at=_queued_at(task, assignment_id),
    )
    selector = {
        "runtime": runtime_name,
        "provider": provider,
        "lane": lane,
        "agent_id": assignment.principal_ref,
        "task_id": task_id,
    }
    policy = {
        "mode": CONNECT_WAKE_MODE,
        "assignment": {
            "schema": "switchboard.connect.assignment.v1",
            **asdict(assignment),
        },
    }
    suffix = str(predecessor_wake_id or "initial")
    wake = coordination_repo.request_wake(
        selector=selector,
        reason=f"Connect assignment {task_id}",
        source="connect",
        policy=policy,
        task_id=task_id,
        actor=actor,
        project=project,
        idem_key=f"connect-start:v1:{project}:{task_id}:{runtime_name}:{suffix}",
    )
    if str(wake.get("error") or "") == "idempotency conflict":
        # Another start owns this generation with a different request body (a
        # race, or a non-terminal predecessor). Name the condition instead of
        # leaking the storage layer's conflict string to the operator panel.
        return {
            "dispatched": False,
            "error": "dispatch_generation_conflict",
            "reason": ("another start already owns this dispatch generation; "
                       "wait for it to finish or retry"),
            "task_id": task_id,
            "project": project,
        }
    if wake.get("error") or not wake.get("wake_id"):
        return {
            "dispatched": False,
            "error": wake.get("error") or wake.get("reason") or "wake_not_created",
            "task_id": task_id,
            "project": project,
        }
    return {
        "dispatched": True,
        "task_id": task_id,
        "project": project,
        "wake_id": wake["wake_id"],
        "wake_status": wake.get("status"),
        "assignment_id": assignment.assignment_id,
        "runtime": runtime_name,
        "provider": provider,
        "execution_mode": CONNECT_WAKE_MODE,
    }
