"""Health / saturation / narration ops REST routes (ARCH-MS-51).

Owns ``/health*``, ``/api/saturation``, and ``/api/narration/*`` while the
composition root supplies project/principal boundaries and readiness inputs.
"""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Optional, Tuple

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import auth
import narration_ops
import store


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
SaturationSnapshot = Callable[[str], dict]
InitFailures = Callable[[], Dict[str, str]]


# Deep readiness must not queue behind the default executor used by normal request
# handlers. One worker is enough because probes are cheap, and the router shares one
# in-flight task so repeated monitor polls cannot fan out when a probe stalls.
_READINESS_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="health-deep")
_DEFAULT_READINESS_TIMEOUT_SECONDS = 2.0
_MAX_READINESS_TIMEOUT_SECONDS = 4.0  # Caddy's /health* response-header timeout is 5s.


def _readiness_timeout_seconds() -> float:
    raw = (os.environ.get("PM_HEALTH_DEEP_TIMEOUT_SECONDS") or "").strip()
    try:
        configured = float(raw) if raw else _DEFAULT_READINESS_TIMEOUT_SECONDS
    except ValueError:
        configured = _DEFAULT_READINESS_TIMEOUT_SECONDS
    return min(_MAX_READINESS_TIMEOUT_SECONDS, max(0.05, configured))


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  saturation_snapshot: SaturationSnapshot,
                  project_init_failures: InitFailures) -> APIRouter:
    """Build health/narration router against shared trust boundaries."""
    router = APIRouter()
    readiness_task: Optional[asyncio.Task[Tuple[int, int]]] = None
    readiness_task_lock = asyncio.Lock()
    last_projects_configured: Optional[int] = None

    def _probe_readiness() -> Tuple[int, int]:
        configured = store.project_ids()
        init_failures = project_init_failures()
        unready = 0
        for pid in configured:
            # A project skipped at startup is unready regardless of a later live probe;
            # otherwise re-check the db + schema live so a db that went away is caught.
            reason = init_failures.get(pid) or store.probe_project_db(pid)
            if reason:
                unready += 1
                # Detail (which project, why) goes to the server log only — never the wire.
                print(f"[readiness] project not ready: {pid}: {reason}", flush=True)
        return len(configured), unready

    async def _run_readiness_probe() -> Tuple[int, int]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_READINESS_EXECUTOR, _probe_readiness)

    async def _shared_readiness_task() -> asyncio.Task[Tuple[int, int]]:
        nonlocal readiness_task
        async with readiness_task_lock:
            if readiness_task is None or readiness_task.done():
                # Consume an exception from a timed-out background task before replacing it.
                if readiness_task is not None and not readiness_task.cancelled():
                    readiness_task.exception()
                readiness_task = asyncio.create_task(_run_readiness_probe())
            return readiness_task

    @router.get("/health")
    async def health():
        """Liveness probe — must stay cheap so monitors/Caddy never block the event loop."""
        return {"status": "ok", "service": "taikun-pm"}


    @router.get("/health/deep")
    async def health_deep():
        """Readiness probe. Publicly routed under Caddy's /health*, so it must NOT expose any
        project data (ids, task counts, names). It verifies that every configured board db is
        accessible with the required schema, and fails CLOSED (503) if any configured project
        could not initialize at startup or is not reachable now. BUG-48."""
        nonlocal last_projects_configured
        timeout_seconds = _readiness_timeout_seconds()
        probe_task = await _shared_readiness_task()
        try:
            projects_configured, projects_unready = await asyncio.wait_for(
                asyncio.shield(probe_task), timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            # Shield leaves the single probe running for a later request to reuse. This is
            # deliberately fail-closed and bounded below Caddy's timeout.
            configured = last_projects_configured or 0
            return JSONResponse({
                "status": "unready",
                "service": "taikun-pm",
                "ready": False,
                "reason": "probe_timeout",
                "timeout_seconds": timeout_seconds,
                "counts_available": last_projects_configured is not None,
                "projects_configured": configured,
                "projects_ready": 0,
                "projects_unready": configured,
            }, status_code=503, headers={"Retry-After": "1"})
        except Exception as exc:
            print(f"[readiness] probe failed: {type(exc).__name__}", flush=True)
            configured = last_projects_configured or 0
            return JSONResponse({
                "status": "unready",
                "service": "taikun-pm",
                "ready": False,
                "reason": "probe_error",
                "counts_available": last_projects_configured is not None,
                "projects_configured": configured,
                "projects_ready": 0,
                "projects_unready": configured,
            }, status_code=503, headers={"Retry-After": "1"})

        last_projects_configured = projects_configured
        ready = projects_unready == 0
        body = {
            "status": "ready" if ready else "unready",
            "service": "taikun-pm",
            "ready": ready,
            "projects_configured": projects_configured,
            "projects_ready": projects_configured - projects_unready,
            "projects_unready": projects_unready,
        }
        return JSONResponse(body, status_code=200 if ready else 503)


    @router.get("/health/saturation")
    def health_saturation(project: str = Query(store.DEFAULT_PROJECT)):
        """Cheap saturation/alerts probe for external monitors (PERF-7)."""
        snap = saturation_snapshot(project)
        return {
            "status": snap.get("status") or "healthy",
            "as_of": snap.get("as_of"),
            "project": snap.get("project"),
            "alert_count": snap.get("alert_count", 0),
            "alerts": snap.get("alerts") or [],
            "slos_ok": (snap.get("slos") or {}).get("ok"),
            "load_shed": (snap.get("load_shed") or {}).get("should_shed"),
            "psi_available": (snap.get("psi") or {}).get("available"),
            "sqlite_lock_waits": (snap.get("mcp_observability") or {}).get("sqlite_lock_waits", 0),
            "sqlite_lock_waits_window": (snap.get("mcp_observability") or {}).get(
                "sqlite_lock_waits_window", 0),
            "webhook_inbox_pending": (snap.get("webhook_inbox_depth") or {}).get("pending", 0),
            "concurrency_inflight": (snap.get("concurrency_limiter") or {}).get("inflight", 0),
            "concurrency_limit": (snap.get("concurrency_limiter") or {}).get("limit", 0),
            "concurrency_saturated": (snap.get("concurrency_limiter") or {}).get("saturated", False),
        }


    @router.get("/api/saturation")
    def api_saturation(project: str = Query(store.DEFAULT_PROJECT)):
        """Full saturation dashboard payload: PSI, lock-wait, inbox depth, SLOs, alerts."""
        return saturation_snapshot(project)


    # ---- NARRATE-13: narration queue health + authorized operator controls ----

    @router.get("/api/narration/health")
    def api_narration_health(request: Request, project: str = Query(store.DEFAULT_PROJECT)):
        """Bounded narration queue + receipt/cost snapshot with alert flags (read-only)."""
        resolve_principal(request, project, ("read",), dev_actor="web")
        return narration_ops.narration_health(resolve_project(project))


    @router.post("/api/narration/narrate-now")
    async def api_narrate_now(request: Request, body: dict = Body(...),
                              project: str = Query(store.DEFAULT_PROJECT)):
        """Force (re)generation of an entity's current narration revision — audited, deduped, and
        still subject to the generation budget (no silent bypass)."""
        principal = resolve_principal(request, project, ("write:system",), dev_actor="web")
        result = narration_ops.narrate_now(
            resolve_project(project), (body or {}).get("entity_type") or "",
            (body or {}).get("entity_id") or "", actor=auth.actor(principal),
            reason=(body or {}).get("reason") or "")
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result


    @router.post("/api/narration/reactivate")
    async def api_narration_reactivate(request: Request, body: dict = Body(...),
                                       project: str = Query(store.DEFAULT_PROJECT)):
        """Authorized retry / dead-letter recovery on a narration request (audited)."""
        principal = resolve_principal(request, project, ("write:system",), dev_actor="web")
        result = narration_ops.reactivate_request(
            resolve_project(project), (body or {}).get("event_id") or "", actor=auth.actor(principal),
            action=(body or {}).get("action") or "retry", reason=(body or {}).get("reason") or "")
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result


    return router
