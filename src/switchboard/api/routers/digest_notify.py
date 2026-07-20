"""Digest and notify REST routes."""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import APIRouter, HTTPException, Query

import notify
from switchboard.application.commands import project_digest
from switchboard.application.queries.project_scope import require_explicit_project


ProjectResolver = Callable[[str], str]


def create_router(*, resolve_project: ProjectResolver | None = None) -> APIRouter:
    router = APIRouter()
    _resolve = resolve_project or (lambda p: p)

    @router.post("/api/digest")
    async def make_digest(project: str = Query(...)):
        """Generate the weekly chief-of-staff brief for one explicit project."""
        selected = _resolve(project)
        ctx = require_explicit_project(selected, source="query")
        try:
            return await asyncio.to_thread(project_digest.generate, ctx)
        except Exception as e:
            raise HTTPException(502, f"digest error: {e}")

    @router.get("/api/digests")
    async def get_digests(project: str = Query(...)):
        selected = _resolve(project)
        ctx = require_explicit_project(selected, source="query")
        return {"digests": project_digest.list_recent(ctx, limit=20), "project": selected}

    @router.get("/api/notify/status")
    async def notify_status(project: str = Query(...)):
        """Which channels are wired (configured) vs dry-run."""
        selected = _resolve(project)
        return {**notify.status(), "project": selected}

    @router.post("/api/notify/test")
    async def notify_test(project: str = Query(...)):
        selected = _resolve(project)
        ctx = require_explicit_project(selected, source="query")
        return {
            "results": project_digest.send_test(ctx),
            "project": selected,
        }

    @router.post("/api/digest/{digest_id}/send")
    async def send_digest(digest_id: int, project: str = Query(...)):
        selected = _resolve(project)
        ctx = require_explicit_project(selected, source="query")
        results = await asyncio.to_thread(project_digest.send_one, ctx, digest_id=digest_id)
        if results is None:
            raise HTTPException(404, "no such digest")
        return {
            "results": results,
            "project": selected,
        }

    return router
