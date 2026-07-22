"""SIMPLIFY-10: the one Task Execution command service.

COORD-44 shipped ``start_task`` alone.  This module completes the command set
that every surface must use, against the SIMPLIFY-1 execution projection:

    get_task_execution   start_task      open_session
    send_message         stop_task       retry_task
    get_execution_transcript

Every command returns the same envelope on REST and on MCP, and every failure
returns the same typed error (``error_code`` + ``failure_class``).  Adapters own
authentication and serialization only — they never resolve a runner id, select a
host, assemble a wake, or author an assignment payload.  ``execution_id`` is the
runner session id: one execution attempt, one durable identity.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from constants import DEFAULT_PROJECT
from switchboard.application.commands import runner_pty as runner_pty_command
from switchboard.application.queries import task_session as task_session_query
from switchboard.security import redact_provider_secrets
from switchboard.storage.repositories import coordination as coordination_repo
from switchboard.storage.repositories import runner as runner_repo

SCHEMA = "switchboard.task_execution.v1"
ERROR_SCHEMA = "switchboard.task_execution_error.v1"
TRANSCRIPT_SCHEMA = "switchboard.execution_transcript.v1"

COMMANDS = (
    "get_task_execution", "start_task", "open_session", "send_message",
    "stop_task", "retry_task", "get_execution_transcript",
)

#: Wake statuses that still own the lifecycle: a second start would fork.
IN_FLIGHT_WAKE_STATUSES = frozenset({"pending", "claimed"})

DEFAULT_WATCH_SCOPES = ("watch", "input", "resize", "signal")
DEFAULT_GRACE_SECONDS = 10

#: One HTTP status per error code so REST and MCP agree on severity, and one
#: fail_fix_signal.v1 failure_class so a refusal is auditable rather than silent.
ERROR_STATUS: dict[str, int] = {
    "invalid_input": 400,
    "task_not_found": 404,
    "execution_not_found": 404,
    "no_active_session": 409,
    "not_running": 409,
    "runner_bind_incomplete": 409,
    "wrong_session": 409,
    "start_refused": 409,
    "control_refused": 502,
    "open_refused": 502,
}
ERROR_FAILURE_CLASS: dict[str, str] = {
    "invalid_input": "invalid_input",
    "task_not_found": "missing_data",
    "execution_not_found": "missing_data",
    "no_active_session": "missing_data",
    "not_running": "missing_data",
    "runner_bind_incomplete": "unbound_identity",
    "wrong_session": "unbound_identity",
    "start_refused": "failed_gate",
    "control_refused": "unreachable_agent",
    "open_refused": "unreachable_agent",
}


class TaskExecutionError(ValueError):
    """One typed refusal that REST and MCP render identically."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    @property
    def status(self) -> int:
        return ERROR_STATUS.get(self.code, 400)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": ERROR_SCHEMA,
            "error": self.message,
            "error_code": self.code,
            "message": self.message,
            "failure_class": ERROR_FAILURE_CLASS.get(self.code, "invalid_input"),
            "refused": True,
            **self.details,
        }


def _envelope(command: str, task_id: str, project: str,
              **fields: Any) -> dict[str, Any]:
    return {"schema": SCHEMA, "command": command, "task_id": task_id,
            "project": project, **fields}


def _normalize(task_id: Any) -> str:
    return str(task_id or "").strip().upper()


def _projection(task_id: str, project: str) -> dict[str, Any]:
    """The SIMPLIFY-1 read model is the sole execution-state authority."""
    projection = task_session_query.execute_for(task_id, project=project)
    if not projection:
        raise TaskExecutionError("task_not_found", "task not found",
                                 task_id=task_id, project=project)
    return projection


def _active_execution_id(projection: dict[str, Any]) -> str:
    runner = projection.get("active_runner") or {}
    return str(runner.get("runner_session_id") or "")


def _attempt_execution_id(projection: dict[str, Any]) -> str:
    """The most recent attempt's runner id, live or terminal."""
    active = _active_execution_id(projection)
    if active:
        return active
    attempt = projection.get("active_attempt") or {}
    return str(attempt.get("runner_session_id") or "")


