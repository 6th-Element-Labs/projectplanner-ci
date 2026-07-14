#!/usr/bin/env python3
"""taikun-pm — opt-in project-board satellite microservice (see ADR 0007).

Standalone FastAPI app (port 8110). Owns: the board UI (static/), task state
(SQLite via store.py), and live exports (export.py). Borrows only the shared
LLM gateway (later, for the per-task agent). Does NOT import actionengine core
and does NOT touch the shared Postgres.

Run:
    uvicorn app:app --host 0.0.0.0 --port 8110            # from services/taikun-pm/
    python -m uvicorn app:app --port 8110
"""
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

# Load a local .env if present (SMTP/gateway config for later slices). No core import.
_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from fastapi import Body, FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import auth  # noqa: E402
import concurrency_limiter  # noqa: E402
import dispatch  # noqa: E402
import narration_ops  # noqa: E402
import request_observability  # noqa: E402
import saturation_signals  # noqa: E402
import signals  # noqa: E402
import store  # noqa: E402
app = FastAPI(title="Taikun PM", version="0.1.0")
_req_obs = request_observability.RequestObservability()

# Global auth router — always mounted. Browser users authenticate via
# taikun_session JWT; agents/API callers keep bearer-token principals.
import scripts.switchboard_path  # noqa: E402,F401
from switchboard.api.routers.auth import service as _auth_service, session as _auth_session, store as _auth_store  # noqa: E402
from switchboard.api.routers.auth.routes import router as _global_auth_router  # noqa: E402
from switchboard.api.routers.plan_chat import create_router as _create_plan_chat_router  # noqa: E402
from switchboard.api.routers.projects import create_router as _create_project_router  # noqa: E402
from switchboard.api.routers.provider_credentials import create_router as _create_provider_credentials_router  # noqa: E402
from switchboard.api.routers.tasks import create_router as _create_task_router  # noqa: E402
from switchboard.api.routers.claims import create_router as _create_claims_router  # noqa: E402
from switchboard.api.routers.wakes import create_router as _create_wakes_router  # noqa: E402
from switchboard.api.routers.agents import create_router as _create_agents_router  # noqa: E402
from switchboard.api.routers.messaging import create_router as _create_messaging_router  # noqa: E402
from switchboard.api.routers.access import create_router as _create_access_router  # noqa: E402
from switchboard.api.routers.health import create_router as _create_health_router  # noqa: E402
from switchboard.api.routers.tally import create_router as _create_tally_router  # noqa: E402
from switchboard.api.routers.ixp_work_sessions import create_router as _create_ixp_work_sessions_router  # noqa: E402
from switchboard.api.routers.runner import create_router as _create_runner_router  # noqa: E402
from switchboard.api.routers.external_effects import create_router as _create_external_effects_router  # noqa: E402
from switchboard.api.routers.intake_inbox import create_router as _create_intake_inbox_router  # noqa: E402
from switchboard.api.routers.digest_notify import create_router as _create_digest_notify_router  # noqa: E402
from switchboard.api.routers.ops_export import create_router as _create_ops_export_router  # noqa: E402
from switchboard.api.routers.github_webhook import (  # noqa: E402
    create_router as _create_github_webhook_router,
    webhook_secret_configured,
)
from switchboard.api.routers.coordination import create_router as _create_coordination_router  # noqa: E402
from switchboard.api.routers.deliverables import create_router as _create_deliverables_router  # noqa: E402
from switchboard.application.commands import request_wake as request_wake_command  # noqa: E402
from switchboard.domain.projects import ProjectLifecycleWriteBlocked  # noqa: E402

_auth_store.init()
app.include_router(_global_auth_router)


@app.exception_handler(ProjectLifecycleWriteBlocked)
async def _project_lifecycle_write_blocked(_request: Request,
                                           exc: ProjectLifecycleWriteBlocked):
    return JSONResponse(status_code=423, content={"detail": exc.detail})

