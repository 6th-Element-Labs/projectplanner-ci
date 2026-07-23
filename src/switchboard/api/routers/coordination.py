"""Coordination REST routes."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Query
import store


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  sibling_bc_only: bool = False) -> APIRouter:
    router = APIRouter()

    if not sibling_bc_only:
        @router.get("/api/coordination")
        async def api_coordination(project: str = Query(...), limit: int = 500):
            """Read-only rollup for one project's live coordination record."""
            proj = resolve_project(project)
            return {
                "project": proj,
                "agents": store.list_active_agents(project=proj),
                "messages": store.list_agent_messages(project=proj, limit=limit),
                "decisions": store.list_decisions(project=proj, limit=limit),
                "coordinator_decisions": store.list_coordinator_decisions(
                    project=proj, limit=min(limit, 200)),
            }

    if not sibling_bc_only:
        @router.get("/api/coordinator_decisions")
        async def api_coordinator_decisions(
            project: str = Query(...),
            task_id: str = "",
            deliverable_id: str = "",
            decision_kind: str = "",
            limit: int = 100,
        ):
            """COORD-3 structured planner decisions for cockpit/UI."""
            proj = resolve_project(project)
            return {
                "project": proj,
                "schema": "switchboard.coordinator_decision.v1",
                "decisions": store.list_coordinator_decisions(
                    task_id=task_id, deliverable_id=deliverable_id,
                    decision_kind=decision_kind, limit=limit, project=proj,
                ),
            }

    return router