def _in_flight_wake(projection: dict[str, Any]) -> Optional[dict[str, Any]]:
    attempt = projection.get("active_attempt") or {}
    if str(attempt.get("status") or "") not in IN_FLIGHT_WAKE_STATUSES:
        return None
    if not attempt.get("wake_id"):
        return None
    # A claimed wake whose runner already died is not in flight; the projection
    # has already reclassified it as start_failed_retry.
    if projection.get("lifecycle_phase") == "start_failed_retry":
        return None
    return attempt


def _require_live_runner(projection: dict[str, Any], task_id: str,
                         project: str) -> dict[str, Any]:
    runner = projection.get("active_runner")
    if not runner:
        raise TaskExecutionError(
            "no_active_session",
            "no live execution session for this task",
            task_id=task_id, project=project,
            lifecycle_phase=projection.get("lifecycle_phase"),
            last_dispatch_outcome=projection.get("last_dispatch_outcome"),
        )
    verdict = runner_repo.assert_runner_watchable(runner)
    if not verdict.get("watchable"):
        raise TaskExecutionError(
            "runner_bind_incomplete",
            str(verdict.get("message") or "execution session is not fully bound"),
            task_id=task_id, project=project,
            execution_id=str(runner.get("runner_session_id") or ""),
            missing=list(verdict.get("missing") or []),
        )
    return runner


def _known_code(candidate: Any, fallback: str) -> str:
    """Keep an upstream error_code only when this service can price it."""
    code = str(candidate or "")
    return code if code in ERROR_STATUS else fallback


def _refusal_reason(result: dict[str, Any], action: str) -> str:
    """A refused control request carries its cause under ``result``.

    The top-level ``reason`` is the operator prompt we just sent, not the
    failure, so reading that first reports "operator requested kill" as the
    reason a kill failed.
    """
    detail = result.get("result") if isinstance(result.get("result"), dict) else {}
    return str(result.get("message") or result.get("error") or detail.get("reason")
               or result.get("reason") or f"host refused {action}")


def _try_control(execution_id: str, action: str, *, reason: str,
                 options: dict[str, Any], actor: str, principal_id: str,
                 project: str) -> dict[str, Any]:
    """Request a host control action and report the outcome without raising."""
    result = runner_repo.request_runner_control(
        execution_id, action, reason=reason, options=options, actor=actor,
        principal_id=principal_id, project=project)
    requested = bool(result.get("requested"))
    return {
        "action": action,
        "requested": requested,
        "control_request_id": result.get("request_id"),
        "reason": None if requested else _refusal_reason(result, action),
    }


def _control(execution_id: str, action: str, *, reason: str, options: dict[str, Any],
             actor: str, principal_id: str, project: str,
             error_code: str) -> dict[str, Any]:
    result = runner_repo.request_runner_control(
        execution_id, action, reason=reason, options=options, actor=actor,
        principal_id=principal_id, project=project)
    if result.get("requested"):
        return result
    raise TaskExecutionError(
        _known_code(result.get("error_code") or result.get("error"), error_code),
        _refusal_reason(result, action),
        execution_id=execution_id, action=action, host_result=result,
    )


# --------------------------------------------------------------------------
# 1. get_task_execution — the read model plus which commands are legal now.
# --------------------------------------------------------------------------

def _session_is_terminal(session: dict[str, Any]) -> bool:
    return bool(session.get("stale")) or (
        str(session.get("status") or "").lower() in runner_repo.RUNNER_TERMINAL_STATUSES)