store.init_project_registry()
store.init_db()
_seeded = store.seed_if_empty()
# Additional projects — each in its OWN db file; one-shot seed, guarded so a restart never
# wipes or re-imports. Maxwell (DEFAULT_PROJECT) is seeded above, untouched.
# A project that fails to initialize does NOT block startup (the box must keep serving the
# projects that are healthy), but it is recorded so /health/deep can fail readiness closed —
# a silently-skipped project must never let the service report itself ready. BUG-48.
_PROJECT_INIT_FAILURES: dict[str, str] = {}
for _pid in store.project_ids():
    if _pid != store.DEFAULT_PROJECT:
        try:
            store.init_db(_pid)
            store.seed_if_empty(_pid)
        except Exception as _e:  # never let a second project block startup
            _PROJECT_INIT_FAILURES[_pid] = f"{type(_e).__name__}: {_e}"
            print(f"[projects] seed {_pid} skipped: {_e}")

# NARRATE-14: register the event-driven narration wake accelerator. Inert until an operator sets
# PM_NARRATION_EVENT_PRIMARY; the durable outbox + narrate_events recovery sweep are the backstop.
try:
    import narration_cutover  # noqa: E402
    narration_cutover.register_production_wake_sink()
except Exception as _e:  # never let narration wiring block startup
    print(f"[narration] wake sink registration skipped: {_e}")


ADMIN_SCOPES = [
    "read", "read:credentials", "write:tasks", "write:ixp", "write:system",
    "write:bug_intake", "write:credentials", "use:credentials", "admin",
]


def _bootstrap_admin_from_env():
    password = (os.environ.get("PM_BOOTSTRAP_ADMIN_PASSWORD") or
                os.environ.get("PM_ADMIN_PASSWORD") or "").strip()
    if not password:
        return
    project = (os.environ.get("PM_BOOTSTRAP_PROJECT") or "switchboard").strip()
    if not store.has_project(project):
        return
    login = (os.environ.get("PM_BOOTSTRAP_ADMIN_LOGIN") or
             os.environ.get("PM_ADMIN_LOGIN") or "admin").strip().lower()
    display_name = (os.environ.get("PM_BOOTSTRAP_ADMIN_NAME") or login).strip()
    email = (os.environ.get("PM_BOOTSTRAP_ADMIN_EMAIL") or f"{login}@taikunai.com").strip().lower()
    principal_id = "user-" + hashlib.sha256(f"{project}:{login}".encode("utf-8")).hexdigest()[:16]
    if _auth_store.get_user_by_email(email):
        return
    account = _auth_store.create_user(
        email, display_name, auth.password_hash(password),
        is_superadmin=True, user_id=principal_id)
    store.ensure_bootstrap_project_owner(
        project, account["id"], login, display_name, actor="switchboard/auth")
    store.append_activity(
        "auth.admin_bootstrapped", "switchboard/auth",
        {"project": project, "email": email, "principal_id": account["id"], "source": "env"},
        task_id=None, project=project)


_bootstrap_admin_from_env()


def _proj(project: str) -> str:
    """Validate a project id against the registry — fail closed (400) on anything unknown
    so a bad/stale id can never be silently routed to (or written into) the wrong db."""
    if not store.has_project(project):
        raise HTTPException(400, f"unknown project: {project}")
    return project


def _principal(request: Request, project: str, scopes=("write:ixp",), dev_actor: str = "web"):
    pre = getattr(request.state, "principal", None)
    if isinstance(pre, dict):
        if auth._has_scopes(pre, scopes, _proj(project)):
            return pre
        raise HTTPException(403, "forbidden: token is missing required scope")
    try:
        return auth.authenticate_request(request, _proj(project), scopes, dev_actor=dev_actor)
    except PermissionError as e:
        status = 403 if "forbidden" in str(e) else 401
        raise HTTPException(status, str(e))


def _body_project(body: dict) -> str:
    return _proj((body or {}).get("project") or store.DEFAULT_PROJECT)


def _control_plane_http(result):
    if isinstance(result, dict) and result.get("error") == "control_plane_unavailable":
        raise HTTPException(503, result)
    if (isinstance(result, list) and result and isinstance(result[0], dict) and
            result[0].get("error") == "control_plane_unavailable"):
        raise HTTPException(503, result[0])
    return result


