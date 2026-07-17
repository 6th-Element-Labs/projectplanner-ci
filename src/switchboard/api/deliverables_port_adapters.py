"""Production adapters for Deliverables query/Auth ports.

This composition module stays outside ``services.deliverables``. It binds the
thin service to the package repository while root Auth remains behind one
project-scoped read port.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from fastapi import Request

from switchboard.api import deps as api_deps
from switchboard.services.deliverables import deps as deliverables_deps
from switchboard.services.deliverables.ports import (
    DeliverablesQueryPort,
    DeliverablesReadAuthPort,
)
from switchboard.storage.repositories import deliverables as deliverables_repo
from switchboard.storage.repositories import projects as projects_repo


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


def probe_deliverables_readiness(project: str = "") -> dict[str, Any]:
    """Fail-closed readiness across DB/schema, Auth/session, and one real read."""
    from switchboard.api.routers.auth import session as auth_session

    project = project or os.environ.get(
        "SWITCHBOARD_DELIVERABLES_READY_PROJECT", "switchboard"
    ).strip()
    checks: dict[str, Any] = {}
    db_error = projects_repo.probe_project_db(project)
    checks["database_schema"] = "ok" if db_error is None else db_error
    try:
        auth_session.require_production_secret()
        checks["browser_session_auth"] = (
            "ok" if auth_session.COOKIE_NAME else "cookie_name_missing"
        )
    except Exception as exc:
        checks["browser_session_auth"] = type(exc).__name__
    try:
        RepositoryDeliverablesQueries().list_deliverables(project)
        checks["deliverables_repository_read"] = "ok"
    except Exception as exc:
        checks["deliverables_repository_read"] = type(exc).__name__
    return {"ok": all(value == "ok" for value in checks.values()), "checks": checks}