def _queue_capacity_hint(project: str, *, attempt: dict[str, Any],
                         preferred_host_id: str = "") -> dict[str, Any]:
    """Best-effort "behind N on <host>" from online host capacity.

    WATCH-5: when a Connect wake is still pending, the panel must distinguish
    capacity wait from a live session. Prefer an explicitly named host; otherwise
    the most-saturated online host (zero available sessions, highest active).
    """
    from switchboard.application.commands import connect_dispatch
    readback = connect_dispatch.capacity_readback(attempt, project=project)
    hosts = readback.get("matching_online_hosts") or []
    preferred = str(preferred_host_id or "").strip()
    candidates = []
    for host in hosts:
        if not isinstance(host, dict) or host.get("stale"):
            continue
        host_id = str(host.get("host_id") or "").strip()
        if not host_id:
            continue
        if preferred and host_id != preferred:
            continue
        try:
            active = int(host.get("active_sessions") or 0)
        except (TypeError, ValueError):
            active = 0
        available = host.get("available_sessions")
        try:
            available_i = int(available) if available is not None else None
        except (TypeError, ValueError):
            available_i = None
        candidates.append({
            "host_id": host_id,
            "behind_active_runs": active,
            "available_sessions": available_i,
            "saturated": available_i == 0,
        })
    if not candidates:
        return {"capacity": readback}
    # Prefer saturated hosts, then highest active count.
    candidates.sort(key=lambda row: (
        0 if row.get("saturated") else 1,
        -int(row.get("behind_active_runs") or 0),
    ))
    best = candidates[0]
    behind = int(best.get("behind_active_runs") or 0)
    host_id = str(best.get("host_id") or "")
    if behind <= 0 and not best.get("saturated"):
        return {"host_id": host_id, "behind_active_runs": behind,
                "capacity": readback}
    return {
        "host_id": host_id,
        "behind_active_runs": behind,
        "detail": f"Queued behind {behind} active runs on {host_id}",
        "capacity": readback,
    }


def _panel_projection(projection: dict[str, Any], *, project: str) -> dict[str, Any]:
    """WATCH-5: four honest operator states for the task/Watch panel."""
    active_runner = projection.get("active_runner") or {}
    attempt = projection.get("active_attempt") or {}
    wake_status = str(attempt.get("status") or "").strip().lower()
    host_id = str(
        active_runner.get("host_id")
        or attempt.get("host_id")
        or ""
    ).strip()

    if active_runner:
        runner_id = str(active_runner.get("runner_session_id") or "")
        try:
            from switchboard.application import runner_pty_relay as relay
            host_attached = relay.host_attached_for(runner_id)
        except Exception:
            host_attached = None
        if host_attached is False:
            # Explicit False from the relay: Detached (WATCH-4 host_not_attached).
            return {
                "state": "detached",
                "label": "Detached",
                "detail": (
                    "Bridge detached — reconnecting to the host tunnel"
                    + (f" on {host_id}" if host_id else "")
                ),
                "host_id": host_id or None,
                "host_attached": False,
                "wake_status": wake_status or None,
                "behind_active_runs": None,
            }
        # True, or None when this process cannot see the relay: treat as Live.
        # Detached requires an explicit host_attached=false signal.
        return {
            "state": "live",
            "label": "Live",
            "detail": f"Runner live on {host_id or 'host'}"
                      + (" — relay attached" if host_attached is True else ""),
            "host_id": host_id or None,
            "host_attached": host_attached,
            "wake_status": wake_status or None,
            "behind_active_runs": None,
        }

    if wake_status == "claimed":
        return {
            "state": "starting",
            "label": "Starting",
            "detail": (
                f"Host {host_id} claimed the wake — registering the runner"
                if host_id else
                "Wake claimed — registering the runner"
            ),
            "host_id": host_id or None,
            "host_attached": None,
            "wake_status": "claimed",
            "behind_active_runs": None,
        }

    if wake_status == "pending" or _in_flight_wake(projection) is not None:
        hint = _queue_capacity_hint(project, attempt=attempt,
                                    preferred_host_id=host_id)
        behind = hint.get("behind_active_runs")
        hint_host = str(hint.get("host_id") or host_id or "")
        detail = str(hint.get("detail") or "").strip()
        if not detail:
            detail = (
                f"Queued — waiting for a host to claim"
                + (f" ({hint_host})" if hint_host else "")
            )
        return {
            "state": "queued",
            "label": "Queued",
            "detail": detail,
            "host_id": hint_host or None,
            "host_attached": None,
            "wake_status": wake_status or "pending",
            "behind_active_runs": behind,
            "capacity": hint.get("capacity"),
        }

    return {
        "state": "idle",
        "label": "Ready",
        "detail": "No Connect wake is in flight",
        "host_id": None,
        "host_attached": None,
        "wake_status": None,
        "behind_active_runs": None,
    }


