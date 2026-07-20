"""UI-58: the one deliverable/task Autopilot command service.

The Start/Pause/Resume/Stop controls had a REST surface calling ``store.*``
directly and no command layer or MCP tool, so "the UI displays the same state
returned through MCP" could not be proven. This is the one service both
transports adapt to: REST (``/api/deliverables/{id}/autopilot`` and the task
variant) and MCP (``get_autopilot`` / ``control_autopilot``) call
:func:`execute_mapping_result` and return its body verbatim, so an operator and
an agent see byte-identical envelopes and identical typed errors.

The durable scope lifecycle stays in ``storage.repositories.autopilot_scopes``;
this layer only maps the four operator verbs onto it and normalizes the store's
bare-string failures into a typed envelope. It never selects a host, assembles a
wake, or resolves a runner id.
"""
from __future__ import annotations

from typing import Any, Callable

from constants import DEFAULT_PROJECT
from switchboard.storage.repositories import autopilot_scopes as scopes_repo

SCHEMA = "switchboard.autopilot.v1"
ERROR_SCHEMA = "switchboard.autopilot_error.v1"

COMMANDS = ("get_autopilot", "control_autopilot")

#: The operator verbs, exactly the REST body's action Literal. ``start`` routes
#: to ``start_autopilot_scope``; the rest to ``control_autopilot_scope``.
ACTIONS = ("start", "pause", "resume", "stop")

ERROR_STATUS: dict[str, int] = {
    "invalid_input": 400,
    "deliverable_not_found": 404,
    "task_not_linked": 409,
    "no_active_scope": 409,
}
ERROR_FAILURE_CLASS: dict[str, str] = {
    "invalid_input": "invalid_input",
    "deliverable_not_found": "missing_data",
    "task_not_linked": "invalid_input",
    "no_active_scope": "missing_data",
}

#: Map the store's bare-string failures to a typed code. Order matters: the
#: first substring that matches wins, so the specific cases precede the generic.
_STORE_ERROR_CODES: tuple[tuple[str, str], ...] = (
    ("unknown deliverable", "deliverable_not_found"),
    ("unknown replacement deliverable", "deliverable_not_found"),
    ("not linked to deliverable", "task_not_linked"),
    ("does not preserve task scope links", "task_not_linked"),
    ("live autopilot scope not found", "no_active_scope"),
)


class AutopilotError(ValueError):
    """One typed refusal that REST and MCP render identically."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

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


def _normalize_deliverable(deliverable_id: Any) -> str:
    return str(deliverable_id or "").strip()


def _raise_store_error(result: dict[str, Any]) -> None:
    """Translate a store ``{"error": ...}`` dict into a typed AutopilotError."""
    message = str(result.get("error") or "autopilot request refused")
    lowered = message.lower()
    code = "invalid_input"
    for needle, mapped in _STORE_ERROR_CODES:
        if needle in lowered:
            code = mapped
            break
    details = {k: v for k, v in result.items() if k != "error"}
    raise AutopilotError(code, message, **details)


def _scope_fields(deliverable_id: str, scope_type: str, task_project: str,
                  task_id: str) -> dict[str, Any]:
    kind = str(scope_type or "deliverable").strip().lower()
    fields: dict[str, Any] = {"deliverable_id": deliverable_id, "scope_type": kind}
    if kind == "task":
        fields["task_project"] = task_project or None
        fields["task_id"] = (task_id or "").strip().upper() or None
    return fields


def get_autopilot(deliverable_id: Any, *, project: str = DEFAULT_PROJECT,
                  profile_id: str = "autopilot-default") -> dict[str, Any]:
    """Return every live (active/paused) scope for one deliverable cockpit."""
    deliverable_id = _normalize_deliverable(deliverable_id)
    if not deliverable_id:
        raise AutopilotError("invalid_input", "deliverable_id required",
                             project=project)
    from switchboard.storage.repositories import deliverables as deliverables_repo
    if not deliverables_repo.get_deliverable(
            deliverable_id, project=project, include_task_snapshots=False):
        raise AutopilotError("deliverable_not_found", "unknown deliverable",
                             deliverable_id=deliverable_id, project=project)
    scopes = scopes_repo.list_autopilot_scopes(
        project=project, profile_id=profile_id, deliverable_id=deliverable_id,
        status="active,paused", limit=500)
    return {
        "schema": SCHEMA, "command": "get_autopilot", "project": project,
        "deliverable_id": deliverable_id, "scopes": scopes,
    }


def control_autopilot(deliverable_id: Any, *, project: str = DEFAULT_PROJECT,
                      action: str = "start", scope_type: str = "deliverable",
                      task_project: str = "", task_id: str = "",
                      runtime: str = "codex", profile_id: str = "autopilot-default",
                      actor: str = "user") -> dict[str, Any]:
    """Start, pause, resume, or stop one durable Autopilot scope.

    ``start`` creates (or idempotently readbacks) a scope; the other three move
    an existing live scope. The verb is validated here so an unknown action is
    refused before it reaches the store.
    """
    deliverable_id = _normalize_deliverable(deliverable_id)
    verb = str(action or "").strip().lower()
    if verb not in ACTIONS:
        raise AutopilotError(
            "invalid_input",
            f"action must be one of {', '.join(ACTIONS)}",
            action=action, deliverable_id=deliverable_id, project=project)
    if not deliverable_id:
        raise AutopilotError("invalid_input", "deliverable_id required",
                             project=project)
    common = {
        "project": project, "profile_id": profile_id,
        "deliverable_id": deliverable_id,
        "scope_type": str(scope_type or "deliverable").strip().lower(),
        "task_project": task_project, "task_id": task_id, "actor": actor,
    }
    if verb == "start":
        result = scopes_repo.start_autopilot_scope(**common, runtime=runtime)
    else:
        result = scopes_repo.control_autopilot_scope(**common, action=verb)
    if isinstance(result, dict) and result.get("error"):
        _raise_store_error(result)
    return {
        "schema": SCHEMA, "command": "control_autopilot", "project": project,
        "action": verb, **_scope_fields(deliverable_id, common["scope_type"],
                                        task_project, task_id),
        "scope": result,
    }


_DISPATCH: dict[str, Callable[..., dict[str, Any]]] = {
    "get_autopilot": get_autopilot,
    "control_autopilot": control_autopilot,
}


def execute_mapping_result(command: str, /, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run one command and return its envelope or its typed error, never raise.

    REST and MCP both call this, so an operator and an agent see byte-identical
    bodies for the same request.
    """
    handler = _DISPATCH.get(command)
    if handler is None:
        return AutopilotError("invalid_input", f"unknown command: {command}",
                              command=command).as_dict()
    try:
        return handler(*args, **kwargs)
    except AutopilotError as exc:
        return {**exc.as_dict(), "command": command}


def error_status(result: dict[str, Any]) -> int:
    """HTTP status for a refusal envelope (REST only; the body is unchanged)."""
    return ERROR_STATUS.get(str(result.get("error_code") or ""), 400)
