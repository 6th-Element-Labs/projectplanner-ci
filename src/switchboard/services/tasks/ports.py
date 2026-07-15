"""Tasks independence ports — Protocols only; no monolith imports.

Adapters that wrap root ``store`` / ``auth`` / ``dispatch`` live outside this
package (see ``switchboard.api.tasks_port_adapters``). ARCH-MS-87.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, runtime_checkable


@runtime_checkable
class TaskPrincipalPort(Protocol):
    """Principal → public write actor (Auth coupling via port)."""

    def actor(self, principal: Mapping[str, Any]) -> str:
        """Return the display write actor for ``principal``."""


@runtime_checkable
class TaskWriteBindingPort(Protocol):
    """Write-binding + activity helpers for task/claim mutations."""

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
        """Bind shared env tokens / system actors before mutation."""

    def write_binding_activity_payload(
        self, binding: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Normalize a write-binding dict for activity payloads."""

    def append_activity(
        self,
        kind: str,
        actor: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        task_id: Optional[str] = None,
        project: str = "",
    ) -> int:
        """Append an activity row; return its id."""


@runtime_checkable
class TaskBoardPort(Protocol):
    """Day-one task board persistence still owned by project SQLite."""

    def list_tasks(
        self,
        *,
        workstream: Optional[str] = None,
        status: Optional[str] = None,
        assignee: Optional[str] = None,
        project: str = "",
    ) -> list[dict[str, Any]]:
        """Return filtered task rows for the board."""

    def delete_task(self, task_id: str, *, project: str = "") -> bool:
        """Hard-delete a task and its activity; True when a row was removed."""

    def archive_task(
        self,
        task_id: str,
        *,
        reason: str = "",
        actor: str = "system",
        project: str = "",
    ) -> dict[str, Any]:
        """Archive a task snapshot (or return an error dict)."""

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
        """Append a comment activity; optionally return hydrated task."""


@runtime_checkable
class ClaimLifecyclePort(Protocol):
    """Claim abandon / revoke — claim-only TXP surface for Mode A day one."""

    def abandon_claim(
        self,
        claim_id: str,
        reason: str,
        *,
        actor: str = "system",
        project: str = "",
    ) -> dict[str, Any]:
        """Abandon an active claim and release the task lease."""

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
        """Operator revoke of an active claim."""


@runtime_checkable
class WorkSessionLookupPort(Protocol):
    """Work-session lookup for claim binding / idempotency day one."""

    def get_work_session(
        self, work_session_id: str, *, project: str = ""
    ) -> Optional[dict[str, Any]]:
        """Return a work session row or None."""