def get_task_execution(task_id: Any, *, project: str = DEFAULT_PROJECT) -> dict[str, Any]:
    """Return the one authoritative answer to "what is running" for a task."""
    task_id = _normalize(task_id)
    projection = _projection(task_id, project)
    execution_id = _attempt_execution_id(projection)
    live = bool(projection.get("active_runner"))
    in_flight = _in_flight_wake(projection) is not None
    # UI-58: the task-modal card must choose Resume-review vs Start without
    # reading the runner-watch surface and inspecting sessions itself. Derive
    # that distinction here so the browser reads one server-authoritative answer.
    sessions = runner_repo.list_runner_sessions(
        task_id=task_id, include_stale=True, project=project)
    has_ended_session = (not live) and any(_session_is_terminal(s) for s in sessions)
    task_status = str((projection.get("task") or {}).get("status") or "")
    resumable_review = (
        task_status == "In Review" and not live and not in_flight
        and any(_session_is_terminal(s) for s in sessions))
    panel = _panel_projection(projection, project=project)
    return _envelope(
        "get_task_execution", task_id, project,
        execution_id=execution_id or None,
        lifecycle_phase=projection.get("lifecycle_phase"),
        running=live,
        starting=in_flight,
        panel=panel,
        has_ended_session=has_ended_session,
        resumable_review=resumable_review,
        execution=projection,
        available_commands=sorted({
            "get_task_execution", "get_execution_transcript",
            *(("open_session", "send_message", "stop_task") if live else ()),
            *(("stop_task",) if in_flight else ()),
            *(("start_task",) if not live and not in_flight else ()),
            "retry_task",
        }),
    )


# --------------------------------------------------------------------------
# 2. start_task — attach, dedupe, or launch. Never two sessions for one task.
# --------------------------------------------------------------------------

