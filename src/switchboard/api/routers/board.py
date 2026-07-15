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
                  saturation_snapshot: SaturationSnapshot) -> APIRouter:
    """Build the board/roster/signals router against shared trust boundaries."""
    router = APIRouter()

    @router.get("/api/board")
    def board(request: Request, project: str = Query(...),
              view: str = Query("")):
        # Sync (def, not async) on purpose: FastAPI runs it in the threadpool, so the
        # board's SQLite I/O doesn't block the single worker's event loop — other
        # requests (incl. /health) stay responsive while a board builds (HARDEN-36).
        # lite: drop heavy per-task fields the board UI never renders (re-fetched by
        # the task-detail modal); the store also serves a short-TTL cached copy.
        # view=cards: further omit description (largest wire cost) for kanban paint (BUG-A2).
        cards = (view or "").strip().lower() == "cards"
        payload = store.board_payload(resolve_project(project), lite=True, cards=cards)
        # HARDEN-37: ETag + short max-age so a tab refocus / reload that finds the
        # board unchanged gets a bodyless 304 instead of re-downloading ~250KB.
        return etag_json(request, payload, max_age=5)

    @router.get("/api/people")
    async def people(project: str = Query(...)):
        return {"people": store.get_meta(
            "people", store.DEFAULT_PEOPLE, project=resolve_project(project))}

    @router.get("/api/dispatch/status")
    async def dispatch_status(project: str = Query(...)):
        """Is dispatch wired, and is a work-capable agent host online for this project?"""
        return await asyncio.to_thread(dispatch.status, resolve_project(project))

    @router.get("/api/signals")
    def plan_signals(project: str = Query(...)):
        """Derived plan health: overdue / due-soon / blocked / ready / critical-slip /
        past-due decisions + each owner's next-best 1-2 tasks.

        Sync (def, not async) on purpose: compute_plan_signals walks the full enriched
        task list synchronously, so FastAPI runs it in the threadpool where its SQLite
        I/O can't block the single worker's event loop — other requests (incl. /health)
        stay responsive (HARDEN-36). The store also serves a short-TTL cached copy."""
        return signals.compute_plan_signals(project=resolve_project(project))

    @router.get("/ixp/v1/saturation_signals")
    def ixp_saturation_signals(project: str = Query(...)):
        """REST parity for PERF-7 saturation dashboard (PSI + lock-wait + inbox + SLOs)."""
        return saturation_snapshot(project)

    return router
