"""Board / roster / plan-health REST routes (ARCH-MS-70).

Owns ``/api/board``, ``/api/people``, ``/api/dispatch/status``,
``/api/signals``, and the IXP REST parity mirror of the saturation dashboard,
while the composition root supplies project resolution and the shared
saturation snapshot / ETag helpers.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import APIRouter, Query, Request

import dispatch
import signals
import store


ProjectResolver = Callable[[str], str]
EtagJson = Callable[..., Any]
SaturationSnapshot = Callable[[str], dict]


def create_router(*, resolve_project: ProjectResolver,
                  etag_json: EtagJson,
                  saturation_snapshot: SaturationSnapshot,
                  sibling_bc_only: bool = False) -> APIRouter:
    """Build the board/roster/signals router against shared trust boundaries."""
    router = APIRouter()

    if not sibling_bc_only:
        @router.get("/api/board")
        def board(request: Request, project: str = Query(...),
                  view: str = Query("")):
            # Sync (def, not async) on purpose: FastAPI runs it in the threadpool.
            cards = (view or "").strip().lower() == "cards"
            payload = store.board_payload(resolve_project(project), lite=True, cards=cards)
            # HARDEN-37: short caching avoids repeating the full board payload.
            return etag_json(request, payload, max_age=5)

    @router.get("/api/people")
    async def people(project: str = Query(...)):
        return {"people": store.get_meta(
            "people", store.DEFAULT_PEOPLE, project=resolve_project(project))}

    @router.get("/api/dispatch/status")
    async def dispatch_status(project: str = Query(...)):
        """Is dispatch wired, and is a work-capable agent host online for this project?"""
        return await asyncio.to_thread(dispatch.status, resolve_project(project))

    if not sibling_bc_only:
        @router.get("/api/signals")
        def plan_signals(project: str = Query(...)):
            """Derived plan health and each owner's next-best tasks.

            Sync keeps its SQLite work in FastAPI's threadpool (HARDEN-36).
            """
            return signals.compute_plan_signals(project=resolve_project(project))

    @router.get("/ixp/v1/saturation_signals")
    def ixp_saturation_signals(project: str = Query(...)):
        """REST parity for PERF-7 saturation dashboard (PSI + lock-wait + inbox + SLOs)."""
        return saturation_snapshot(project)

    @router.get("/ixp/v1/open_prs")
    def ixp_open_prs(project: str = Query(...)):
        """Open PRs on the canonical repo with badge-ready status for the fleet dock.

        Sync on purpose (threadpool): the cached path is instant and the cold path
        does network I/O. Degrades to {"prs": [], "unavailable": ...} — never 500s
        a polling dock.
        """
        import open_prs
        return open_prs.open_prs_payload(resolve_project(project))

    return router