def start_task(task_id: Any, *, project: str = DEFAULT_PROJECT, actor: str = "user",
               principal_id: str = "", role: str = "implementation",
               runtime: str = "codex", source_sha: str = "",
               instruction: str = "", findings: Optional[list[dict[str, Any]]] = None,
               launcher: Optional[Callable[..., dict[str, Any]]] = None) -> dict[str, Any]:
    """Start or resume THE task session (COORD-44 contract, service-owned).

    Connect is the only component that assembles a wake; callers reach it
    exclusively through this command and never select a host.
    """
    task_id = _normalize(task_id)
    if not task_id:
        raise TaskExecutionError("invalid_input", "task_id required", project=project)
    projection = _projection(task_id, project)
    active_runner = projection.get("active_runner") or {}
    pending = _in_flight_wake(projection)
    if active_runner:
        result = {
            "action": "attach",
            "started": False,
            "attached": True,
            "runner_session_id": active_runner.get("runner_session_id"),
            "host_id": active_runner.get("host_id"),
        }
    elif pending:
        from switchboard.application.commands import connect_dispatch
        result = {
            "action": "starting",
            "started": False,
            "starting": True,
            "attached": False,
            "runner_session_id": pending.get("runner_session_id"),
            "wake_id": pending.get("wake_id"),
            "host_id": pending.get("host_id"),
            "capacity": connect_dispatch.capacity_readback(
                pending, project=project, runtime=runtime,
                lane=str((projection.get("task") or {}).get("_wsId")
                         or (projection.get("task") or {}).get("workstream") or "")),
        }
    elif launcher is None:
        from switchboard.application.commands import connect_dispatch

        task = projection.get("task") or {}
        predecessor = str(
            ((projection.get("last_dispatch_outcome") or {}).get("wake_id")) or "")
        result = connect_dispatch.enqueue_task(
            task, project=project, actor=actor, runtime=runtime,
            predecessor_wake_id=predecessor,
            generation_ref=(f"{role}:{source_sha.lower()}"
                            if role in {"review_merge", "remediation"}
                            and source_sha else ""),
        )
    else:
        # Test/adapter seam retained while all product surfaces use Connect.
        result = launcher(task_id, actor=actor, project=project,
                          principal_id=principal_id, role=role, runtime=runtime,
                          instruction=instruction, findings=list(findings or []))
    if "action" not in result:
        result = {
            **result,
            "action": "started" if result.get("dispatched") else "refused",
            "started": bool(result.get("dispatched")),
            "attached": False,
        }
    action = str(result.get("action") or "")
    if action == "refused":
        raise TaskExecutionError(
            "start_refused",
            str(result.get("reason") or result.get("error") or "start refused"),
            task_id=task_id, project=project,
            start_error=result.get("error"),
            last_dispatch_outcome=result.get("dispatch"),
        )
    execution_id = str(result.get("runner_session_id") or "").strip()
    wake_id = str(result.get("wake_id") or "").strip()
    host_id = str(result.get("host_id") or "").strip()
    descriptor: dict[str, Any] = {}
    if execution_id and result.get("attached"):
        from switchboard.application import runner_pty_relay as _relay
        descriptor = runner_pty_command.mint_ticket_for_session(
            runner_session_id=execution_id,
            project=project,
            scopes=list(DEFAULT_WATCH_SCOPES),
            actor=principal_id or actor,
            host_attached=_relay.host_attached_for(execution_id),
        )
    elif execution_id and wake_id and host_id and action in {"started", "starting"}:
        descriptor = runner_pty_command.mint_ticket_for_pending_direct_session(
            runner_session_id=execution_id,
            task_id=task_id,
            wake_id=wake_id,
            host_id=host_id,
            project=project,
            user_id=principal_id or actor,
            scopes=list(DEFAULT_WATCH_SCOPES),
        )
    return _envelope(
        "start_task", task_id, project,
        action=action or "started",
        started=bool(result.get("started")),
        attached=bool(result.get("attached")),
        execution_id=execution_id or None,
        wake_id=wake_id or None,
        host_id=host_id or None,
        # Role describes the newly created generation only. Attaching to an
        # existing/pending execution must not pretend the caller replaced its
        # lifecycle authority.
        role=role if action == "started" else None,
        intake_routing=result.get("intake_routing") or None,
        lifecycle_phase=("running" if result.get("attached")
                         else "starting" if action in {"started", "starting"} else None),
        transport=descriptor.get("transport"),
        relay_path=descriptor.get("relay_path"),
        relay_url=descriptor.get("relay_url"),
        ticket=descriptor.get("ticket"),
        scopes=descriptor.get("scopes"),
        expires_at=descriptor.get("expires_at"),
        browser_safe=descriptor.get("browser_safe"),
        capacity=result.get("capacity"),
    )


# --------------------------------------------------------------------------
# 3. open_session — the server picks the runner and mints the relay capability.
# --------------------------------------------------------------------------

def open_session(task_id: Any, *, project: str = DEFAULT_PROJECT, actor: str = "user",
                 principal_id: str = "", scopes: Optional[list[str]] = None,
                 ttl_seconds: int = 0) -> dict[str, Any]:
    """Open a watchable terminal on the task's live execution session."""
    task_id = _normalize(task_id)
    projection = _projection(task_id, project)
    runner = _require_live_runner(projection, task_id, project)
    execution_id = str(runner.get("runner_session_id") or "")
    # Nudging the host tunnel is best-effort: a runner that does not advertise
    # ``runner_open`` can still be watched over an already-attached tunnel, so a
    # refusal here must not deny the relay. It is reported, never swallowed.
    host_open = _try_control(
        execution_id, "open", reason=f"open_session {task_id}", options={},
        actor=actor, principal_id=principal_id, project=project)
    from switchboard.application import runner_pty_relay as _relay
    mint_kwargs: dict[str, Any] = {
        "runner_session_id": execution_id,
        "project": project,
        "scopes": list(scopes or DEFAULT_WATCH_SCOPES),
        "actor": actor,
        # WATCH-4: a run proven live by its attached relay tunnel mints a ticket
        # even without a scheduler claim/Work Session.
        "host_attached": _relay.host_attached_for(execution_id),
    }
    if ttl_seconds:
        mint_kwargs["ttl_seconds"] = int(ttl_seconds)
    descriptor = runner_pty_command.mint_ticket_for_session(**mint_kwargs)
    if descriptor.get("error"):
        raise TaskExecutionError(
            _known_code(descriptor.get("error_code"), "open_refused"),
            str(descriptor.get("error")),
            task_id=task_id, project=project, execution_id=execution_id,
            missing=list(descriptor.get("missing") or []) or None,
        )
    browser_safe = bool(descriptor.get("browser_safe"))
    return _envelope(
        "open_session", task_id, project,
        execution_id=execution_id,
        host_id=runner.get("host_id"),
        opened=bool(host_open.get("requested")),
        host_open=host_open,
        browser_safe=browser_safe,
        transport=descriptor.get("transport"),
        relay_path=descriptor.get("relay_path"),
        relay_url=descriptor.get("relay_url"),
        ticket=descriptor.get("ticket"),
        scopes=descriptor.get("scopes"),
        expires_at=descriptor.get("expires_at"),
        # Never invent a URL the browser cannot reach: say so instead (HARDEN
        # fail-fix rule — a fallback must stay visible).
        reason=None if browser_safe else "relay public base is unset or loopback",
    )