def _etag_json(request: Request, payload, *, max_age: int) -> Response:
    """Serialize payload to JSON with a weak ETag + short max-age, returning a bodyless
    304 when the client's If-None-Match already matches. The one reused shape behind the
    hot poll endpoints (/api/board + project_context, HARDEN-36/37; and the mission
    pollers, CONSOL-8): a tab refocus/reload — or a 5s live tick that revalidates — skips
    re-downloading an unchanged payload. Pairs with the store's short-TTL read cache: the
    TTL saves the server rebuild, the ETag saves the wire."""
    body = json.dumps(payload, default=str, separators=(",", ":")).encode()
    etag = 'W/"%s"' % hashlib.md5(body).hexdigest()  # noqa: S324 (cache tag, not security)
    headers = {"ETag": etag, "Cache-Control": "private, max-age=%d" % max_age}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


app.include_router(_create_task_router(
    resolve_project=_proj,
    resolve_principal=_principal,
))
app.include_router(_create_claims_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    resolve_body_project=_body_project,
))
app.include_router(_create_wakes_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    resolve_body_project=_body_project,
    control_plane_http=_control_plane_http,
))
app.include_router(_create_agents_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    resolve_body_project=_body_project,
    control_plane_http=_control_plane_http,
))
app.include_router(_create_messaging_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    resolve_body_project=_body_project,
))
app.include_router(_create_project_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    current_user=lambda token: _auth_service.current_user(token),
    cookie_name=_auth_session.COOKIE_NAME,
    accessible_project_ids=_auth_store.accessible_project_ids,
    etag_json=_etag_json,
    webhook_secret_configured=webhook_secret_configured,
))
app.include_router(_create_provider_credentials_router(
    resolve_project=_proj,
    resolve_principal=_principal,
))
app.include_router(_create_plan_chat_router(resolve_project=_proj))
app.include_router(_create_access_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    lookup_auth_user=_auth_store.get_user,
    lookup_auth_user_by_email=_auth_store.get_user_by_email,
))
app.include_router(_create_tally_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    resolve_body_project=_body_project,
))
app.include_router(_create_health_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    saturation_snapshot=lambda project: _saturation_snapshot(project),
    project_init_failures=lambda: _PROJECT_INIT_FAILURES,
))
app.include_router(_create_ixp_work_sessions_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    resolve_body_project=_body_project,
))
app.include_router(_create_runner_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    resolve_body_project=_body_project,
))
app.include_router(_create_external_effects_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    resolve_body_project=_body_project,
))
app.include_router(_create_intake_inbox_router(resolve_project=_proj))
app.include_router(_create_digest_notify_router())
app.include_router(_create_ops_export_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    resolve_body_project=_body_project,
))
app.include_router(_create_github_webhook_router(resolve_project=_proj))
app.include_router(_create_coordination_router(
    resolve_project=_proj,
    resolve_principal=_principal,
))
app.include_router(_create_deliverables_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    etag_json=_etag_json,
))


def _attach_server_timing(response: Response, started_at: float) -> Response:
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


# --- Content-hashed static asset versions ------------------------------------
# The app shell (index.html) and the auth pages load app.js / taikun-*.css with a
# ?v=<n> query whose sole job is cache-busting. That number was bumped by hand and
# kept getting forgotten — three PRs changed app.js without a bump, so returning
# browsers ran week-stale JS and the deliverable map "never loaded" until #199 bumped
# it reactively. We derive ?v= from each asset's content hash at serve time instead:
# edit the file and its URL changes on the next request, with no human step. The HTML
# shell is served no-cache (see _attach_server_timing) so fresh hashes always reach the
# browser; the hashed assets are served immutable + long-cached (_VersionedStaticFiles).
_LOCAL_ASSET_RE = re.compile(
    rb'((?:src|href)=")(?!https?://|//|/)([^"?]+\.(?:js|css))(?:\?[^"]*)?(")'
)
_ASSET_VERSION_CACHE: dict = {}


def _asset_version(path: Path) -> str:
    """Short content hash of a static asset, memoized on (mtime_ns, size) so a
    changed file re-hashes on the next request without a restart; '0' if missing."""
    try:
        st = path.stat()
    except OSError:
        return "0"
    sig = (st.st_mtime_ns, st.st_size)
    cached = _ASSET_VERSION_CACHE.get(path)
    if cached and cached[0] == sig:
        return cached[1]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    _ASSET_VERSION_CACHE[path] = (sig, digest)
    return digest


