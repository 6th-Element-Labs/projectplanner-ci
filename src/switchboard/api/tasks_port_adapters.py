"""Monolith adapters for Tasks ports — live *outside* ``services/tasks``.

Wraps Auth principal helpers and Tasks/claims/activity/work-session
repositories so the Tasks service package stays free of root ``store`` /
``auth`` / ``dispatch`` imports (ARCH-MS-87). Prefer repository modules over
``import store`` so the ARCH-MS-84 store-import ceiling does not grow.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

import auth as _auth

from switchboard.domain.access import identity as _identity
from switchboard.services.tasks import deps as tasks_deps
from switchboard.services.tasks.ports import (
    ClaimLifecyclePort,
    TaskBoardPort,
    TaskPrincipalPort,
    TaskWriteBindingPort,
    WorkSessionLookupPort,
)
from switchboard.storage.repositories import access as _access
from switchboard.storage.repositories import activity as _activity
from switchboard.storage.repositories import claims as _claims
from switchboard.storage.repositories import tasks as _tasks
from switchboard.storage.repositories import work_sessions as _work_sessions


class AuthTaskPrincipal:
    """Adapter: root ``auth.actor`` for Tasks write surfaces."""

    def actor(self, principal: Mapping[str, Any]) -> str:
        return _auth.actor(dict(principal))


class MonolithTaskWriteBinding:
    """Adapter: write-binding + activity via access/activity repositories."""

    def resolve_write_actor(
        self,
        actor: str,
        *,
        project: str = "",
        task_id: str = "",
        agent_id: str = "",
        system_actor: str = "",
        system_reason: str = "",
        principal_id: str = "",
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"actor": actor}
        if project:
            kwargs["project"] = project
        if task_id:
            kwargs["task_id"] = task_id
        if agent_id:
            kwargs["agent_id"] = agent_id
        if system_actor:
            kwargs["system_actor"] = system_actor
        if system_reason:
            kwargs["system_reason"] = system_reason
        if principal_id:
            kwargs["principal_id"] = principal_id
        return _access.resolve_write_actor(**kwargs)

    def write_binding_activity_payload(
        self, binding: Mapping[str, Any]
    ) -> dict[str, Any]:
        return _identity.write_binding_activity_payload(binding)

    def append_activity(
        self,
        kind: str,
        actor: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        task_id: Optional[str] = None,
        project: str = "",
    ) -> int:
        kwargs: dict[str, Any] = {
            "kind": kind,
            "actor": actor,
            "payload": dict(payload or {}),
        }
        if task_id is not None:
            kwargs["task_id"] = task_id
        if project:
            kwargs["project"] = project
        return int(_activity.append_activity(**kwargs))


class MonolithTaskBoard:
    """Adapter: task board persistence via the tasks repository."""

    def list_tasks(
        self,
        *,
        workstream: Optional[str] = None,
        status: Optional[str] = None,
        assignee: Optional[str] = None,
        project: str = "",
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if workstream is not None:
            kwargs["workstream"] = workstream
        if status is not None:
            kwargs["status"] = status
        if assignee is not None:
            kwargs["assignee"] = assignee
        if project:
            kwargs["project"] = project
        return list(_tasks.list_tasks(**kwargs))

    def delete_task(self, task_id: str, *, project: str = "") -> bool:
        if project:
            return bool(_tasks.delete_task(task_id, project=project))
        return bool(_tasks.delete_task(task_id))

    def archive_task(
        self,
        task_id: str,
        *,
        reason: str = "",
        actor: str = "system",
        project: str = "",
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"reason": reason, "actor": actor}
        if project:
            kwargs["project"] = project
        return dict(_tasks.archive_task(task_id, **kwargs))

    def add_comment(
        self,
        task_id: str,
        actor: str,
        text: str,
        *,
        kind: str = "comment",
        project: str = "",
        hydrate_task: bool = True,
    ) -> Optional[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "kind": kind,
            "hydrate_task": hydrate_task,
        }
        if project:
            kwargs["project"] = project
        result = _tasks.add_comment(task_id, actor, text, **kwargs)
        return dict(result) if result is not None else None


class MonolithClaimLifecycle:
    """Adapter: claim abandon/revoke via the claims repository."""

    def abandon_claim(
        self,
        claim_id: str,
        reason: str,
        *,
        actor: str = "system",
        project: str = "",
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"actor": actor}
        if project:
            kwargs["project"] = project
        return dict(_claims.abandon_claim(claim_id, reason, **kwargs))

    def revoke_claim(
        self,
        claim_id: str,
        reason: str,
        *,
        reassign_to: str = "",
        sort_order: Optional[int] = None,
        partial_evidence: Any = None,
        notify: bool = True,
        ack_deadline_minutes: float = 5,
        expected_task_id: str = "",
        actor: str = "switchboard/operator",
        project: str = "",
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "reassign_to": reassign_to,
            "sort_order": sort_order,
            "partial_evidence": partial_evidence,
            "notify": notify,
            "ack_deadline_minutes": ack_deadline_minutes,
            "expected_task_id": expected_task_id,
            "actor": actor,
        }
        if project:
            kwargs["project"] = project
        return dict(_claims.revoke_claim(claim_id, reason, **kwargs))


class MonolithWorkSessionLookup:
    """Adapter: work-session rows via the work_sessions repository."""

    def get_work_session(
        self, work_session_id: str, *, project: str = ""
    ) -> Optional[dict[str, Any]]:
        if project:
            row = _work_sessions.get_work_session(work_session_id, project=project)
        else:
            row = _work_sessions.get_work_session(work_session_id)
        return dict(row) if row is not None else None


def configure_tasks_ports(
    *,
    principal: TaskPrincipalPort | None = None,
    write_binding: TaskWriteBindingPort | None = None,
    board: TaskBoardPort | None = None,
    claims: ClaimLifecyclePort | None = None,
    work_sessions: WorkSessionLookupPort | None = None,
) -> None:
    """Bind Tasks package ports (idempotent defaults for tests and app_impl)."""
    tasks_deps.configure(
        principal=principal or AuthTaskPrincipal(),
        write_binding=write_binding or MonolithTaskWriteBinding(),
        board=board or MonolithTaskBoard(),
        claims=claims or MonolithClaimLifecycle(),
        work_sessions=work_sessions or MonolithWorkSessionLookup(),
    )
