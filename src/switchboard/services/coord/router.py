"""FastAPI router for only the ADR-0013 Coord day-one read surface."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Query, Request

from . import deps
from .ports import CoordQueryPort, CoordReadAuthPort


ProjectResolver = Callable[[str], str]
EtagJson = Callable[..., Any]


def create_router(
    *,
    resolve_project: ProjectResolver,
    etag_json: EtagJson,
    queries: CoordQueryPort | None = None,
    auth: CoordReadAuthPort | None = None,
) -> APIRouter:
    """Build the thin router from injected query and Auth ports."""
    query_port = queries or deps.queries()
    auth_port = auth or deps.auth()
    router = APIRouter()

    def project_for(request: Request, raw_project: str) -> str:
        project = resolve_project(raw_project)
        auth_port.authorize(request, project)
        return project

    @router.get("/api/board")
    def board(request: Request, project: str = Query(...), view: str = Query("")):
        resolved = project_for(request, project)
        payload = query_port.board(resolved, cards=(view or "").strip().lower() == "cards")
        return etag_json(request, payload, max_age=5)

    @router.get("/api/signals")
    def plan_signals(request: Request, project: str = Query(...)):
        return query_port.signals(project_for(request, project))

    @router.get("/ixp/v1/delta")
    def delta(request: Request, project: str = Query(...), lane: str = "",
              since_cursor: int = 0):
        return query_port.delta(
            project_for(request, project), since_cursor=since_cursor, lane=lane
        )

    @router.get("/api/coordination")
    def coordination(request: Request, project: str = Query(...), limit: int = 500):
        return query_port.coordination(project_for(request, project), limit=limit)

    @router.get("/api/coordinator_decisions")
    def coordinator_decisions(
        request: Request,
        project: str = Query(...),
        task_id: str = "",
        deliverable_id: str = "",
        decision_kind: str = "",
        limit: int = 100,
    ):
        resolved = project_for(request, project)
        return {
            "project": resolved,
            "schema": "switchboard.coordinator_decision.v1",
            "decisions": query_port.coordinator_decisions(
                resolved,
                task_id=task_id,
                deliverable_id=deliverable_id,
                decision_kind=decision_kind,
                limit=limit,
            ),
        }

    return router
