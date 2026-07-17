"""Production adapters for Deliverables query/Auth ports.

This composition module stays outside ``services.deliverables``. It binds the
thin service to the package repository while root Auth remains behind one
project-scoped read port.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import Request

from switchboard.api import deps as api_deps
from switchboard.services.deliverables import deps as deliverables_deps
from switchboard.services.deliverables.ports import (
    DeliverablesQueryPort,
    DeliverablesReadAuthPort,
)
from switchboard.storage.repositories import deliverables as deliverables_repo


class RepositoryDeliverablesQueries:
    """Read-only projections implemented against the Deliverables repository."""

    def list_deliverables(
        self, project: str, *, board_id: str = "", summaries: bool = False
    ) -> list[dict[str, Any]]:
        if summaries:
            return list(
                deliverables_repo.list_deliverable_summaries(
                    project=project, board_id=board_id
                )
            )
        return list(
            deliverables_repo.list_deliverables(
                project=project, board_id=board_id
            )
        )

    def get_deliverable(
        self, project: str, deliverable_id: str
    ) -> dict[str, Any] | None:
        result = deliverables_repo.get_deliverable(
            deliverable_id, project=project
        )
        return dict(result) if result is not None else None

    def mission_status(
        self,
        project: str,
        *,
        deliverable_id: str = "",
        board_id: str = "",
        mission_id: str = "",
    ) -> dict[str, Any]:
        return dict(
            deliverables_repo.get_mission_status(
                project=project,
                deliverable_id=deliverable_id,
                board_id=board_id,
                mission_id=mission_id,
            )
        )

    def dependency_graph(
        self, project: str, deliverable_id: str
    ) -> dict[str, Any]:
        return dict(
            deliverables_repo.get_deliverable_dependency_graph(
                project=project, deliverable_id=deliverable_id
            )
        )

    def closure_report(
        self, project: str, deliverable_id: str, *, report_id: str = ""
    ) -> dict[str, Any]:
        return dict(
            deliverables_repo.get_deliverable_closure_report(
                deliverable_id, project=project, report_id=report_id
            )
        )

    def list_breakdown_proposals(
        self,
        project: str,
        *,
        deliverable_id: str = "",
        status: str = "",
    ) -> list[dict[str, Any]]:
        return list(
            deliverables_repo.list_deliverable_breakdown_proposals(
                deliverable_id=deliverable_id, project=project, status=status
            )
        )

    def get_breakdown_proposal(
        self, project: str, proposal_id: str
    ) -> dict[str, Any] | None:
        result = deliverables_repo.get_deliverable_breakdown_proposal(
            proposal_id, project=project
        )
        return dict(result) if result is not None else None


class ProjectScopedDeliverablesReadAuth:
    """Adapter over the shared project-scoped principal resolver."""

    def __init__(self, resolver: Callable[..., dict[str, Any]] | None = None):
        self._resolver = resolver or api_deps.resolve_principal

    def authorize(self, request: Request, project: str) -> dict[str, Any]:
        return dict(
            self._resolver(
                request, project, ("read",), dev_actor="deliverables"
            )
        )


def configure_deliverables_ports(
    *,
    queries: DeliverablesQueryPort | None = None,
    auth: DeliverablesReadAuthPort | None = None,
) -> None:
    deliverables_deps.configure(
        queries=queries or RepositoryDeliverablesQueries(),
        auth=auth or ProjectScopedDeliverablesReadAuth(),
    )
