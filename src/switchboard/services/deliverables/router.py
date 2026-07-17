"""FastAPI router for only the ADR-0014 Deliverables day-one read surface."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request

from . import deps
from .ports import DeliverablesQueryPort, DeliverablesReadAuthPort


ProjectResolver = Callable[[str], str]
EtagJson = Callable[..., Any]


def _read_error(result: dict[str, Any], *, closure: bool = False) -> None:
    error = str(result.get("error") or "")
    if not error:
        return
    missing = (
        "unknown" in error
        or "no deliverable" in error
        or (closure and "not found" in error)
    )
    raise HTTPException(404 if missing else 400, error)


def create_router(
    *,
    resolve_project: ProjectResolver,
    etag_json: EtagJson,
    queries: DeliverablesQueryPort | None = None,
    auth: DeliverablesReadAuthPort | None = None,
) -> APIRouter:
    """Build the thin read router from injected repository and Auth ports."""
    query_port = queries or deps.queries()
    auth_port = auth or deps.auth()
    router = APIRouter()

    def project_for(request: Request, raw_project: str) -> str:
        project = resolve_project(raw_project)
        auth_port.authorize(request, project)
        return project

    @router.get("/api/deliverables")
    def list_deliverables(
        request: Request,
        project: str = Query(...),
        board_id: str = "",
        view: str = "",
    ):
        resolved = project_for(request, project)
        if view == "picker":
            return {
                "project": resolved,
                "board_id": board_id or None,
                "view": "picker",
                "deliverables": query_port.list_deliverables(
                    resolved, board_id=board_id, summaries=True
                ),
            }
        if view:
            raise HTTPException(400, "unknown deliverable list view")
        return {
            "project": resolved,
            "board_id": board_id or None,
            "deliverables": query_port.list_deliverables(
                resolved, board_id=board_id
            ),
        }

    # Literal routes precede /{deliverable_id}; Starlette matches first registered.
    @router.get("/api/deliverables/breakdown_proposals")
    def list_breakdown_proposals(
        request: Request,
        project: str = Query(...),
        deliverable_id: str = "",
        status: str = "",
    ):
        resolved = project_for(request, project)
        return {
            "project": resolved,
            "deliverable_id": deliverable_id or None,
            "proposals": query_port.list_breakdown_proposals(
                resolved, deliverable_id=deliverable_id, status=status
            ),
        }

    @router.get("/api/deliverables/breakdown_proposals/{proposal_id}")
    def get_breakdown_proposal(
        request: Request, proposal_id: str, project: str = Query(...)
    ):
        resolved = project_for(request, project)
        result = query_port.get_breakdown_proposal(resolved, proposal_id)
        if not result:
            raise HTTPException(404, "proposal not found")
        _read_error(result)
        return result

    @router.get("/api/deliverables/{deliverable_id}")
    def get_deliverable(
        request: Request, deliverable_id: str, project: str = Query(...)
    ):
        resolved = project_for(request, project)
        result = query_port.get_deliverable(resolved, deliverable_id)
        if not result:
            raise HTTPException(404, "deliverable not found")
        return result

    @router.get("/api/mission_status")
    def mission_status(
        request: Request,
        project: str = Query(...),
        deliverable_id: str = "",
        board_id: str = "",
        mission_id: str = "",
    ):
        resolved = project_for(request, project)
        result = query_port.mission_status(
            resolved,
            deliverable_id=deliverable_id,
            board_id=board_id,
            mission_id=mission_id,
        )
        _read_error(result)
        return etag_json(request, result, max_age=5)

    @router.get("/api/deliverables/{deliverable_id}/mission_status")
    def deliverable_mission_status(
        request: Request, deliverable_id: str, project: str = Query(...)
    ):
        resolved = project_for(request, project)
        result = query_port.mission_status(
            resolved, deliverable_id=deliverable_id
        )
        _read_error(result)
        return etag_json(request, result, max_age=5)

    @router.get("/api/deliverables/{deliverable_id}/dependency_graph")
    def dependency_graph(
        request: Request, deliverable_id: str, project: str = Query(...)
    ):
        resolved = project_for(request, project)
        result = query_port.dependency_graph(resolved, deliverable_id)
        _read_error(result)
        return etag_json(request, result, max_age=5)

    @router.get("/api/deliverables/{deliverable_id}/closure_report")
    def closure_report(
        request: Request,
        deliverable_id: str,
        project: str = Query(...),
        report_id: str = "",
    ):
        resolved = project_for(request, project)
        result = query_port.closure_report(
            resolved, deliverable_id, report_id=report_id
        )
        _read_error(result, closure=True)
        return result

    return router