# --------------------------------------------------------------------------
# 4. send_message — durable text into the live session, bound to this task.
# --------------------------------------------------------------------------

def send_message(task_id: Any, text: str, *, project: str = DEFAULT_PROJECT,
                 actor: str = "user", principal_id: str = "") -> dict[str, Any]:
    """Queue one operator/agent message for the task's live execution session."""
    task_id = _normalize(task_id)
    message = str(text or "")
    if not message.strip():
        raise TaskExecutionError("invalid_input", "text required",
                                 task_id=task_id, project=project)
    projection = _projection(task_id, project)
    runner = _require_live_runner(projection, task_id, project)
    execution_id = str(runner.get("runner_session_id") or "")
    result = _control(
        execution_id, "inject", reason=f"send_message {task_id}",
        # Host relay INJECT_KINDS accepts session_chat (not chat_text).
        options={"text": message, "task_id": task_id, "kind": "session_chat"},
        actor=actor, principal_id=principal_id, project=project,
        error_code="control_refused")
    return _envelope(
        "send_message", task_id, project,
        execution_id=execution_id,
        host_id=runner.get("host_id"),
        # The host executes the inject; queued is the honest state at this edge.
        queued=True,
        delivered=False,
        control_request_id=result.get("request_id") or result.get("control_request_id"),
        characters=len(message),
    )


# --------------------------------------------------------------------------
# 5. stop_task — end whichever half of the lifecycle is actually live.
# --------------------------------------------------------------------------

def stop_task(task_id: Any, *, project: str = DEFAULT_PROJECT, actor: str = "user",
              principal_id: str = "", reason: str = "operator stop",
              grace_seconds: int = DEFAULT_GRACE_SECONDS) -> dict[str, Any]:
    """Stop the task's execution: kill a live runner and/or cancel a queued start.

    Both halves are stopped in one command, because leaving a queued wake behind
    a killed runner is exactly how a "stopped" task starts itself again.
    """
    task_id = _normalize(task_id)
    projection = _projection(task_id, project)
    stopped = _supersede(projection, task_id, project, actor=actor,
                         principal_id=principal_id, reason=reason,
                         grace_seconds=grace_seconds)
    if not stopped["execution_id"] and not stopped["wake_id"]:
        raise TaskExecutionError(
            "not_running", "nothing is running for this task",
            task_id=task_id, project=project,
            lifecycle_phase=projection.get("lifecycle_phase"))
    return _envelope(
        "stop_task", task_id, project,
        stopped=True,
        execution_id=stopped["execution_id"] or None,
        cancelled_wake_id=stopped["wake_id"] or None,
        killed=bool(stopped["execution_id"]),
        # The host owns process death; the server owns "this is no longer current".
        pending_host_ack=bool(stopped["execution_id"]),
        reason=reason,
    )


