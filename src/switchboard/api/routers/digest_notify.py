"""Digest and notify REST routes."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

import digest
import notify
import store


def create_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/digest")
    async def make_digest():
        """Generate + post the weekly chief-of-staff brief (signals + activity deltas)."""
        try:
            return await asyncio.to_thread(digest.generate_digest)
        except Exception as e:
            raise HTTPException(502, f"digest error: {e}")

    @router.get("/api/digests")
    async def get_digests():
        return {"digests": store.list_digests(20)}

    @router.get("/api/notify/status")
    async def notify_status():
        """Which channels are wired (configured) vs dry-run."""
        return notify.status()

    @router.post("/api/notify/test")
    async def notify_test():
        return {"results": notify.send("Project Maxwell — test", "Notify is wired (test message from plan.taikunai.com).")}

    @router.post("/api/digest/{digest_id}/send")
    async def send_digest(digest_id: int):
        d = next((x for x in store.list_digests(50) if x["id"] == digest_id), None)
        if not d:
            raise HTTPException(404, "no such digest")
        proj = store.get_meta("project") or "the plan"
        # UI-14: honor this project's configured digest recipients (matches jobs.weekly_digest);
        # falls back to the global list when unset.
        return {"results": await asyncio.to_thread(
            notify.send, f"{proj} — digest", d["content"], ("slack", "email"),
            store.DEFAULT_PROJECT, "digest")}

    return router
