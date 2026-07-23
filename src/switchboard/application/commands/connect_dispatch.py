"""Durable adapter from task Start to the content-blind Connect plane.

Task Execution decides *that* a task needs a process.  This adapter records the
opaque assignment and asks the existing durable wake substrate to deliver it.
It deliberately does not create prompts, credentials, workflow roles, claims,
Work Sessions, review policy, or completion instructions.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from typing import Any

from switchboard.application.session_boot import ADVERTISED_LAUNCH_RUNTIMES
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
_UNSUPPORTED_RUNTIME_REPAIR = (
    "Call start_task with a supported runtime; do not use runtime=cli. "
    "Connect boots the CLI worker. From a launcher session do not claim_task."
)


def _runtime(value: str) -> tuple[str, str]:
    selected = _RUNTIMES.get(str(value or "codex").strip().lower())
    if not selected:
        raise ValueError("unsupported_runtime")
    return selected


def unsupported_runtime_payload(requested_runtime: str) -> dict[str, Any]:
    """Structured refusal for unknown Connect launch runtimes."""
    return {
        "dispatched": False,
        "error": "unsupported_runtime",
        "requested_runtime": str(requested_runtime or ""),
        "supported_runtimes": list(ADVERTISED_LAUNCH_RUNTIMES),
        "reason": _UNSUPPORTED_RUNTIME_REPAIR,
        "repair": _UNSUPPORTED_RUNTIME_REPAIR,
        "message": _UNSUPPORTED_RUNTIME_REPAIR,
    }


def _assignment_id(project: str, task_id: str, runtime: str, generation: str) -> str:
    source = f"{project}:{task_id}:{runtime}:{generation or 'initial'}"
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


def capacity_readback(
    wake: dict[str, Any], *, project: str, runtime: str = "", lane: str = "",
) -> dict[str, Any]:
    """Join one pending wake to the online hosts that could claim it.

    This is deliberately a read model: wake intents and host heartbeats remain
    the sources of truth.  Start callers therefore learn whether they are
    queued without introducing a second capacity state that could go stale.
    """
    selector = wake.get("selector") if isinstance(wake.get("selector"), dict) else {}
    runtime = str(runtime or selector.get("runtime") or "").strip()
    lane = str(lane or selector.get("lane") or "").strip()
    try:
        hosts = coordination_repo.list_agent_hosts(
            runtime=runtime, lane=lane, project=project) or []
        pending = coordination_repo.list_wake_intents(
            status="pending", runtime=runtime, project=project) or []
    except Exception as exc:
        return {
            "schema": "switchboard.connect.capacity_readback.v1",
            "wake_id": str(wake.get("wake_id") or "") or None,
            "wake_status": str(wake.get("status") or "") or None,
            "queue_position": None,
            "pending_ahead": None,
            "matching_online_hosts": [],
            "matching_online_host_count": 0,
            "readback_error": {
                "reason": "capacity_readback_unavailable",
                "detail": str(exc),
            },
        }
    matching_hosts: list[dict[str, Any]] = []
    for host in hosts:
        if (not isinstance(host, dict) or host.get("stale") or host.get("error")
                or str(host.get("status") or "online").lower() != "online"):
            continue
        capacity = host.get("capacity") if isinstance(host.get("capacity"), dict) else {}
        limits = host.get("limits") if isinstance(host.get("limits"), dict) else {}
        try:
            active = int(capacity.get("active_sessions") or 0)
        except (TypeError, ValueError):
            active = 0
        raw_max = limits.get("max_sessions")
        try:
            maximum = int(raw_max) if raw_max is not None else None
        except (TypeError, ValueError):
            maximum = None
        raw_available = host.get("available_sessions")
        try:
            available = int(raw_available) if raw_available is not None else (
                max(0, maximum - active) if maximum is not None else None)
        except (TypeError, ValueError):
            available = None
        matching_hosts.append({
            "host_id": str(host.get("host_id") or ""),
            "display_name": str(host.get("display_name") or "") or None,
            "active_sessions": active,
            "max_sessions": maximum,
            "available_sessions": available,
        })

    wake_id = str(wake.get("wake_id") or "")
    pending = [
        row for row in pending
        if isinstance(row, dict) and not row.get("error")
        and (not lane or str((row.get("selector") or {}).get("lane") or "") == lane)
    ]
    queue_position = next(
        (index for index, row in enumerate(pending, start=1)
         if str(row.get("wake_id") or "") == wake_id), None)
    ahead = max(0, queue_position - 1) if queue_position is not None else None
    result: dict[str, Any] = {
        "schema": "switchboard.connect.capacity_readback.v1",
        "wake_id": wake_id or None,
        "wake_status": str(wake.get("status") or "") or None,
        "queue_position": queue_position,
        "pending_ahead": ahead,
        "matching_online_hosts": matching_hosts,
        "matching_online_host_count": len(matching_hosts),
    }
    if not matching_hosts:
        result["no_capacity"] = {
            "reason": "no_matching_online_hosts",
            "runtime": runtime or None,
            "lane": lane or None,
        }
    return result


def enqueue_task(
    task: dict[str, Any],
    *,
    project: str,
    actor: str,
    runtime: str = "codex",
    predecessor_wake_id: str = "",
    generation_ref: str = "",
    role: str = "implementation",
    caller_agent_id: str = "",
    principal_id: str = "",
    source_sha: str = "",
    reason_code: str = "",
    acceptance_findings: list[dict[str, Any]] | None = None,
    route: str = "",
    decision_attempt: int = 0,
    state_version: int = 0,
) -> dict[str, Any]:
    """Persist one provider-neutral assignment for any Start surface.

    ``generation_ref`` is an opaque lifecycle generation supplied by Task
    Execution.  Connect neither parses nor persists its meaning; it only uses
    the value to make repeated requests for one generation idempotent.  This is
    what lets an exact-head review dispatch re-arm for a new head without
    replaying forever after a terminal runner on the old head.
    """

    task_id = str(task.get("task_id") or "").strip().upper()
    if not task_id:
        return {"dispatched": False, "error": "task_id_required"}
    try:
        runtime_name, provider = _runtime(runtime)
    except ValueError as exc:
        if str(exc) == "unsupported_runtime":
            return unsupported_runtime_payload(runtime)
        return {"dispatched": False, "error": str(exc), "runtime": runtime}
    generation_ref = str(generation_ref or "").strip()
    if not predecessor_wake_id and not generation_ref:
        predecessor_wake_id = _latest_terminal_wake_id(task_id, project)
    lane = str(task.get("_wsId") or task.get("workstream") or "").strip()
    generation = generation_ref or str(predecessor_wake_id or "")
    assignment_id = _assignment_id(project, task_id, runtime_name, generation)
    execution_agent_id = str(caller_agent_id or "").strip()
    assignment = Assignment(
        assignment_id=assignment_id,
        principal_ref=(execution_agent_id
                       or f"agent/{runtime_name}/{task_id.lower()}"),
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
        # A named generation must remain byte-identical even when unrelated
        # task narration/activity updates the task row between coordinator ticks.
        queued_at=(_queued_at({}, assignment_id) if generation_ref
                   else _queued_at(task, assignment_id)),
    )
    selector = {
        "runtime": runtime_name,
        "provider": provider,
        "lane": lane,
        "agent_id": assignment.principal_ref,
        "task_id": task_id,
    }
    selector["capabilities"] = [
        "execution_lease_v2", "runner_lease_enforcement"]
    policy = {
        "mode": CONNECT_WAKE_MODE,
        "assignment": {
            "schema": "switchboard.connect.assignment.v1",
            **asdict(assignment),
        },
        # Assignment v1 is an adapter compatibility boundary. Server-owned
        # execution identity travels beside it so older hosts can continue to
        # decode the Assignment byte-for-byte.
        "lifecycle": {
            "schema": "switchboard.execution_lifecycle.v1",
            "task_id": task_id,
            "role": str(role or "implementation"),
            "head_sha": str(
                source_sha or (task.get("git_state") or {}).get("head_sha") or ""),
            "reason_code": str(reason_code or ""),
            "acceptance_findings": list(acceptance_findings or []),
            "route": str(route or ""),
            "attempt": int(decision_attempt or 0),
            "state_version": int(state_version or 0),
            "ttl_seconds": int(
                os.environ.get("PM_CONNECT_MAX_RUNTIME_SECONDS", "7200")),
        },
    }
    # The external effect represents one durable completion decision, not the
    # coordinator/lease that happened to request it. Generation and fence are
    # allocated atomically below and intentionally do not participate either.
    lifecycle = policy["lifecycle"]
    if lifecycle["role"] in {"review_merge", "remediation"}:
        policy["effect_identity"] = {
            "schema": "switchboard.completion_effect_identity.v1",
            "task_id": task_id,
            "head_sha": lifecycle["head_sha"],
            "route": lifecycle["route"],
            "role": lifecycle["role"],
            "reason_code": lifecycle["reason_code"],
            "attempt": lifecycle["attempt"],
            "state_version": lifecycle["state_version"],
            "acceptance_findings_hash": hashlib.sha256(json.dumps(
                lifecycle["acceptance_findings"], sort_keys=True,
                separators=(",", ":"), default=str).encode()).hexdigest(),
        }
    suffix = generation_ref or str(predecessor_wake_id or "initial")
    wake = coordination_repo.request_wake(
        selector=selector,
        reason=f"Connect assignment {task_id}",
        source="connect",
        policy=policy,
        task_id=task_id,
        actor=actor,
        principal_id=principal_id,
        caller_agent_id=caller_agent_id,
        enforce_task_ownership=True,
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
    capacity = capacity_readback(
        wake, project=project, runtime=runtime_name, lane=lane)
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
        "capacity": capacity,
    }