def _supersede(projection: dict[str, Any], task_id: str, project: str, *,
               actor: str, principal_id: str, reason: str,
               grace_seconds: int) -> dict[str, str]:
    """Terminate the current attempt. Returns what was actually superseded."""
    result = {"execution_id": "", "wake_id": ""}
    wake = _in_flight_wake(projection)
    if wake:
        cancelled = coordination_repo.cancel_wake(
            str(wake.get("wake_id")), reason=reason, actor=actor, project=project)
        if not cancelled.get("error"):
            result["wake_id"] = str(wake.get("wake_id"))
    runner = projection.get("active_runner")
    if runner and runner.get("runner_session_id"):
        execution_id = str(runner.get("runner_session_id"))
        _control(execution_id, "kill", reason=reason,
                 options={"grace_seconds": int(grace_seconds)}, actor=actor,
                 principal_id=principal_id, project=project,
                 error_code="control_refused")
        result["execution_id"] = execution_id
    return result


# --------------------------------------------------------------------------
# 6. retry_task — supersede the attempt; never fork a second execution.
# --------------------------------------------------------------------------

def retry_task(task_id: Any, *, project: str = DEFAULT_PROJECT, actor: str = "user",
               principal_id: str = "", role: str = "implementation",
               runtime: str = "",
               reason: str = "operator retry",
               launcher: Optional[Callable[..., dict[str, Any]]] = None) -> dict[str, Any]:
    """Replace the current attempt with a new one.

    A queued start is cancelled synchronously, so the replacement launches in the
    same call.  A *live* runner cannot be replaced in one call — the host owns
    process death — so retry stops it and reports ``superseding`` instead of
    launching a second session alongside the first.  Callers poll
    :func:`get_task_execution` and retry again once the runner is terminal.
    """
    task_id = _normalize(task_id)
    projection = _projection(task_id, project)
    active_runner = projection.get("active_runner") or {}
    active_attempt = projection.get("active_attempt") or {}
    selected_runtime = str(
        runtime
        or active_runner.get("runtime")
        or active_attempt.get("runtime")
        or (active_attempt.get("selector") or {}).get("runtime")
        or "codex"
    )
    had_live_runner = bool(projection.get("active_runner"))
    superseded = _supersede(projection, task_id, project, actor=actor,
                            principal_id=principal_id, reason=reason,
                            grace_seconds=DEFAULT_GRACE_SECONDS)
    if had_live_runner:
        return _envelope(
            "retry_task", task_id, project,
            action="superseding",
            started=False,
            superseded_execution_id=superseded["execution_id"] or None,
            superseded_wake_id=superseded["wake_id"] or None,
            message=("Stopping the live session first. Retry again once it is "
                     "terminal; a second session is never started alongside it."),
        )
    started = start_task(task_id, project=project, actor=actor,
                         principal_id=principal_id, role=role,
                         runtime=selected_runtime, launcher=launcher)
    return _envelope(
        "retry_task", task_id, project,
        action=started.get("action") or "started",
        started=bool(started.get("started")),
        attached=bool(started.get("attached")),
        execution_id=started.get("execution_id"),
        wake_id=started.get("wake_id"),
        host_id=started.get("host_id"),
        superseded_execution_id=superseded["execution_id"] or None,
        superseded_wake_id=superseded["wake_id"] or None,
    )


# --------------------------------------------------------------------------
# 7. get_execution_transcript — what one execution actually produced.
# --------------------------------------------------------------------------