def _shell_response(path: Path) -> Response:
    """Serve an HTML shell with every local .js/.css reference's ?v= rewritten to
    that asset's content hash. CDN/absolute ("/…") URLs are left untouched."""
    def _sub(m: "re.Match") -> bytes:
        asset = m.group(2)
        version = _asset_version(_static / asset.decode()).encode()
        return m.group(1) + asset + b"?v=" + version + m.group(3)
    return HTMLResponse(_LOCAL_ASSET_RE.sub(_sub, path.read_bytes()))


def _request_project(request: Request, path: str) -> str:
    if path == "/api/projects":
        return "switchboard"
    if path.startswith("/api/projects/"):
        parts = path.split("/")
        if len(parts) > 3 and parts[3]:
            return parts[3]
    return request.query_params.get("project") or store.DEFAULT_PROJECT



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


def _global_user_scopes(user: dict, project: str) -> list:
    """A global user's effective scopes on a project — superadmin gets admin."""
    if user.get("is_superadmin"):
        return list(ADMIN_SCOPES)
    scopes: set = set()
    for grant in store.principal_project_roles(project, user["id"]):
        scopes.update(grant.get("scopes") or [])
    # ACCESS-15: if the project is in the user's accessible set (owner, invitee, or org
    # membership — including the private→org-admin/owner rule from ACCESS-14), they can at
    # least READ it. Aligns the read gate with the project list so "visible" means "openable";
    # writes still require an explicit role grant.
    accessible = {p.get("id") for p in (user.get("projects") or [])}
    accessible.update(_auth_store.accessible_project_ids(
        user["id"], bool(user.get("is_superadmin"))))
    if project in accessible:
        scopes.add("read")
    return sorted(scopes)


def _global_principal(user: dict, scopes: list) -> dict:
    return {
        "id": user["id"], "kind": "user",
        "display_name": user.get("display_name") or user.get("email") or user["id"],
        "email": user.get("email"), "scopes": scopes, "effective_scopes": scopes,
        "is_superadmin": bool(user.get("is_superadmin")),
    }


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
        request.state.principal = _global_principal(user, list(ADMIN_SCOPES))
        return _attach_server_timing(await call_next(request), started_at)
    project = _request_project(request, path)
    if not store.has_project(project):
        return _attach_server_timing(
            JSONResponse({"detail": f"unknown project: {project}"}, status_code=400), started_at)
    scopes = _global_user_scopes(user, project)
    required = ("read",) if is_read else _write_required_scopes(path)
    if "admin" not in scopes and not set(required).issubset(set(scopes)):
        return _attach_server_timing(
            JSONResponse({"detail": "forbidden: no access to this project"}, status_code=403), started_at)
    request.state.principal = _global_principal(user, scopes)
    return _attach_server_timing(await call_next(request), started_at)


def _saturation_snapshot(project: str) -> dict:
    window_s = float(os.environ.get("PM_SQLITE_LOCK_WAIT_WINDOW_S", "60"))
    return saturation_signals.compute_saturation_signals(
        project=_proj(project),
        mcp_obs_provider=lambda: {
            "sqlite_lock_waits": store.sqlite_lock_wait_count(),
            "sqlite_lock_waits_window": store.sqlite_lock_waits_in_window(window_s),
            "sqlite_lock_wait_window_s": window_s,
        },
        request_obs_provider=_req_obs.snapshot,
    )


def _slow_request_log_ms() -> float:
    """Threshold (ms) above which a request is logged with its exact path. Default 500ms
    keeps the log to the genuinely-slow tail; PM_SLOW_REQUEST_LOG_MS=0 disables it."""
    try:
        return float(os.environ.get("PM_SLOW_REQUEST_LOG_MS", "500") or 0)
    except (TypeError, ValueError):
        return 500.0


