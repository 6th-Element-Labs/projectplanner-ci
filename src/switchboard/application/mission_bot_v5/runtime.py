"""Production ports and one bounded Mission Bot v5 tick."""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from switchboard.application.commands import (
    capacity_mission_events,
    github_mission_events,
    task_execution,
)
from switchboard.application.commands import (
    mission_journal as mission_journal_commands,
)
from switchboard.application.mission_bot_v5.worker import (
    ScopedMissionWorkerPorts,
    tick_scoped_mission,
)
from switchboard.domain.provenance import offline_evidence_from_state
from switchboard.storage.repositories import autopilot_scopes, runner, tasks
from switchboard.storage.repositories.mission_journal import (
    MissionJournalError,
    MissionJournalRepository,
    default_mission_journal_repository,
)
from switchboard.storage.repositories.mission_launch_attempts import (
    default_mission_launch_attempt_repository,
)

_CI_FAILURE_STATES = frozenset({
    "action_required", "cancelled", "error", "failure", "stale",
    "startup_failure", "timed_out",
})


def _wait(task_id: str, reason: str, **details: Any) -> dict[str, Any]:
    return {
        "schema": "switchboard.mission_worker_tick.v5",
        "task_id": task_id,
        "action": "wait",
        "reason": reason,
        "mutations": 0,
        **details,
    }


