from __future__ import annotations

from typing import Callable
from fastapi import APIRouter, Body, Header, HTTPException, Query, Request

from . import deps


def create_router(*, resolve_project: Callable[[str], str]) -> APIRouter:
    ingest, auth = deps.ports()
    router = APIRouter()

    @router.get("/api/inbox")
    def get_inbox(request: Request, status: str | None = None, project: str = Query(...)):
        resolved = resolve_project(project)
        auth.authorize(request, resolved, ("read",))
        return ingest.list_inbox(resolved, status)

    @router.post("/api/intake")
    def intake_artifact(
        request: Request,
        body: dict = Body(...),
        project: str = Query(...),
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text required")
        resolved = resolve_project(project)
        auth.authorize(request, resolved, ("write",))
        try:
            return ingest.intake(resolved, body, idempotency_key.strip())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc

    return router
