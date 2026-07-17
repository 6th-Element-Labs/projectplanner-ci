"""Ports for the read-only Deliverables/mission day-one process boundary."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from fastapi import Request


@runtime_checkable
class DeliverablesQueryPort(Protocol):
    """Committed, project-scoped read models authorized by ADR-0014."""

    def list_deliverables(
        self, project: str, *, board_id: str = "", summaries: bool = False
    ) -> list[dict[str, Any]]: ...

    def get_deliverable(
        self, project: str, deliverable_id: str
    ) -> dict[str, Any] | None: ...

    def mission_status(
        self,
        project: str,
        *,
        deliverable_id: str = "",
        board_id: str = "",
        mission_id: str = "",
    ) -> dict[str, Any]: ...

    def dependency_graph(
        self, project: str, deliverable_id: str
    ) -> dict[str, Any]: ...

    def closure_report(
        self, project: str, deliverable_id: str, *, report_id: str = ""
    ) -> dict[str, Any]: ...

    def list_breakdown_proposals(
        self,
        project: str,
        *,
        deliverable_id: str = "",
        status: str = "",
    ) -> list[dict[str, Any]]: ...

    def get_breakdown_proposal(
        self, project: str, proposal_id: str
    ) -> dict[str, Any] | None: ...


@runtime_checkable
class DeliverablesReadAuthPort(Protocol):
    """Shared Auth boundary applied before every Deliverables repository read."""

    def authorize(self, request: Request, project: str) -> dict[str, Any]: ...
