"""HTTP middleware stack (ARCH-MS-70): request observability, concurrency
shedding, optional load-shed, and the global auth boundary.

``register_middleware`` wires the four ``@app.middleware("http")`` handlers
the monolith always ran, in the same registration order, so Starlette's
outside-in dispatch stays identical. The composition root supplies the
per-process ``RequestObservability`` instance and the saturation/global-scope
callables that need to stay backed by shared state; ``store``/``auth`` and the
global-auth submodules are imported directly since they are the same stable,
dependency-light shared modules every router already imports.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse

import auth
import concurrency_limiter
import store
from switchboard.api.routers.auth import service as _auth_service
from switchboard.api.routers.auth import session as _auth_session


SaturationSnapshot = Callable[[str], dict]
GlobalUserScopes = Callable[[dict, str], list]
GlobalPrincipal = Callable[[dict, list], dict]


def _protected_read_path(path: str) -> bool:
    return path.startswith(("/api/", "/ixp/", "/txp/", "/tally/"))


def _auth_exempt_path(path: str) -> bool:
    return (
        path == "/health" or
        path == "/health/saturation" or
        path == "/api/github/webhook" or
        path == "/api/cleanup/apply" or
        path.startswith("/api/auth/")
    )


def _write_required_scopes(path: str) -> tuple:
    # ACCESS-14: creating a project needs write:projects (contributors and up), not write:system.
    if path == "/api/projects":
        return ("write:projects",)
    if "/provider-credential-leases/" in path or path.endswith("/leases"):
        return ("use:credentials",)
    if "/provider-connections" in path:
        return ("write:credentials",)
    # ACCESS-21: ordinary metadata is project-editor work. Lifecycle and repo/trust
    # boundary mutations remain system-only even though they share the projects prefix.
    if re.fullmatch(r"/api/projects/[^/]+", path):
        return ("write:projects",)
    if ((path.startswith("/api/projects/") and
         (path.endswith(("/archive", "/restore", "/repo_topology", "/github_repo",
                         "/cleanup-review")) or
          "/consolidation/" in path or "/purge/" in path)) or
            path.startswith(("/api/access/", "/api/audit/", "/api/cleanup/"))):
        return ("write:system",)
    return ("write:tasks",)


def _request_project(request: Request, path: str) -> str:
    """Resolve project for the auth gate — fail closed on omission (SEG-4).

    Returns ``""`` when no explicit project is present so callers can reject
    instead of inventing Maxwell via ``DEFAULT_PROJECT``.
    """
    if path == "/api/projects":
        return "switchboard"
    if path.startswith("/api/projects/"):
        parts = path.split("/")
        if len(parts) > 3 and parts[3]:
            return parts[3]
    return (request.query_params.get("project") or "").strip()


def _slow_request_log_ms() -> float:
    """Threshold (ms) above which a request is logged with its exact path. Default 500ms
    keeps the log to the genuinely-slow tail; PM_SLOW_REQUEST_LOG_MS=0 disables it."""
    try:
        return float(os.environ.get("PM_SLOW_REQUEST_LOG_MS", "500") or 0)
    except (TypeError, ValueError):
        return 500.0


def register_middleware(app, *, req_obs, saturation_snapshot: SaturationSnapshot,
                        global_user_scopes: GlobalUserScopes,
                        global_principal: GlobalPrincipal,
                        admin_scopes: list) -> None:
    """Register the four global middleware handlers against ``app``, preserving
    the monolith's exact registration order and behavior."""

    def _attach_server_timing(response, started_at: float):
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        metric = f"app;dur={elapsed_ms:.1f}"
        existing = response.headers.get("Server-Timing")
        response.headers["Server-Timing"] = f"{existing}, {metric}" if existing else metric
        response.headers["X-Switchboard-Server-Ms"] = f"{elapsed_ms:.1f}"
        # HTML documents (app shell + login/signup/reset pages) must always revalidate
        # so a deploy's new ?v= asset references reach browsers without a manual
        # hard-refresh — the exact trap that locked users out after the auth cutover.
        # Versioned assets (…?v=<hash>) stay immutable + long-cached (_VersionedStaticFiles).
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    async def _global_auth_gate(request: Request, call_next, started_at: float, path: str, method: str):
        """Global auth gate. Bearer tokens use per-project auth; browser users use JWT."""
        is_read = method in {"GET", "HEAD"}
        is_write = method in {"POST", "PATCH", "DELETE"}
        protocol = path.startswith(("/ixp/", "/txp/", "/tally/"))
        gated = (is_read and _protected_read_path(path) and not protocol) or (is_write and not protocol)
        if not gated:
            return _attach_server_timing(await call_next(request), started_at)

        if auth.auth_mode() == auth.DEV_OPEN and not auth.bearer_from_request(request):
            return _attach_server_timing(await call_next(request), started_at)

        # Agents / API tokens
        if auth.bearer_from_request(request):
            project = _request_project(request, path)
            if not project:
                return _attach_server_timing(
                    JSONResponse({"detail": "project required"}, status_code=400), started_at)
            if not store.has_project(project):
                return _attach_server_timing(
                    JSONResponse({"detail": f"unknown project: {project}"}, status_code=400), started_at)
            required = ("read",) if is_read else _write_required_scopes(path)
            try:
                request.state.principal = auth.authenticate_request(request, project, required, dev_actor="agent")
            except PermissionError as e:
                status = 403 if "forbidden" in str(e) else 401
                return _attach_server_timing(JSONResponse({"detail": str(e)}, status_code=status), started_at)
            return _attach_server_timing(await call_next(request), started_at)

        # Browser users — global taikun_session JWT.
        user = _auth_service.current_user(request.cookies.get(_auth_session.COOKIE_NAME, ""))
        if not user:
            return _attach_server_timing(JSONResponse({"detail": "not authenticated"}, status_code=401), started_at)
        # "list my projects" — any authenticated user; the route filters to their grants.
        if path == "/api/projects" and is_read:
            request.state.principal = global_principal(user, list(admin_scopes))
            return _attach_server_timing(await call_next(request), started_at)
        project = _request_project(request, path)
        if not project:
            return _attach_server_timing(
                JSONResponse({"detail": "project required"}, status_code=400), started_at)
        if not store.has_project(project):
            return _attach_server_timing(
                JSONResponse({"detail": f"unknown project: {project}"}, status_code=400), started_at)
        scopes = global_user_scopes(user, project)
        required = ("read",) if is_read else _write_required_scopes(path)
        if "admin" not in scopes and not set(required).issubset(set(scopes)):
            return _attach_server_timing(
                JSONResponse({"detail": "forbidden: no access to this project"}, status_code=403), started_at)
        request.state.principal = global_principal(user, scopes)
        return _attach_server_timing(await call_next(request), started_at)

    @app.middleware("http")
    async def _request_observability(request: Request, call_next):
        """Record per-route latency for PERF-7 SLO gates (web p99, webhook ingest p99)."""
        started_at = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        path = request.url.path
        req_obs.record_path(
            path,
            elapsed_ms,
            response.status_code,
            dropped_webhook=(path == "/api/github/webhook" and response.status_code >= 500),
        )
        # Per-CLASS SLO buckets (web/webhook/health) can't tell which exact path is the p99 tail.
        # Name the slow ones so the tail is diagnosable from logs. PM_SLOW_REQUEST_LOG_MS=0 disables.
        _slow = _slow_request_log_ms()
        if _slow and elapsed_ms >= _slow:
            print(
                f"[slow-request] {elapsed_ms:.0f}ms {request.method} {path}"
                f"?{request.url.query} status={response.status_code}",
                flush=True,
            )
        return response

    @app.middleware("http")
    async def _global_concurrency_limit(request: Request, call_next):
        """PERF-5: reject expensive work immediately when global slots are full (429 + Retry-After)."""
        path = request.url.path
        if not concurrency_limiter.is_expensive_request(request.method, path):
            return await call_next(request)
        acquired, snap = concurrency_limiter.try_acquire()
        if not acquired:
            return JSONResponse(
                concurrency_limiter.build_shed_payload(snap),
                status_code=429,
                headers=concurrency_limiter.build_shed_headers(snap),
            )
        try:
            return await call_next(request)
        finally:
            concurrency_limiter.release()

    @app.middleware("http")
    async def _optional_load_shed(request: Request, call_next):
        """When PM_LOAD_SHED_ENABLED=1, shed expensive writes before pressure becomes failure."""
        enabled = (os.environ.get("PM_LOAD_SHED_ENABLED") or "").strip().lower() in (
            "1", "true", "on", "yes")
        if not enabled:
            return await call_next(request)
        path = request.url.path
        if path in ("/api/github/webhook", "/health", "/health/saturation", "/health/deep"):
            return await call_next(request)
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return await call_next(request)
        project = _request_project(request, path)
        if not project or not store.has_project(project):
            return await call_next(request)
        snap = await asyncio.to_thread(saturation_snapshot, project)
        shed = snap.get("load_shed") or {}
        if not shed.get("should_shed"):
            return await call_next(request)
        retry = int(shed.get("retry_after_s") or 5)
        return JSONResponse(
            {
                "error": "load_shed",
                "schema": "switchboard.load_shed.v1",
                "reasons": shed.get("reasons") or [],
                "retry_after_s": retry,
                "saturation_status": snap.get("status"),
            },
            status_code=503,
            headers={"Retry-After": str(retry)},
        )

    @app.middleware("http")
    async def _auth_boundary(request: Request, call_next):
        """Gate Switchboard data reads and state-changing writes when auth is required.

        Protocol endpoints authenticate inside their handlers because their project lives in the
        JSON body. GitHub webhooks keep their HMAC check. Static assets stay public so the login
        page can render; project data and control APIs do not.
        """
        started_at = time.perf_counter()
        path = request.url.path
        method = request.method.upper()
        if _auth_exempt_path(path):
            return _attach_server_timing(await call_next(request), started_at)

        return await _global_auth_gate(request, call_next, started_at, path, method)
