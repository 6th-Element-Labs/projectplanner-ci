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

from typing import Any

from constants import DEFAULT_PROJECT
from switchboard.storage.repositories import autopilot_scopes as scopes_repo

SCHEMA = "switchboard.autopilot.v1"
ERROR_SCHEMA = "switchboard.autopilot_error.v1"

COMMANDS = ("get_autopilot", "control_autopilot", "autopilot_coverage")

#: The operator verbs, exactly the REST body's action Literal. ``start`` routes
#: to ``start_autopilot_scope``; the rest to ``control_autopilot_scope``.
ACTIONS = ("start", "pause", "resume", "stop")

ERROR_STATUS: dict[str, int] = {
    "invalid_input": 400,
    "deliverable_not_found": 404,
    "task_not_linked": 409,
    "structural_blocker": 409,
    "no_active_scope": 409,
}
ERROR_FAILURE_CLASS: dict[str, str] = {
    "invalid_input": "invalid_input",
    "deliverable_not_found": "missing_data",
    "task_not_linked": "invalid_input",
    "structural_blocker": "failed_gate",
    "no_active_scope": "missing_data",
}

#: Map the store's bare-string failures to a typed code. Order matters: the
#: first substring that matches wins, so the specific cases precede the generic.
_STORE_ERROR_CODES: tuple[tuple[str, str], ...] = (
    ("unknown deliverable", "deliverable_not_found"),
    ("unknown replacement deliverable", "deliverable_not_found"),
    ("not linked to deliverable", "task_not_linked"),
    ("does not preserve task scope links", "task_not_linked"),
    ("structurally ineligible", "structural_blocker"),
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
                      actor: str = "user", agent_id: str = "",
                      ) -> dict[str, Any]:
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
    kind = str(scope_type or "deliverable").strip().lower()
    # A task scope is identified by its task, a deliverable scope by its
    # deliverable. Demanding a deliverable_id for a standalone task scope made
    # such a scope armable but not stoppable: Start could create one, and every
    # pause/resume/stop then refused because there was no deliverable to name.
    if kind == "task":
        if not str(task_id or "").strip():
            raise AutopilotError("invalid_input", "task_id required for a task scope",
                                 project=project)
    elif not deliverable_id:
        raise AutopilotError("invalid_input",
                             "deliverable_id required for a deliverable scope",
                             project=project)
    runtime = str(runtime or "codex").strip().lower()
    common = {
        "project": project, "profile_id": profile_id,
        "deliverable_id": deliverable_id,
        "scope_type": kind,
        "task_project": task_project, "task_id": task_id, "actor": actor,
    }
    if verb == "start" and common["scope_type"] == "task":
        invalid = scopes_repo.validate_autopilot_target(
            project=project, deliverable_id=deliverable_id, scope_type="task",
            task_project=task_project, task_id=task_id, runtime=runtime)
        if invalid:
            _raise_store_error(invalid)
    if verb == "start":
        result = scopes_repo.start_autopilot_scope(**common, runtime=runtime)
    else:
        result = scopes_repo.control_autopilot_scope(**common, action=verb)
    if isinstance(result, dict) and result.get("error"):
        _raise_store_error(result)
    if verb == "start":
        # Operator Start is the v4 mission creation boundary. Initialize every
        # exact task covered by the new/reused scope before the scoped worker
        # can observe it; creation and mission_started append are idempotent.
        from switchboard.application.commands import mission_journal
        if kind == "task":
            mission_journal.create_mission(
                str(task_id).strip().upper(),
                project=task_project or project,
                requested_role="implementation",
            )
        else:
            from switchboard.storage.repositories import deliverables
            status = deliverables.get_mission_status(
                project=project, deliverable_id=deliverable_id)
            for linked in status.get("linked_tasks") or []:
                linked_task_id = str(linked.get("task_id") or "").strip().upper()
                linked_project = str(
                    linked.get("project_id") or linked.get("task_project") or project
                ).strip()
                if linked_task_id:
                    mission_journal.create_mission(
                        linked_task_id,
                        project=linked_project,
                        requested_role="implementation",
                    )
    return {
        "schema": SCHEMA, "command": "control_autopilot", "project": project,
        "action": verb, **_scope_fields(deliverable_id, common["scope_type"],
                                        task_project, task_id),
        "scope": result,
    }


def autopilot_coverage(task_ids: Any, *, project: str = DEFAULT_PROJECT,
                       task_project: str = "",
                       profile_id: str = "autopilot-default") -> dict[str, Any]:
    """Batched per-task coverage read for the Fleet dock (UI-66).

    For each task id: which scope covers it (deliverable via task links, or a
    standalone task scope) and an honest liveness verdict — ``armed`` / ``live``
    / ``stale`` / ``paused`` — derived from the holder lease, never from
    ``status`` alone (a restart-killed scope keeps status "active" while
    nothing ticks).
    """
    if isinstance(task_ids, str):
        task_ids = [part.strip() for part in task_ids.split(",")]
    wanted = [str(item or "").strip() for item in (task_ids or [])]
    wanted = [item for item in wanted if item]
    if not wanted:
        raise AutopilotError("invalid_input", "task_ids required",
                             project=project)
    if len(wanted) > 100:
        raise AutopilotError("invalid_input", "at most 100 task_ids per call",
                             requested=len(wanted), project=project)
    coverage = scopes_repo.autopilot_coverage_for_tasks(
        wanted, project=project, task_project=task_project,
        profile_id=profile_id)
    return {
        "schema": SCHEMA, "command": "autopilot_coverage", "project": project,
        "coverage": coverage,
    }


_DISPATCH: dict[str, Callable[..., dict[str, Any]]] = {
    "get_autopilot": get_autopilot,
    "control_autopilot": control_autopilot,
    "autopilot_coverage": autopilot_coverage,
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