@app.middleware("http")
async def _request_observability(request: Request, call_next):
    """Record per-route latency for PERF-7 SLO gates (web p99, webhook ingest p99)."""
    started_at = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    path = request.url.path
    _req_obs.record_path(
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
    if not store.has_project(project):
        return await call_next(request)
    snap = await asyncio.to_thread(_saturation_snapshot, project)
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





@app.get("/", include_in_schema=False)
async def root(request: Request):
    index = _static / "index.html"
    if _auth_service.current_user(request.cookies.get(_auth_session.COOKIE_NAME, "")):
        return _shell_response(index)
    if auth.auth_mode() == auth.DEV_OPEN:
        return _shell_response(index)
    return _shell_response(_static / "login-global.html")


@app.get("/login", include_in_schema=False)
async def login_page():
    login = _static / "login-global.html"
    if login.exists():
        return _shell_response(login)
    raise HTTPException(404, "login page not found")


@app.get("/signup", include_in_schema=False)
async def signup_page():
    page = _static / "signup.html"
    if page.exists():
        return _shell_response(page)
    raise HTTPException(404, "signup page not found")


@app.get("/account", include_in_schema=False)
async def account_page(request: Request):
    page = _static / "account.html"
    if page.exists():
        return _shell_response(page)
    raise HTTPException(404, "account page not found")


@app.get("/forgot-password", include_in_schema=False)
async def forgot_password_page():
    page = _static / "forgot-password.html"
    if page.exists():
        return _shell_response(page)
    raise HTTPException(404, "forgot-password page not found")


@app.get("/reset-password", include_in_schema=False)
async def reset_password_page():
    page = _static / "reset-password.html"
    if page.exists():
        return _shell_response(page)
    raise HTTPException(404, "reset-password page not found")


@app.get("/api/auth/me")
async def auth_me(request: Request, project: str = Query(store.DEFAULT_PROJECT)):
    """UI compatibility: map the global session to per-project effective scopes."""
    user = _auth_service.current_user(request.cookies.get(_auth_session.COOKIE_NAME, ""))
    if user:
        proj = _proj(project)
        scopes = _global_user_scopes(user, proj)
        principal = _global_principal(user, scopes)
        principal["project_roles"] = store.principal_project_roles(proj, user["id"])
        return {"principal": principal, "mode": auth.auth_mode(), "project": proj}
    principal = _principal(request, project, ("read",), dev_actor="web")
    return {"principal": auth.public_principal(principal),
            "mode": auth.auth_mode(), "project": _proj(project)}



@app.get("/api/board")
def board(request: Request, project: str = Query(store.DEFAULT_PROJECT)):
    # Sync (def, not async) on purpose: FastAPI runs it in the threadpool, so the
    # board's SQLite I/O doesn't block the single worker's event loop — other
    # requests (incl. /health) stay responsive while a board builds (HARDEN-36).
    # lite: drop heavy per-task fields the board UI never renders (re-fetched by
    # the task-detail modal); the store also serves a short-TTL cached copy.
    payload = store.board_payload(_proj(project), lite=True)
    # HARDEN-37: ETag + short max-age so a tab refocus / reload that finds the
    # board unchanged gets a bodyless 304 instead of re-downloading ~250KB.
    return _etag_json(request, payload, max_age=5)


@app.get("/api/people")
async def people(project: str = Query(store.DEFAULT_PROJECT)):
    return {"people": store.get_meta("people", store.DEFAULT_PEOPLE, project=_proj(project))}


@app.get("/api/dispatch/status")
async def dispatch_status(project: str = Query(store.DEFAULT_PROJECT)):
    """Is dispatch wired, and is a work-capable agent host online for this project?"""
    return await asyncio.to_thread(dispatch.status, _proj(project))


@app.get("/api/signals")
def plan_signals(project: str = Query(store.DEFAULT_PROJECT)):
    """Derived plan health: overdue / due-soon / blocked / ready / critical-slip /
    past-due decisions + each owner's next-best 1-2 tasks.

    Sync (def, not async) on purpose: compute_plan_signals walks the full enriched
    task list synchronously, so FastAPI runs it in the threadpool where its SQLite
    I/O can't block the single worker's event loop — other requests (incl. /health)
    stay responsive (HARDEN-36). The store also serves a short-TTL cached copy."""
    return signals.compute_plan_signals(project=_proj(project))


@app.get("/coordination", include_in_schema=False)
async def coordination_page():
    """Standalone, read-only Agent Coordination view (the agent-to-agent war room).
    Unlinked from the board nav on purpose; reachable by URL. Data comes from
    /api/coordination, which is gated by the normal read auth."""
    page = _static / "coordination.html"
    if page.exists():
        return _shell_response(page)
    raise HTTPException(404, "coordination page not found")


@app.get("/ixp/v1/saturation_signals")
def ixp_saturation_signals(project: str = Query(store.DEFAULT_PROJECT)):
    """REST parity for PERF-7 saturation dashboard (PSI + lock-wait + inbox + SLOs)."""
    return _saturation_snapshot(project)





@app.post("/ixp/v1/claim")
async def ixp_claim(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "agent")
    names = body.get("names") or body.get("files") or []
    if isinstance(names, str):
        names = [x.strip() for x in names.replace("\n", ",").split(",") if x.strip()]
    return store.claim_resources(
        agent_id=(body.get("agent_id") or auth.actor(principal)).strip(),
        resource_type=(body.get("resource_type") or "file").strip(),
        names=names, task_id=body.get("task") or body.get("task_id"),
        ttl_seconds=int(body.get("ttl_s") or body.get("ttl_seconds") or
                        (int(body.get("ttl_min") or 30) * 60)),
        principal_id=principal["id"], actor=auth.actor(principal),
        idem_key=body.get("idem_key") or "", project=project)


@app.post("/ixp/v1/check")
async def ixp_check(body: dict = Body(...)):
    project = _body_project(body)
    names = body.get("names") or body.get("files") or []
    if isinstance(names, str):
        names = [x.strip() for x in names.replace("\n", ",").split(",") if x.strip()]
    return {"held": store.check_resources((body.get("resource_type") or "file").strip(),
                                           names, project=project)}


@app.post("/ixp/v1/release")
async def ixp_release(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="agent")
    return store.release_resource_lease((body.get("lease_id") or "").strip(),
                                        actor=auth.actor(principal), project=project)


@app.get("/ixp/v1/leases")
async def ixp_leases(project: str = Query(store.DEFAULT_PROJECT)):
    return {"leases": store.list_active_resource_leases(project=_proj(project))}


# UI-8 Fleet control: wake-intent read/write over REST (hosts + runners already have
# their routes above). Mirrors the request_wake / list_wake_intents / cancel_wake tools.
@app.get("/ixp/v1/wake_intents")
async def ixp_wake_intents(project: str = Query(store.DEFAULT_PROJECT),
                           status: str = "", host_id: str = "", runtime: str = ""):
    return {"wake_intents": store.list_wake_intents(
        status=status, host_id=host_id, runtime=runtime, project=_proj(project))}


@app.post("/ixp/v1/request_wake")
async def ixp_request_wake(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/operator")
    payload = dict(body or {})
    payload["project"] = project
    if not payload.get("source"):
        payload["source"] = auth.actor(principal)
    result = request_wake_command.execute_mapping_result(
        payload, actor=auth.actor(principal), principal_id=principal["id"])
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/cancel_wake")
async def ixp_cancel_wake(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/operator")
    result = store.cancel_wake(
        (body.get("wake_id") or "").strip(), reason=body.get("reason") or "cancelled",
        actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.get("/ixp/v1/inbox")
async def ixp_inbox(project: str = Query(store.DEFAULT_PROJECT),
                    to_agent: str = "", unacked: bool = True, signal: str = ""):
    msgs = store.list_unacked_messages(to_agent, project=_proj(project)) if unacked else []
    if signal:
        msgs = [m for m in msgs if m.get("signal") == signal]
    return {"messages": msgs}


@app.get("/ixp/v1/message_status")
async def ixp_message_status(message_id: int, project: str = Query(store.DEFAULT_PROJECT)):
    msg = store.get_message_status(message_id, project=_proj(project))
    if not msg:
        raise HTTPException(404, "message not found")
    return msg


@app.get("/ixp/v1/pending_acks")
async def ixp_pending_acks(project: str = Query(store.DEFAULT_PROJECT), agent_id: str = ""):
    return {"pending_acks": store.list_pending_acks(agent_id=agent_id, project=_proj(project))}


@app.get("/ixp/v1/monitors")
async def ixp_monitors(project: str = Query(store.DEFAULT_PROJECT), status: str = "",
                       kind: str = "", task_id: str = ""):
    return {"monitors": store.list_coordination_monitors(status=status, kind=kind,
                                                         task_id=task_id,
                                                         project=_proj(project))}


@app.post("/ixp/v1/sweep_monitors")
async def ixp_sweep_monitors(request: Request, body: dict = Body(default={})):
    project = _body_project(body or {})
    _principal(request, project, ("write:ixp",), dev_actor="switchboard/monitor")
    return store.sweep_coordination_monitors(project=project)


@app.post("/ixp/v1/reconcile_alerts")
async def ixp_reconcile_alerts(request: Request, body: dict = Body(default={})):
    project = _body_project(body or {})
    _principal(request, project, ("write:ixp",), dev_actor="switchboard/reconcile")
    return store.run_reconcile_alerts(
        project=project,
        alert_to=body.get("alert_to") or "switchboard/operator",
        min_severity=body.get("min_severity") or "medium")


@app.post("/ixp/v1/resolve_monitor")
async def ixp_resolve_monitor(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/monitor")
    return store.resolve_monitor(body.get("monitor_id") or body.get("id") or "",
                                 reason=body.get("reason") or "manual",
                                 actor=auth.actor(principal), project=project)


@app.post("/ixp/v1/cancel_monitor")
async def ixp_cancel_monitor(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/monitor")
    return store.cancel_monitor(body.get("monitor_id") or body.get("id") or "",
                                reason=body.get("reason") or "cancelled",
                                actor=auth.actor(principal), project=project)


@app.get("/ixp/v1/delta")
async def ixp_delta(project: str = Query(store.DEFAULT_PROJECT), lane: str = "",
                    since_cursor: int = 0):
    return store.get_activity_delta(since_cursor=since_cursor, lane=lane, project=_proj(project))


@app.get("/ixp/v1/working_agreement")
async def ixp_working_agreement(project: str = Query(store.DEFAULT_PROJECT)):
    from switchboard.application.queries.working_agreement import execute
    return execute(project=_proj(project))


@app.post("/ixp/v1/bugs/submit")
async def ixp_submit_bug(request: Request, body: dict = Body(...)):
    from switchboard.application.commands.submit_bug import execute_mapping_result
    project = _body_project(body)
    principal = _principal(request, project, ("write:bug_intake",),
                           dev_actor=body.get("source_agent") or "bug-intake")
    result = execute_mapping_result(
        body,
        actor=auth.actor(principal),
        project=project,
    )
    if result.get("error"):
        raise HTTPException(400, result)
    return result


@app.get("/ixp/v1/reconcile")
async def ixp_reconcile(project: str = Query(store.DEFAULT_PROJECT)):
    return store.reconcile(project=_proj(project))


@app.get("/ixp/v1/background_jobs")
async def ixp_list_background_jobs():
    return store.list_background_jobs()


@app.get("/ixp/v1/background_jobs/runs")
async def ixp_list_background_job_runs(project: str = Query(store.DEFAULT_PROJECT),
                                     job_name: str = Query(""),
                                     limit: int = Query(20, ge=1, le=200)):
    return store.list_background_job_runs(
        project=_proj(project), job_name=job_name, limit=limit)


@app.get("/ixp/v1/background_jobs/runs/{run_id}")
async def ixp_get_background_job_run(run_id: str,
                                     project: str = Query(store.DEFAULT_PROJECT)):
    result = store.get_background_job_run(project=_proj(project), run_id=run_id)
    if result.get("error") == "run_not_found":
        raise HTTPException(404, result["error"])
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/background_jobs/{job_name}/run")
async def ixp_run_background_job(job_name: str, request: Request,
                                 project: str = Query(store.DEFAULT_PROJECT)):
    body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    try:
        import background_jobs
        return store.run_background_job(
            project=_proj(project),
            job_name=job_name,
            run_id=str(body.get("run_id") or ""),
            resume=bool(body.get("resume", True)),
            params=body.get("params") if isinstance(body.get("params"), dict) else body,
            actor=str(body.get("actor") or "api/background_job"),
        )
    except background_jobs.JobBoundaryError as exc:
        raise HTTPException(400, str(exc)) from exc


class _VersionedStaticFiles(StaticFiles):
    """StaticFiles that marks content-versioned assets (…?v=<hash>) immutable and
    long-cached: safe because a changed asset gets a new hash, hence a new URL, so a
    stale copy can never be served under a live URL. Requests without a ?v= keep
    StaticFiles' default etag/last-modified validators."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        params = scope.get("query_string", b"").split(b"&")
        if any(p == b"v" or p.startswith(b"v=") for p in params):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# Static board UI last, so /api/* and /health win. html=True serves index.html at /.
_static = Path(__file__).parent / "static"
if _static.exists():
    app.mount("/", _VersionedStaticFiles(directory=str(_static), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PM_PORT", "8110")))
