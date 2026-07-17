"""Coordination REST routes."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, Query, Request

import auth
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

    @router.get("/api/coordinator_dispatch/plan")
    async def api_coordinator_dispatch_plan(
        request: Request,
        project: str = Query(...),
        max_dispatches: int = 3,
        max_nudges: int = 3,
    ):
        """COORD-4 dry plan preview — what T1 would wake/nudge without acting."""
        import coordinator_dispatch as coord_dispatch
        proj = resolve_project(project)
        resolve_principal(request, proj, ("read",), dev_actor="web")
        db_path = str(store._resolve(proj)["db"])
        snapshot = __import__("coordinator_audit").collect_snapshot(db_path, proj)
        plan = coord_dispatch.build_dispatch_plan(
            snapshot,
            policy={"dry_run": True, "max_dispatches_per_tick": max_dispatches,
                    "max_nudges_per_tick": max_nudges},
        )
        return {"project": proj, "plan": plan}

    @router.post("/api/coordinator_dispatch")
    async def api_coordinator_dispatch(request: Request, body: dict = Body(default={})):
        """COORD-4 T1 dispatch tick. Defaults to dry_run=true; set dry_run=false to act."""
        import coordinator_dispatch as coord_dispatch
        proj = resolve_project(body.get("project") or request.query_params.get("project"))
        principal = resolve_principal(request, proj, ("write:ixp",), dev_actor="web")
        policy = dict(body.get("policy") or {})
        if "dry_run" in body:
            policy["dry_run"] = bool(body.get("dry_run"))
        else:
            policy.setdefault("dry_run", True)
        tick = coord_dispatch.run_dispatch_tick(
            proj, policy=policy, actor=auth.actor(principal),
        )
        return tick

    return router
