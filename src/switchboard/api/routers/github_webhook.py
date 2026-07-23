"""GitHub webhook REST routes."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from typing import Callable

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import github_sync
import store
import webhook_inbox


ProjectResolver = Callable[[str], str]

_GH_SECRET = os.environ.get("PM_GITHUB_WEBHOOK_SECRET", "")


def webhook_secret_configured() -> bool:
    return bool(_GH_SECRET)


def _verify_gh_signature(body: bytes, sig_header: str) -> bool:
    """HMAC-SHA256 signature check — skip if no secret configured (dev mode)."""
    if not _GH_SECRET:
        return True
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = hmac.new(_GH_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", sig_header)


async def _drain_webhook_inbox_bg(project: str):
    """Best-effort drain kicked off the request path. Failures are non-fatal: the
    event is already durable in the inbox, and the backstop drain job / reconcile
    will apply it on the next pass."""
    try:
        await asyncio.to_thread(webhook_inbox.drain, project)
    except Exception:
        pass


def create_router(*, resolve_project: ProjectResolver) -> APIRouter:
    router = APIRouter()

    @router.post("/api/github/webhook")
    async def github_webhook(request: Request, project: str = ""):
        """Receive GitHub lifecycle and check events (PERF-1: accept-and-ack, never drop).

        The request path does ONE durable thing — append the raw event to the webhook
        inbox and return 2xx in O(1). No synchronous provenance fan-out, so it cannot
        lock-timeout under a burst and GitHub never sees a 5xx that would drop the
        delivery. A separate drain worker applies provenance idempotently off-path.
        Set PM_GITHUB_WEBHOOK_SECRET in .env and configure the matching secret in GitHub."""
        body = await request.body()
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_gh_signature(body, sig):
            raise HTTPException(401, "invalid webhook signature")

        event = request.headers.get("X-GitHub-Event", "")
        delivery = request.headers.get("X-GitHub-Delivery", "")
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON payload")

        requested_project = project
        project = github_sync.resolve_project(payload, project)
        resolve_project(project)  # fail-closed on unknown project — a misroute is not a drop class

        # Durable commit point: once this returns, the delivery survives process death.
        enq = await asyncio.to_thread(
            webhook_inbox.enqueue_event, project,
            delivery_guid=delivery, event=event,
            payload_bytes=body, headers=dict(request.headers),
            signature_verified=True, requested_project=requested_project,
        )
        # Apply provenance off the request path — never blocks the ack.
        asyncio.create_task(_drain_webhook_inbox_bg(project))

        return JSONResponse({
            "action": "accepted", "event": event, "project": project,
            "delivery": enq.get("delivery_guid"),
            "inbox_id": enq.get("id"),
            "queued": enq.get("enqueued", False),
            "duplicate": enq.get("duplicate", False),
        })

    @router.post("/api/github/webhook/drain")
    async def github_webhook_drain(project: str = Query(...), limit: int = 200):
        """Operator/backstop: apply pending inbox rows now. Idempotent."""
        resolve_project(project)
        return JSONResponse(await asyncio.to_thread(
            webhook_inbox.drain, project, limit=limit))

    @router.get("/api/github/webhook/inbox")
    async def github_webhook_inbox_depth(project: str = Query(...)):
        """Observable inbox depth: counts by status + oldest-pending age."""
        resolve_project(project)
        return JSONResponse(await asyncio.to_thread(webhook_inbox.inbox_depth, project))

    return router
