"""Digest and notify REST routes."""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import APIRouter, HTTPException, Query

import digest
import notify
import store
from switchboard.application.adapters import legacy_maxwell_default


ProjectResolver = Callable[[str], str]


def create_router(*, resolve_project: ProjectResolver | None = None) -> APIRouter:
    router = APIRouter()
    _resolve = resolve_project or (lambda p: p)

    @router.post("/api/digest")
    async def make_digest(project: str = Query(...)):
        """Generate the weekly chief-of-staff brief.

        Digests remain Maxwell-backed until SEG-6; callers must still pass an
        explicit project. Only the named Maxwell adapter may run generation.
        """
        selected = _resolve(project)
        if selected != legacy_maxwell_default.maxwell_project_id():
            raise HTTPException(
                400,
                "digest generation is Maxwell-backed pending SEG-6; "
                "pass project=maxwell via the legacy adapter path",
            )
        # Opt in by name — never via DEFAULT_PROJECT omission.
        _ = legacy_maxwell_default.project_context()
        try:
            return await asyncio.to_thread(digest.generate_digest)
        except Exception as e:
            raise HTTPException(502, f"digest error: {e}")

    @router.get("/api/digests")
    async def get_digests(project: str = Query(...)):
        selected = _resolve(project)
        if selected != legacy_maxwell_default.maxwell_project_id():
            raise HTTPException(
                400,
                "digest list is Maxwell-backed pending SEG-6; pass project=maxwell",
            )
        _ = legacy_maxwell_default.project_context()
        return {"digests": store.list_digests(20), "project": selected}

    @router.get("/api/notify/status")
    async def notify_status(project: str = Query(...)):
        """Which channels are wired (configured) vs dry-run."""
        selected = _resolve(project)
        return {**notify.status(), "project": selected}

    @router.post("/api/notify/test")
    async def notify_test(project: str = Query(...)):
        selected = _resolve(project)
        return {
            "results": notify.send(
                f"{selected} — test",
                "Notify is wired (test message from plan.taikunai.com).",
                project=selected,
            ),
            "project": selected,
        }

    @router.post("/api/digest/{digest_id}/send")
    async def send_digest(digest_id: int, project: str = Query(...)):
        selected = _resolve(project)
        if selected != legacy_maxwell_default.maxwell_project_id():
            raise HTTPException(
                400,
                "digest send is Maxwell-backed pending SEG-6; pass project=maxwell",
            )
        _ = legacy_maxwell_default.project_context()
        d = next((x for x in store.list_digests(50) if x["id"] == digest_id), None)
        if not d:
            raise HTTPException(404, "no such digest")
        proj = store.get_meta("project") or selected
        return {
            "results": await asyncio.to_thread(
                notify.send, f"{proj} — digest", d["content"], ("slack", "email"),
                selected, "digest"),
            "project": selected,
        }

    return router