def production_ports(
    *,
    actor: str,
    agent_id: str,
    scope_project: str,
    journal: MissionJournalRepository = default_mission_journal_repository,
    store_mod: Any = None,
) -> ScopedMissionWorkerPorts:
    """Bind only the W2 scope, C1 liveness, and W1 start boundaries."""

    def resolve(name: str, fallback: Any) -> Any:
        candidate = getattr(store_mod, name, None) if store_mod is not None else None
        return candidate if callable(candidate) else fallback

    scope_validator = resolve(
        "validate_autopilot_scope_authority",
        autopilot_scopes.validate_autopilot_scope_authority,
    )
    task_reader = resolve("get_task", tasks.get_task)
    liveness_reader = resolve("task_has_live_execution", runner.task_has_live_execution)

    def validate(authority: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        return scope_validator(
            dict(authority),
            project=scope_project,
            task_project=str(kwargs.get("task_project") or kwargs.get("project") or ""),
            task_id=str(kwargs.get("task_id") or ""),
        )

    def get_task(task_id: str, *, project: str) -> Mapping[str, Any] | None:
        return task_reader(task_id, project=project)

    def has_live(task_id: str, *, project: str) -> bool:
        return bool(liveness_reader(task_id, project=project))

    def start(task_id: str, **kwargs: Any) -> Mapping[str, Any]:
        authority = dict(kwargs.pop("scope_authority"))
        verdict = validate(
            authority,
            project=str(kwargs["project"]),
            task_project=str(kwargs["project"]),
            task_id=task_id,
        )
        if verdict.get("allowed") is not True:
            return {
                "error": verdict.get("error") or "scope_authority_denied",
                "reason_codes": list(verdict.get("reason_codes") or []),
                "refused": True,
            }
        try:
            return task_execution.start_task(
                task_id,
                project=str(kwargs["project"]),
                actor=actor,
                agent_id=agent_id,
                operator_launch_authorized=True,
                scope_launch_authorized=True,
                role=str(kwargs["role"]),
                runtime=str(os.environ.get("PM_MISSION_BOT_V5_RUNTIME") or "codex"),
                codex_profile=str(
                    os.environ.get("PM_MISSION_BOT_V5_CODEX_PROFILE") or ""
                ),
                source_sha=str(kwargs.get("source_sha") or ""),
                instruction=str(kwargs.get("instruction") or ""),
                mission_key=str(kwargs.get("mission_key") or ""),
            )
        except task_execution.TaskExecutionError as exc:
            return exc.as_dict()

    return ScopedMissionWorkerPorts(
        validate_scope=validate,
        get_task=get_task,
        has_live_execution=has_live,
        start_task=start,
        journal=journal,
        launch_attempts=default_mission_launch_attempt_repository,
        max_launch_attempts=max(
            1, int(os.environ.get("PM_MISSION_BOT_V5_MAX_LAUNCH_ATTEMPTS", "3")),
        ),
        retry_base_seconds=max(
            1, int(os.environ.get("PM_MISSION_BOT_V5_RETRY_BASE_SECONDS", "15")),
        ),
    )


def project_terminal_provenance(
    task_id: str,
    *,
    project: str,
    actor: str,
    journal: MissionJournalRepository = default_mission_journal_repository,
    task_reader: Callable[..., Mapping[str, Any] | None] = tasks.get_task,
) -> dict[str, Any]:
    """Copy already-persisted canonical Done truth into the mission row."""
    task = task_reader(task_id, project=project)
    if not task:
        return {"projected": False, "reason": "task_not_found"}
    if str(task.get("status") or "") != "Done":
        return {"projected": False, "reason": "task_not_terminal"}
    git_state = task.get("git_state")
    git_state = git_state if isinstance(git_state, Mapping) else {}
    merged_sha = str(git_state.get("merged_sha") or "").strip().lower()
    offline = offline_evidence_from_state(git_state)
    if merged_sha and bool(git_state.get("in_main_content")):
        terminal_kind = "github_merge"
        terminal_ref = merged_sha
    elif offline:
        terminal_kind = "offline"
        terminal_ref = str(offline.get("evidence_hash") or "").strip().lower()
    else:
        return {"projected": False, "reason": "terminal_provenance_missing"}
    if not terminal_ref:
        return {"projected": False, "reason": "terminal_provenance_missing"}
    try:
        receipt = journal.record_terminal_provenance(
            task_id,
            project=project,
            terminal_kind=terminal_kind,
            terminal_ref=terminal_ref,
            actor=actor,
        )
    except MissionJournalError as exc:
        return {"projected": False, "reason": exc.code, "message": str(exc)}
    return {
        "projected": receipt.get("recorded") is True,
        "reason": (
            "terminal_provenance_projected"
            if receipt.get("recorded") is True
            else str(receipt.get("reason") or "terminal_projection_failed")
        ),
        "terminal_kind": terminal_kind,
        "terminal_ref": terminal_ref,
        "receipt": dict(receipt),
    }


def project_ci_remediation(
    task_id: str,
    *,
    project: str,
    task: Mapping[str, Any],
    journal: MissionJournalRepository = default_mission_journal_repository,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """Route an exact-head CI failure twice, then park the mission."""
    item = journal.get_item(task_id, project=project)
    if not item or item.get("state") == "DONE":
        return {"projected": False, "reason": "mission_not_active"}
    head_sha = str((task.get("git_state") or {}).get("head_sha") or "").lower()
    if not head_sha:
        return {"projected": False, "reason": "current_head_missing"}
    events: list[dict[str, Any]] = []
    cursor = 0
    for _page in range(20):
        page = journal.list_events(
            task_id, project=project, after_sequence=cursor, limit=201,
        )
        events.extend(page)
        if len(page) < 201:
            break
        cursor = int(page[-1]["sequence"])
    else:
        return {"projected": False, "reason": "mission_history_too_large"}
    status_events = [
        event for event in events
        if event.get("event_type") == "github_changed"
        and str(event.get("head_sha") or "").lower() == head_sha
        and (event.get("payload") or {}).get("status_state")
    ]
    latest_by_context: dict[str, dict[str, Any]] = {}
    for event in status_events:
        context = str((event.get("payload") or {}).get("status_context") or "")
        latest_by_context[context] = event
    failures = [
        event for event in latest_by_context.values()
        if str((event.get("payload") or {}).get("status_state") or "").lower()
        in _CI_FAILURE_STATES
    ]
    if not failures:
        return {"projected": False, "reason": "current_head_ci_failure_missing"}
    failure = max(failures, key=lambda event: int(event["sequence"]))
    projection_ref = f"v5-ci-remediation:{failure['event_id']}"
    projected_events = [
        event for event in events
        if event.get("event_type") == "task_changed"
    ]
    projected = [
        event for event in events
        if event.get("event_type") == "task_changed"
        and str((event.get("payload") or {}).get("command_ref") or "")
        == "mission_bot_v5_ci_remediation"
    ]
    existing_projection = next((
        event for event in projected_events
        if str(event.get("external_ref") or "") == projection_ref
    ), None)
    if existing_projection and str(
        (existing_projection.get("payload") or {}).get("command_ref") or ""
    ) == "mission_bot_v5_ci_exhausted":
        return {
            "projected": False, "reason": "ci_remediation_exhausted",
            "retry_count": len(projected), "state": "WAITING",
            "head_sha": head_sha,
        }
    if existing_projection:
        return {"projected": False, "reason": "ci_failure_already_projected"}
    if len(projected) >= max(1, int(max_attempts)):
        event = journal.append_event(
            task_id, project=project, event_type="task_changed",
            source_plane="coordination", idempotency_key=projection_ref,
            head_sha=head_sha, external_ref=projection_ref,
            payload={
                "change_ref": projection_ref,
                "changed_fields": ["state"],
                "command_ref": "mission_bot_v5_ci_exhausted",
            },
        )
        updated = journal.update_item(
            task_id, project=project, state="WAITING",
            requested_role=str(item["requested_role"]),
            expected_version=int(item["version"]),
            handled_through=int(event["sequence"]),
        )
        return {
            "projected": True, "reason": "ci_remediation_exhausted",
            "retry_count": len(projected), "state": updated["state"],
            "head_sha": head_sha,
        }
    event = journal.append_event(
        task_id, project=project, event_type="task_changed",
        source_plane="coordination", idempotency_key=projection_ref,
        head_sha=head_sha, external_ref=projection_ref,
        payload={
            "change_ref": projection_ref,
            "changed_fields": ["requested_role"],
            "command_ref": "mission_bot_v5_ci_remediation",
        },
    )
    updated = journal.update_item(
        task_id, project=project, state="ACTIVE", requested_role="remediation",
        expected_version=int(item["version"]),
        handled_through=max(
            int(item.get("handled_through") or 0), int(failure["sequence"]),
        ),
    )
    return {
        "projected": True, "reason": "ci_failure_routed_to_remediation",
        "retry_count": len(projected) + 1,
        "event_sequence": int(event["sequence"]),
        "requested_role": updated["requested_role"], "head_sha": head_sha,
    }


def run_scoped_mission_tick(
    task_id: str,
    *,
    project: str,
    scope_project: str,
    scope_authority: Mapping[str, Any],
    actor: str,
    agent_id: str,
    journal: MissionJournalRepository = default_mission_journal_repository,
    store_mod: Any = None,
) -> dict[str, Any]:
    """Refresh durable facts and run the simple v5 pager once."""
    ports = production_ports(
        actor=actor,
        agent_id=agent_id,
        scope_project=scope_project,
        journal=journal,
        store_mod=store_mod,
    )
    authority = ports.validate_scope(
        dict(scope_authority), project=project, task_project=project, task_id=task_id,
    ) or {}
    if authority.get("allowed") is not True:
        return _wait(
            task_id,
            str(authority.get("error") or "scope_inactive"),
            reason_codes=list(authority.get("reason_codes") or []),
        )

    task_reader = getattr(store_mod, "get_task", None)
    if not callable(task_reader):
        task_reader = tasks.get_task
    task = task_reader(task_id, project=project) or {}
    if not task:
        return _wait(task_id, "task_not_found")

    bootstrap = None
    if journal.get_item(task_id, project=project) is None:
        try:
            bootstrap = journal.create_mission(
                task_id,
                project=project,
                requested_role=mission_journal_commands.initial_requested_role(task),
            )
        except Exception as exc:  # preserve the storage cause at the boundary
            return _wait(
                task_id,
                "mission_bootstrap_failed",
                error=str(getattr(exc, "code", type(exc).__name__)),
                message=str(exc),
            )

    terminal = project_terminal_provenance(
        task_id,
        project=project,
        actor=actor,
        journal=journal,
        task_reader=task_reader,
    )
    if str(task.get("status") or "") == "Done" and not terminal.get("projected"):
        return _wait(task_id, str(terminal.get("reason")), terminal_projection=terminal)

    # C3 is the hard role handoff.  The journal copies its already-persisted
    # receipt; Mission Bot does not derive the role from status or provider data.
    try:
        handoff = journal.project_review_handoff(
            task_id, project=project, actor=actor, task=task_reader(task_id, project=project) or {},
        )
    except MissionJournalError as exc:
        return _wait(task_id, exc.code, message=str(exc))
    if handoff.get("release_blocked") is True:
        return _wait(task_id, str(handoff.get("reason")), review_handoff_projection=handoff)

    try:
        capacity = capacity_mission_events.project_capacity_events(
            project=project,
            task_id=task_id,
            repository=journal,
            list_wakes=getattr(store_mod, "list_wake_intents", None),
            list_runners=getattr(store_mod, "list_runner_sessions", None),
        )
    except capacity_mission_events.CapacityMissionProjectionError as exc:
        return _wait(task_id, "capacity_projection_failed", **exc.as_dict())
    observations = github_mission_events.append_due_observations(
        project=project, task_id=task_id, repository=journal,
    )
    try:
        ci_remediation = project_ci_remediation(
            task_id,
            project=project,
            task=task_reader(task_id, project=project) or {},
            journal=journal,
            max_attempts=max(
                1, int(os.environ.get("PM_MISSION_BOT_V5_MAX_CI_REMEDIATIONS", "2")),
            ),
        )
    except MissionJournalError as exc:
        return _wait(task_id, exc.code, message=str(exc))
    if ci_remediation.get("reason") == "ci_remediation_exhausted":
        return _wait(
            task_id, "ci_remediation_exhausted",
            retry_count=ci_remediation.get("retry_count"),
            ci_remediation_projection=ci_remediation,
        )

    result = tick_scoped_mission(
        task_id,
        project=project,
        scope_authority=scope_authority,
        actor=actor,
        ports=ports,
    )
    result["capacity_projection"] = capacity
    result["observation_projection"] = observations
    result["ci_remediation_projection"] = ci_remediation
    result["review_handoff_projection"] = handoff
    result["terminal_projection"] = terminal
    if bootstrap is not None:
        result["mission_bootstrap"] = bootstrap
    return result


__all__ = [
    "production_ports",
    "project_ci_remediation",
    "project_terminal_provenance",
    "run_scoped_mission_tick",
]