def get_execution_transcript(task_id: Any = "", *, execution_id: str = "",
                             project: str = DEFAULT_PROJECT,
                             limit: int = 20) -> dict[str, Any]:
    """Return the durable record for one execution, live or completed.

    Switchboard does not yet persist full PTY scrollback (SIMPLIFY-9 owns the
    single session transport that would capture it).  This returns the audit
    facts that *do* exist — host log tail, snapshot, completed ``logs`` control
    results, and the vendor transcript pointer — and says plainly that it is not
    the complete stream rather than presenting a tail as one.
    """
    task_id = _normalize(task_id)
    execution_id = str(execution_id or "").strip()
    if not task_id and not execution_id:
        raise TaskExecutionError("invalid_input",
                                 "task_id or execution_id required", project=project)
    if execution_id:
        session = runner_repo.get_runner_session(execution_id, project=project)
        if not session:
            raise TaskExecutionError("execution_not_found", "execution not found",
                                     execution_id=execution_id, project=project)
        if task_id and str(session.get("task_id") or "").upper() != task_id:
            raise TaskExecutionError(
                "wrong_session",
                "execution_id belongs to a different task",
                task_id=task_id, project=project, execution_id=execution_id,
                expected_task_id=str(session.get("task_id") or "") or None)
        task_id = task_id or str(session.get("task_id") or "").upper()
    else:
        projection = _projection(task_id, project)
        execution_id = _attempt_execution_id(projection)
        if not execution_id:
            raise TaskExecutionError(
                "execution_not_found",
                "this task has no recorded execution yet",
                task_id=task_id, project=project,
                lifecycle_phase=projection.get("lifecycle_phase"))
        session = runner_repo.get_runner_session(execution_id, project=project)
        if not session:
            raise TaskExecutionError("execution_not_found", "execution not found",
                                     task_id=task_id, execution_id=execution_id,
                                     project=project)

    environment = session.get("environment") or {}
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    segments: list[dict[str, Any]] = []
    tail = str(environment.get("log_tail") or "")
    if tail:
        segments.append({"source": "runner_log_tail", "text": tail,
                         "at": session.get("heartbeat_at")})
    for control in runner_repo.list_runner_control_requests(
            runner_session_id=execution_id, project=project):
        if str(control.get("action") or "") != "logs":
            continue
        payload = control.get("result") if isinstance(control.get("result"), dict) else {}
        text = str(payload.get("log_tail") or payload.get("logs")
                   or payload.get("output") or "")
        if text:
            segments.append({"source": "control_logs", "text": text,
                             "at": control.get("completed_at"),
                             "request_id": control.get("request_id")})
    segments.sort(key=lambda row: float(row.get("at") or 0))
    if limit and len(segments) > limit:
        segments = segments[-int(limit):]

    status = str(session.get("status") or "")
    return redact_provider_secrets({
        "schema": TRANSCRIPT_SCHEMA,
        "command": "get_execution_transcript",
        "task_id": task_id,
        "project": project,
        "execution_id": execution_id,
        "host_id": session.get("host_id"),
        "status": status,
        "stale": bool(session.get("stale")),
        "terminal": status in runner_repo.RUNNER_TERMINAL_STATUSES,
        "started_at": session.get("started_at"),
        "ended_at": session.get("ended_at"),
        "failure_reason": environment.get("failure_reason"),
        "transcript_ref": (metadata.get("transcript_ref") or metadata.get("session_url")
                           or metadata.get("provider_session_id") or None),
        "log_path": environment.get("log_path"),
        "segments": segments,
        # Named, visible partiality — never a tail dressed up as the stream.
        "complete": False,
        "incomplete_reason": (
            "Switchboard retains host log tails, snapshots and completed logs "
            "control results; durable full-session capture lands with the single "
            "session transport (SIMPLIFY-9)."),
        "generated_at": time.time(),
    })


# --------------------------------------------------------------------------
# Adapter helpers: identical bodies on both transports.
# --------------------------------------------------------------------------

_DISPATCH: dict[str, Callable[..., dict[str, Any]]] = {
    "get_task_execution": get_task_execution,
    "start_task": start_task,
    "open_session": open_session,
    "send_message": send_message,
    "stop_task": stop_task,
    "retry_task": retry_task,
    "get_execution_transcript": get_execution_transcript,
}


def execute_mapping_result(command: str, /, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run one command and return its envelope or its typed error, never raise.

    REST and MCP both call this, so an operator and an agent see byte-identical
    bodies for the same request.
    """
    handler = _DISPATCH.get(command)
    if handler is None:
        return TaskExecutionError("invalid_input", f"unknown command: {command}",
                                  command=command).as_dict()
    try:
        return handler(*args, **kwargs)
    except TaskExecutionError as exc:
        return {**exc.as_dict(), "command": command}


def error_status(result: dict[str, Any]) -> int:
    """HTTP status for a refusal envelope (REST only; the body is unchanged)."""
    return ERROR_STATUS.get(str(result.get("error_code") or ""), 400)
