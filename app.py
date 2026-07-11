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
import hmac
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

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import agent  # noqa: E402
import attachments  # noqa: E402
import auth  # noqa: E402
import comms  # noqa: E402
import concurrency_limiter  # noqa: E402
import digest  # noqa: E402
import transcribe  # noqa: E402
import dispatch  # noqa: E402
import export  # noqa: E402
import external_ci_mirror  # noqa: E402
import inbox as inbox_mod  # noqa: E402
import intake  # noqa: E402
import github_sync  # noqa: E402
import webhook_inbox  # noqa: E402
import notify  # noqa: E402
import ocr  # noqa: E402
import rebrand  # noqa: E402
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
from switchboard.application.commands import create_task as create_task_command  # noqa: E402
from switchboard.api.routers.auth import service as _auth_service, session as _auth_session, store as _auth_store  # noqa: E402
from switchboard.api.routers.auth.routes import router as _global_auth_router  # noqa: E402

_auth_store.init()
app.include_router(_global_auth_router)

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


ADMIN_SCOPES = ["read", "write:tasks", "write:ixp", "write:system", "write:bug_intake", "admin"]


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


def _control_plane_http(result):
    if isinstance(result, dict) and result.get("error") == "control_plane_unavailable":
        raise HTTPException(503, result)
    if (isinstance(result, list) and result and isinstance(result[0], dict) and
            result[0].get("error") == "control_plane_unavailable"):
        raise HTTPException(503, result[0])
    return result


def _actor_from_request(request: Request, fallback: str = "user") -> str:
    p = getattr(request.state, "principal", None)
    return auth.actor(p) if p else fallback


def _resolve_public_write_actor(request: Request, project: str, body: dict,
                                task_id: str = "", scopes=("write:tasks",)):
    principal = _principal(request, project, scopes, dev_actor="web")
    binding = store.resolve_write_actor(
        auth.actor(principal),
        project=project,
        task_id=task_id,
        agent_id=(body or {}).get("agent_id") or "",
        system_actor=(body or {}).get("system_actor") or "",
        system_reason=(body or {}).get("system_reason") or "",
        principal_id=principal.get("id") or "",
    )
    if not binding.get("ok"):
        raise HTTPException(409, binding)
    return binding


def _record_public_write_binding(task_id: str, binding: dict, project: str) -> None:
    if not task_id or not isinstance(binding, dict):
        return
    if binding.get("binding") in ("principal", None):
        return
    store.append_activity(
        "principal.write_bound",
        "switchboard/identity",
        store.write_binding_activity_payload(binding),
        task_id=task_id,
        project=project,
    )


def _without_write_binding_fields(body: dict) -> dict:
    clean = dict(body or {})
    for key in ("agent_id", "system_actor", "system_reason"):
        clean.pop(key, None)
    return clean


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
    if ((path.startswith("/api/projects/") and path.endswith(("/repo_topology", "/github_repo"))) or
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
    if project in {p.get("id") for p in (user.get("projects") or [])}:
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


@app.get("/api/access/model")
async def access_model(request: Request, project: str = Query(store.DEFAULT_PROJECT)):
    principal = _principal(request, project, ("read",), dev_actor="web")
    return store.project_access_model(_proj(project), principal_id=principal["id"])


@app.post("/api/access/project_role")
async def access_grant_project_role(request: Request, body: dict = Body(...),
                                    project: str = Query(store.DEFAULT_PROJECT)):
    principal = _principal(request, project, ("write:system",), dev_actor="web")
    result = store.grant_project_role(
        _proj(project),
        subject_kind=(body or {}).get("subject_kind") or "principal",
        subject_id=(body or {}).get("subject_id") or "",
        role=(body or {}).get("role") or "",
        created_by=auth.actor(principal),
        scopes=store.coerce_csv_list((body or {}).get("scopes")) or None,
    )
    if result.get("error"):
        raise HTTPException(400, result["error"])
    store.append_activity(
        "access.project_role_granted",
        auth.actor(principal),
        result,
        task_id=None,
        project=_proj(project),
    )
    return result


# ---- UI-5: members & access management ----

def _resolve_member_identity(subject_kind: str, subject_id: str, principals_by_id: dict) -> dict:
    """Human-readable identity for a grant subject: principals resolve via the board's
    principal list; users resolve via the global-auth account store when it's enabled."""
    if subject_kind in ("principal", "agent", "system", "host"):
        p = principals_by_id.get(subject_id) or {}
        return {"display_name": p.get("display_name") or subject_id,
                "email": None, "revoked": bool(p.get("revoked_at")) if p else None}
    if subject_kind == "user":
        try:
            u = _auth_store.get_user(subject_id) or _auth_store.get_user_by_email(subject_id)
        except Exception:
            u = None
        if u:
            return {"display_name": u.get("display_name") or u.get("email") or subject_id,
                    "email": u.get("email"), "revoked": None}
    return {"display_name": subject_id,
            "email": subject_id if "@" in subject_id else None, "revoked": None}


@app.get("/api/access/members")
async def access_members(request: Request, project: str = Query(store.DEFAULT_PROJECT)):
    """Members table + private-visibility facts for the UI-5 Members screen (admin-gated).
    Decorates each role grant with a human-readable identity and the audit (granted-by/at)."""
    _principal(request, project, ("write:system",), dev_actor="web")
    proj = _proj(project)
    grants = store.list_project_role_grants(proj)
    principals_by_id = {p.get("id"): p for p in
                        store.list_principals(project=proj, include_revoked=True)}
    members = []
    for g in grants:
        ident = _resolve_member_identity(g["subject_kind"], g["subject_id"], principals_by_id)
        members.append({**g, "display_name": ident["display_name"], "email": ident["email"]})
    access = store.project_access(proj)
    return {
        "project": proj,
        "members": members,
        "access": access,
        "visibility": (access.get("visibility") or "org"),
        "owner_user_id": access.get("owner_user_id"),
        "role_definitions": {r: list(s) for r, s in sorted(store.ROLE_SCOPES.items())},
        "global_auth": True,
    }


@app.post("/api/access/project_role/revoke")
async def access_revoke_project_role(request: Request, body: dict = Body(...),
                                     project: str = Query(store.DEFAULT_PROJECT)):
    principal = _principal(request, project, ("write:system",), dev_actor="web")
    result = store.revoke_project_role(
        _proj(project),
        subject_kind=(body or {}).get("subject_kind") or "principal",
        subject_id=(body or {}).get("subject_id") or "",
        role=(body or {}).get("role") or "",
        created_by=auth.actor(principal),
    )
    if result.get("error"):
        raise HTTPException(400, result["error"])
    store.append_activity("access.project_role_revoked", auth.actor(principal), result,
                          task_id=None, project=_proj(project))
    return result


@app.post("/api/access/invite")
async def access_invite(request: Request, body: dict = Body(...),
                        project: str = Query(store.DEFAULT_PROJECT)):
    """Invite a human into this project by email + role. Under global auth this grants the
    role to their existing account (they see the project on next load); pending-invite email
    for not-yet-registered users is ACCESS-5's scope, so we return a clear next step instead."""
    principal = _principal(request, project, ("write:system",), dev_actor="web")
    proj = _proj(project)
    email = ((body or {}).get("email") or "").strip().lower()
    role = ((body or {}).get("role") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "a valid email is required")
    if not store.role_scopes(role):
        raise HTTPException(400, f"unknown role: {role}")
    user = _auth_store.get_user_by_email(email)
    if not user:
        raise HTTPException(
            404, f"no account for {email} yet — ask them to sign up, then invite again")
    result = store.grant_project_role(proj, subject_kind="user", subject_id=user["id"],
                                      role=role, created_by=auth.actor(principal))
    if result.get("error"):
        raise HTTPException(400, result["error"])
    store.append_activity("access.invited", auth.actor(principal),
                          {"email": email, "user_id": user["id"], "role": role},
                          task_id=None, project=proj)
    return {"project": proj, "grant": result,
            "invited": {"email": email, "user_id": user["id"],
                        "display_name": user.get("display_name")}}


@app.get("/api/access/tokens")
async def access_tokens(request: Request, project: str = Query(store.DEFAULT_PROJECT),
                        include_revoked: bool = False, kind: str = ""):
    _principal(request, project, ("write:system",), dev_actor="web")
    return {
        "project": _proj(project),
        "tokens": store.list_principals(
            project=_proj(project), include_revoked=include_revoked, kind=kind),
        "scope_definitions": store.principal_scope_definitions(),
        "valid_kinds": sorted(store.VALID_PRINCIPAL_KINDS),
    }


@app.post("/api/access/tokens")
async def access_create_token(request: Request, body: dict = Body(...),
                              project: str = Query(store.DEFAULT_PROJECT)):
    principal = _principal(request, project, ("write:system",), dev_actor="web")
    target_project = _proj(project)
    resolved = store.resolve_principal_scopes(
        (body or {}).get("scopes"), role=(body or {}).get("role") or "")
    if resolved.get("error"):
        raise HTTPException(400, resolved["error"])
    kind = store.validate_principal_kind((body or {}).get("kind") or "agent")
    if not kind:
        raise HTTPException(400, "kind must be one of: " + ", ".join(sorted(store.VALID_PRINCIPAL_KINDS)))
    raw_token = auth.new_secret_token()
    created = store.create_principal(
        kind=kind,
        display_name=((body or {}).get("display_name") or kind).strip(),
        token=raw_token,
        scopes=resolved["scopes"],
        principal_id=((body or {}).get("principal_id") or None),
        project=target_project,
    )
    if created.get("error"):
        raise HTTPException(400, created["error"])
    public = store.public_principal_record(created, project=target_project)
    store.append_activity(
        "access.token_created",
        auth.actor(principal),
        {"principal": public, "role": resolved.get("role"), "token_returned_once": True},
        task_id=None,
        project=target_project,
    )
    return {"project": target_project, "principal": public, "token": raw_token,
            "token_returned_once": True}


@app.post("/api/access/tokens/{principal_id}/revoke")
async def access_revoke_token(principal_id: str, request: Request,
                              project: str = Query(store.DEFAULT_PROJECT)):
    principal = _principal(request, project, ("write:system",), dev_actor="web")
    result = store.revoke_principal_token(
        principal_id, project=_proj(project), actor=auth.actor(principal))
    if result.get("error"):
        raise HTTPException(404 if "not found" in result["error"] else 400, result["error"])
    return result


@app.get("/health")
async def health():
    """Liveness probe — must stay cheap so monitors/Caddy never block the event loop."""
    return {"status": "ok", "service": "taikun-pm"}


@app.get("/health/deep")
async def health_deep():
    """Readiness probe. Publicly routed under Caddy's /health*, so it must NOT expose any
    project data (ids, task counts, names). It verifies that every configured board db is
    accessible with the required schema, and fails CLOSED (503) if any configured project
    could not initialize at startup or is not reachable now. BUG-48."""
    def _probe():
        configured = store.project_ids()
        unready = 0
        for pid in configured:
            # A project skipped at startup is unready regardless of a later live probe;
            # otherwise re-check the db + schema live so a db that went away is caught.
            reason = _PROJECT_INIT_FAILURES.get(pid) or store.probe_project_db(pid)
            if reason:
                unready += 1
                # Detail (which project, why) goes to the server log only — never the wire.
                print(f"[readiness] project not ready: {pid}: {reason}")
        return len(configured), unready

    projects_configured, projects_unready = await asyncio.to_thread(_probe)
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


@app.get("/health/saturation")
def health_saturation(project: str = Query(store.DEFAULT_PROJECT)):
    """Cheap saturation/alerts probe for external monitors (PERF-7)."""
    snap = _saturation_snapshot(project)
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


@app.get("/api/saturation")
def api_saturation(project: str = Query(store.DEFAULT_PROJECT)):
    """Full saturation dashboard payload: PSI, lock-wait, inbox depth, SLOs, alerts."""
    return _saturation_snapshot(project)


# ---- NARRATE-13: narration queue health + authorized operator controls ----

@app.get("/api/narration/health")
def api_narration_health(request: Request, project: str = Query(store.DEFAULT_PROJECT)):
    """Bounded narration queue + receipt/cost snapshot with alert flags (read-only)."""
    _principal(request, project, ("read",), dev_actor="web")
    return narration_ops.narration_health(_proj(project))


@app.post("/api/narration/narrate-now")
async def api_narrate_now(request: Request, body: dict = Body(...),
                          project: str = Query(store.DEFAULT_PROJECT)):
    """Force (re)generation of an entity's current narration revision — audited, deduped, and
    still subject to the generation budget (no silent bypass)."""
    principal = _principal(request, project, ("write:system",), dev_actor="web")
    result = narration_ops.narrate_now(
        _proj(project), (body or {}).get("entity_type") or "",
        (body or {}).get("entity_id") or "", actor=auth.actor(principal),
        reason=(body or {}).get("reason") or "")
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/narration/reactivate")
async def api_narration_reactivate(request: Request, body: dict = Body(...),
                                   project: str = Query(store.DEFAULT_PROJECT)):
    """Authorized retry / dead-letter recovery on a narration request (audited)."""
    principal = _principal(request, project, ("write:system",), dev_actor="web")
    result = narration_ops.reactivate_request(
        _proj(project), (body or {}).get("event_id") or "", actor=auth.actor(principal),
        action=(body or {}).get("action") or "retry", reason=(body or {}).get("reason") or "")
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


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


@app.get("/api/projects")
def list_projects(request: Request):
    """The project switcher's source of truth — filtered to accessible projects."""
    if auth.auth_mode() == auth.DEV_OPEN and not request.cookies.get(_auth_session.COOKIE_NAME, ""):
        return {"projects": store.projects(), "default": store.DEFAULT_PROJECT}
    user = _auth_service.current_user(request.cookies.get(_auth_session.COOKIE_NAME, ""))
    if not user:
        raise HTTPException(401, "not authenticated")
    return {"projects": user.get("projects", []), "default": ""}


@app.post("/api/projects")
async def create_project(request: Request, body: dict = Body(...)):
    # ACCESS-14: contributors (write:projects) can create projects, not just admins.
    # Human-created projects default to private (creator + invitees + org admins see them);
    # pass visibility="org" to make one org-wide shared.
    principal = _principal(request, "switchboard", ("write:projects",), dev_actor="web")
    created = store.create_project(
        name=body.get("name") or body.get("label") or "",
        project_id=body.get("project_id") or body.get("id") or "",
        label=body.get("label") or "",
        pretitle=body.get("pretitle") or "",
        github_repo=body.get("github_repo") or body.get("repo") or "",
        owner_principal_id=principal["id"],
        org_id=body.get("org_id") or store.DEFAULT_ORG_ID,
        purpose=body.get("purpose") or "",
        boundary=body.get("boundary") or "",
        visibility=(body.get("visibility") or "private").strip().lower(),
        actor=auth.actor(principal),
    )
    if created.get("error"):
        raise HTTPException(400, created["error"])
    return created


@app.get("/api/projects/{project}/repo_topology")
async def project_repo_topology(project: str):
    return store.get_project_repo_topology(project=_proj(project))


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


@app.get("/api/projects/{project}/context")
def project_context(request: Request, project: str):
    # HARDEN-35: project_context (repo roles, hierarchy, policy profiles) is a
    # near-static ~9KB blob that used to ride on every /api/board load. It lives
    # here now so the board payload stays slim; ETag + a short max-age let a tab
    # refocus / reload reuse the browser-cached copy (bodyless 304). Sync def so
    # its SQLite I/O runs in the threadpool, like /api/board (HARDEN-36).
    payload = store.get_project_context(project=_proj(project))
    return _etag_json(request, payload, max_age=60)


@app.post("/api/projects/{project}/repo_topology")
async def set_project_repo_topology(request: Request, project: str, body: dict = Body(...)):
    _principal(request, "switchboard", ("write:system",), dev_actor="web")
    result = store.set_project_repo_topology(
        project=_proj(project),
        canonical_repo=body.get("canonical_repo") or body.get("private_repo") or "",
        public_ci_repo=body.get("public_ci_repo") or body.get("ci_repo") or "",
        public_repo=body.get("public_repo") or "",
        release_repo=body.get("release_repo") or "",
        topology_type=body.get("topology_type") or "",
        canonical_default_branch=body.get("canonical_default_branch") or body.get("default_branch") or "",
        public_ci_required_status_contexts=(
            body.get("public_ci_required_status_contexts") or
            body.get("ci_required_status_contexts") or
            body.get("required_status_contexts") or
            ""
        ),
        public_ci_sync_scripts=(
            body.get("public_ci_sync_scripts") or
            body.get("ci_sync_scripts") or
            body.get("sync_scripts") or
            ""
        ),
        public_publish_scripts=body.get("public_publish_scripts") or body.get("publish_scripts") or "",
        release_publish_scripts=body.get("release_publish_scripts") or "",
    )
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/projects/{project}/github_association")
def project_github_association(request: Request, project: str, check: int = 0):
    """UI-15: everything the "Wire your repo" panel needs — the webhook payload URL with
    the ?project= pin PRE-FILLED (HARDEN-2/BUG-24: bare URLs fail closed on shared repos),
    the secret name, a copyable gh one-liner, and delivery-based verification. Pass ?check=1
    (the Verify button) to also probe repo reachability; the panel open path omits it so it
    never makes a network call until the operator asks."""
    project = _proj(project)
    repo = store.get_project_github_repo(project) or ""
    base = str(request.base_url).rstrip("/")
    payload_url = f"{base}/api/github/webhook?project={project}"
    gh_command = ""
    if repo:
        gh_command = (
            f"gh api -X POST repos/{repo}/hooks -f name=web -F active=true "
            f"-f 'events[]=push' -f 'events[]=pull_request' "
            f"-f 'config[url]={payload_url}' -f config[content_type]=json "
            f"-f 'config[secret]=$PM_GITHUB_WEBHOOK_SECRET'"
        )
    deliveries = store.github_webhook_deliveries(project)
    reachable = store.github_repo_reachable(repo) if (check and repo) else None
    status = "connected" if deliveries["delivered"] else ("configured" if repo else "unconfigured")
    return {
        "project": project,
        "repo": repo,
        "repo_configured": bool(repo),
        "webhook": {
            "payload_url": payload_url,
            "content_type": "application/json",
            "secret_env": "PM_GITHUB_WEBHOOK_SECRET",
            "secret_configured": bool(_GH_SECRET),
            "events": ["push", "pull_request"],
            "gh_command": gh_command,
        },
        "verification": {**deliveries, "status": status, "repo_reachable": reachable},
    }


@app.post("/api/projects/{project}/github_repo")
async def set_project_github_repo_route(request: Request, project: str, body: dict = Body(...)):
    """UI-15: record/replace a project's canonical repo from the web (Settings path for
    existing projects). Reroutes Done/webhook provenance, so it is gated like repo_topology."""
    project = _proj(project)
    _principal(request, project, ("write:system",), dev_actor="web")
    result = store.set_project_github_repo(
        repo=body.get("github_repo") or body.get("repo") or "", project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/projects/{project}/comms")
def project_comms(request: Request, project: str):
    """UI-14: everything the Settings → Communications screen needs — the project's plus-address,
    its associated inbound domains (the editable UI-13 routing map), per-project digest/notify
    recipients + cadence, the global .env fallback, and channel status. Readable to anyone who can
    read the project; edits below are admin-gated."""
    project = _proj(project)
    cfg = comms.get_config(project)
    # Reflect whether THIS caller may edit, so the UI can disable Save/Test up front instead of
    # only failing on POST. Non-raising probe of the same scope the write routes require.
    try:
        auth.authenticate_request(request, project, ("write:system",), dev_actor="web")
        cfg["can_edit"] = True
    except PermissionError:
        cfg["can_edit"] = False
    return cfg


@app.post("/api/projects/{project}/comms")
async def set_project_comms(request: Request, project: str, body: dict = Body(...)):
    """UI-14: persist a Communications edit — associated inbound domains and/or outbound
    recipients/cadence. Reroutes inbound mail and outbound recipients, so it is admin-gated
    (write:system, same as repo settings) and audited."""
    project = _proj(project)
    principal = _principal(request, project, ("write:system",), dev_actor="web")
    result = comms.update_config(body or {}, project=project, actor=auth.actor(principal))
    if result.get("error"):
        raise HTTPException(400, result["error"])
    store.append_activity("comms.updated", auth.actor(principal),
                          result.get("audit") or {}, project=project)
    return result


@app.post("/api/projects/{project}/comms/test")
async def test_project_comms(request: Request, project: str, body: dict = Body(...)):
    """UI-14 Send-test: email the project's effective recipients so an operator can confirm the
    wiring end-to-end. Admin-gated + audited; dry-runs (logs, sent=false) until SMTP is configured."""
    project = _proj(project)
    principal = _principal(request, project, ("write:system",), dev_actor="web")
    kind = (body or {}).get("kind") or "notify"
    if kind not in ("notify", "digest"):
        raise HTTPException(400, "kind must be 'notify' or 'digest'")
    recipients = comms.recipients_for(project, kind) or comms.global_fallback_recipients()
    subject = f"{project} — communications test"
    text = (f"Communications test from plan.taikunai.com for project '{project}'. "
            f"If you received this, {project}'s {kind} recipients are wired correctly.")
    results = await asyncio.to_thread(notify.send, subject, text, ("email",), project, kind)
    store.append_activity("comms.test_sent", auth.actor(principal),
                          {"kind": kind, "recipients": recipients, "results": results},
                          project=project)
    return {"project": project, "kind": kind, "recipients": recipients, "results": results}


@app.get("/api/projects/{project}/boards")
async def list_project_boards(project: str, kind: str = "", status: str = ""):
    project = _proj(project)
    return {"project": project, "boards": store.list_project_boards(
        project=project, kind=kind, status=status)}


@app.post("/api/projects/{project}/boards")
async def create_project_board(request: Request, project: str, body: dict = Body(...)):
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    result = store.create_project_board(body or {}, actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/projects/{project}/boards/{board_id}")
async def get_project_board(project: str, board_id: str):
    project = _proj(project)
    result = store.get_project_board(board_id, project=project)
    if not result:
        raise HTTPException(404, "board not found")
    return result


@app.get("/api/deliverables")
def list_deliverables(project: str = Query(store.DEFAULT_PROJECT), board_id: str = "",
                      view: str = ""):
    # def (not async): run the SQLite/deliverable work in the threadpool so a slow
    # deliverable read can't block the single worker's event loop (same as /api/board).
    project = _proj(project)
    if view == "picker":
        return {"project": project, "board_id": board_id or None, "view": "picker",
                "deliverables": store.list_deliverable_summaries(
                    project=project, board_id=board_id)}
    if view:
        raise HTTPException(400, "unknown deliverable list view")
    return {"project": project, "board_id": board_id or None,
            "deliverables": store.list_deliverables(project=project, board_id=board_id)}


@app.post("/api/deliverables")
async def create_deliverable(request: Request, body: dict = Body(...),
                             project: str = Query(...)):
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    result = store.create_deliverable(body or {}, actor=auth.actor(principal), project=project)
    if result.get("error"):
        # DELIVERABLES-13: surface the full error object (error + per-field details) when the
        # intake gate rejects a move into in_progress, so the operator sees which fields are
        # missing rather than a bare message. Falls back to the string for simple errors.
        raise HTTPException(400, result if result.get("details") else result["error"])
    return result


# These literal-path reads MUST be registered before the `/{deliverable_id}` route
# below — otherwise Starlette matches `/api/deliverables/breakdown_proposals` against
# `{deliverable_id}` (first-registered wins) and the list 404s as an "unknown
# deliverable". Keep them here; do not move them back down with the other breakdown
# routes (UI-1).
@app.get("/api/deliverables/breakdown_proposals")
async def list_deliverable_breakdown_proposals(deliverable_id: str = "",
                                               project: str = Query(...),
                                               status: str = ""):
    project = _proj(project)
    return {
        "project": project,
        "deliverable_id": deliverable_id or None,
        "proposals": store.list_deliverable_breakdown_proposals(
            deliverable_id=deliverable_id, project=project, status=status),
    }


@app.get("/api/deliverables/breakdown_proposals/{proposal_id}")
async def get_deliverable_breakdown_proposal(proposal_id: str,
                                             project: str = Query(...)):
    project = _proj(project)
    result = store.get_deliverable_breakdown_proposal(proposal_id, project=project)
    if not result:
        raise HTTPException(404, "proposal not found")
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/deliverables/{deliverable_id}")
def get_deliverable(deliverable_id: str, project: str = Query(store.DEFAULT_PROJECT)):
    # def (not async): threadpool the deliverable read so it can't block the event loop.
    project = _proj(project)
    result = store.get_deliverable(deliverable_id, project=project)
    if not result:
        raise HTTPException(404, "deliverable not found")
    return result


@app.post("/api/deliverables/{deliverable_id}/milestones")
async def add_deliverable_milestone(request: Request, deliverable_id: str,
                                    body: dict = Body(...),
                                    project: str = Query(...)):
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    result = store.add_deliverable_milestone(
        deliverable_id, body or {}, actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/deliverables/{deliverable_id}/task_links")
async def link_task_to_deliverable(request: Request, deliverable_id: str,
                                   body: dict = Body(...),
                                   project: str = Query(...)):
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    payload = body or {}
    result = store.link_task_to_deliverable(
        deliverable_id,
        payload.get("task_project") or payload.get("project_id") or "",
        payload.get("task_id") or "",
        milestone_id=payload.get("milestone_id") or "",
        data=payload,
        actor=auth.actor(principal),
        project=project,
    )
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.delete("/api/deliverables/{deliverable_id}/task_links")
async def unlink_task_from_deliverable(request: Request, deliverable_id: str,
                                       task_project: str = Query(...),
                                       task_id: str = Query(...),
                                       project: str = Query(...)):
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    result = store.unlink_task_from_deliverable(
        deliverable_id, task_project, task_id,
        actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/mission_status")
def mission_status_query(request: Request, project: str = Query(...), deliverable_id: str = "",
                         board_id: str = "", mission_id: str = ""):
    project = _proj(project)
    result = store.get_mission_status(
        project=project, deliverable_id=deliverable_id,
        board_id=board_id, mission_id=mission_id)
    if result.get("error"):
        code = 404 if "unknown" in result["error"] or "no deliverable" in result["error"] else 400
        raise HTTPException(code, result["error"])
    # CONSOL-8: the live cockpit polls this on a 5s timer. The store already serves a
    # short-TTL cached copy (HARDEN-36); the ETag/304 gives the mission pollers the same
    # wire-level parity /api/board has — an unchanged tick returns a bodyless 304.
    return _etag_json(request, result, max_age=5)


@app.get("/api/deliverables/{deliverable_id}/mission_status")
def deliverable_mission_status(request: Request, deliverable_id: str, project: str = Query(...)):
    project = _proj(project)
    result = store.get_mission_status(project=project, deliverable_id=deliverable_id)
    if result.get("error"):
        code = 404 if "unknown" in result["error"] else 400
        raise HTTPException(code, result["error"])
    return _etag_json(request, result, max_age=5)  # CONSOL-8: TTL+ETag poll parity


@app.get("/api/deliverables/{deliverable_id}/dependency_graph")
def deliverable_dependency_graph(request: Request, deliverable_id: str, project: str = Query(...)):
    # def (not async): threadpool the graph build so it can't block the event loop.
    project = _proj(project)
    result = store.get_deliverable_dependency_graph(project=project, deliverable_id=deliverable_id)
    if result.get("error"):
        code = 404 if "unknown" in result["error"] else 400
        raise HTTPException(code, result["error"])
    return _etag_json(request, result, max_age=5)  # CONSOL-8: TTL+ETag poll parity


@app.post("/api/deliverables/{deliverable_id}/coordinator_tick")
async def run_mission_coordinator_tick(request: Request, deliverable_id: str,
                                       project: str = Query(...)):
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    body = body or {}
    result = store.run_mission_coordinator_tick(
        project=project,
        deliverable_id=deliverable_id,
        board_id=body.get("board_id") or "",
        mission_id=body.get("mission_id") or "",
        coordinator_agent_id=body.get("coordinator_agent_id") or "",
        actor=auth.actor(principal),
        idem_key=body.get("idem_key") or "",
        policy=body.get("policy"),
    )
    if result.get("error"):
        code = 404 if "unknown" in result["error"] else 400
        raise HTTPException(code, result["error"])
    return result


@app.post("/api/deliverables/{deliverable_id}/mission_brief")
async def generate_mission_brief(request: Request, deliverable_id: str,
                                 project: str = Query(...),
                                 persist: bool = Query(True)):
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    result = store.generate_mission_brief(
        project=project, deliverable_id=deliverable_id,
        actor=auth.actor(principal), persist=persist)
    if result.get("error"):
        code = 404 if "unknown" in result["error"] else 400
        raise HTTPException(code, result["error"])
    return result


@app.patch("/api/deliverables/{deliverable_id}/narrative")
async def update_mission_narrative(request: Request, deliverable_id: str,
                                   body: dict = Body(...),
                                   project: str = Query(...)):
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    payload = body or {}
    result = store.update_mission_narrative(
        deliverable_id, payload.get("narrative") or "",
        actor=auth.actor(principal), project=project,
        append=bool(payload.get("append")))
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/deliverables/{deliverable_id}/breakdown_proposals")
async def propose_deliverable_breakdown(request: Request, deliverable_id: str,
                                        body: dict = Body(...),
                                        project: str = Query(...)):
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    payload = body or {}
    result = store.propose_deliverable_breakdown(
        deliverable_id, payload, actor=auth.actor(principal), project=project,
        proposal_id=payload.get("proposal_id") or "")
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/deliverables/breakdown_proposals/{proposal_id}/approve")
async def approve_deliverable_breakdown(request: Request, proposal_id: str,
                                        project: str = Query(...)):
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    result = store.approve_deliverable_breakdown(
        proposal_id, actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/deliverables/{deliverable_id}/outcome")
async def submit_deliverable_outcome(request: Request, deliverable_id: str,
                                     body: dict = Body(...),
                                     project: str = Query(...)):
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    payload = body or {}
    result = store.submit_deliverable_outcome(
        deliverable_id, payload.get("outcome") or "",
        actor=auth.actor(principal), project=project,
        target_projects=payload.get("target_projects"),
        policy_constraints=payload.get("policy_constraints"),
        acceptance_criteria=payload.get("acceptance_criteria"),
        use_llm=bool(payload.get("use_llm")),
    )
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/deliverables/{deliverable_id}/archive")
async def archive_deliverable_route(request: Request, deliverable_id: str,
                                    body: dict = Body(default={}),
                                    project: str = Query(...)):
    """UI-11: archive a deliverable (or restore it). Body {"archived": bool} (default true)."""
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    archived = True if not body else bool(body.get("archived", True))
    result = store.archive_deliverable(
        deliverable_id, project=project, actor=auth.actor(principal), archived=archived)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.patch("/api/deliverables/breakdown_proposals/{proposal_id}")
async def update_deliverable_breakdown_proposal(request: Request, proposal_id: str,
                                                body: dict = Body(...),
                                                project: str = Query(...)):
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    payload = body or {}
    result = store.update_deliverable_breakdown_proposal(
        proposal_id, payload, actor=auth.actor(principal), project=project,
        outcome_text=payload.get("outcome") or "")
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/deliverables/breakdown_proposals/{proposal_id}/reject")
async def reject_deliverable_breakdown(request: Request, proposal_id: str,
                                       body: dict = Body(...),
                                       project: str = Query(...)):
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    payload = body or {}
    result = store.reject_deliverable_breakdown(
        proposal_id, payload.get("reason") or "",
        actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/deliverables/breakdown_proposals/{proposal_id}/defer")
async def defer_deliverable_breakdown(request: Request, proposal_id: str,
                                      body: dict = Body(...),
                                      project: str = Query(...)):
    project = _proj(project)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    payload = body or {}
    defer_until = payload.get("defer_until")
    if defer_until not in (None, ""):
        try:
            defer_until = float(defer_until)
        except (TypeError, ValueError):
            raise HTTPException(400, "defer_until must be a unix timestamp")
    result = store.defer_deliverable_breakdown(
        proposal_id, payload.get("reason") or "",
        actor=auth.actor(principal), project=project,
        defer_until=defer_until)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


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


@app.get("/api/tasks")
async def list_tasks(workstream: str = None, status: str = None, assignee: str = None,
                     project: str = Query(store.DEFAULT_PROJECT)):
    return {"tasks": store.list_tasks(workstream, status, assignee, project=_proj(project))}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, project: str = Query(store.DEFAULT_PROJECT)):
    t = store.get_task(task_id, project=_proj(project))
    if not t:
        raise HTTPException(404, "task not found")
    return t


@app.post("/api/tasks")
async def create_task(request: Request, body: dict = Body(...), project: str = Query(...)):
    project = _proj(project)
    binding = _resolve_public_write_actor(request, project, body)
    t = create_task_command.execute_mapping_result(_without_write_binding_fields(body), actor=binding["actor"], project=project)
    if t.get("error"):
        raise HTTPException(400, t)
    _record_public_write_binding(t.get("task_id") or "", binding, project)
    return t


@app.patch("/api/tasks/{task_id}")
async def patch_task(request: Request, task_id: str, body: dict = Body(...), project: str = Query(...)):
    project = _proj(project)
    body = dict(body or {})
    binding = _resolve_public_write_actor(request, project, body, task_id=task_id)
    actor = binding["actor"]
    t = store.update_task(task_id, _without_write_binding_fields(body),
                          actor=actor, project=project)
    if not t:
        raise HTTPException(404, "task not found")
    if t.get("error") == "done_requires_merge_provenance":
        raise HTTPException(409, t.get("message") or "Done requires merge provenance")
    _record_public_write_binding(task_id, binding, project)
    return t


@app.post("/api/tasks/{task_id}/verify_offline")
async def verify_task_offline(request: Request, task_id: str, body: dict = Body(default={}),
                              project: str = Query(...)):
    project = _proj(project)
    body = dict(body or {})
    binding = _resolve_public_write_actor(request, project, body, task_id=task_id)
    actor = binding["actor"]
    result = store.mark_task_offline_done(
        task_id,
        evidence=body.get("evidence") or body.get("evidence_json") or {},
        artifact_url=body.get("artifact_url") or "",
        evidence_hash=body.get("evidence_hash") or body.get("hash") or "",
        verifier=body.get("verifier") or actor,
        reviewed_at=body.get("reviewed_at"),
        actor=actor,
        project=project,
    )
    if result.get("error") == "task not found":
        raise HTTPException(404, result)
    if result.get("error"):
        raise HTTPException(409, result)
    _record_public_write_binding(task_id, binding, project)
    return result


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, project: str = Query(...)):
    if not store.delete_task(task_id, project=_proj(project)):
        raise HTTPException(404, "task not found")
    return {"deleted": task_id}


@app.post("/api/tasks/{task_id}/archive")
async def archive_task(request: Request, task_id: str, body: dict = Body(default={}),
                       project: str = Query(...)):
    project = _proj(project)
    principal = _principal(request, "switchboard", ("write:system",), dev_actor="web")
    result = store.archive_task(
        task_id, reason=(body or {}).get("reason") or "",
        actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/tasks/{task_id}/move")
async def move_task(request: Request, task_id: str, body: dict = Body(...),
                    project: str = Query(...)):
    project_from = _proj(project)
    project_to = _proj((body or {}).get("project_to") or (body or {}).get("destination_project") or "")
    principal = _principal(request, "switchboard", ("write:system",), dev_actor="web")
    result = store.move_task(
        task_id, project_from=project_from, project_to=project_to,
        reason=(body or {}).get("reason") or "",
        actor=auth.actor(principal),
        new_task_id=(body or {}).get("new_task_id") or "",
        dependency_policy=(body or {}).get("dependency_policy") or "fail",
    )
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/tasks/{task_id}/claims/{claim_id}/revoke")
async def api_revoke_claim(request: Request, task_id: str, claim_id: str,
                           body: dict = Body(default={}), project: str = Query(...)):
    project = _proj(project)
    body = body or {}
    actor = _actor_from_request(request, "switchboard/operator")
    sort_order = body.get("sort_order")
    try:
        sort_order_value = int(sort_order) if sort_order not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(400, "sort_order must be an integer")
    result = store.revoke_claim(
        claim_id,
        reason=body.get("reason") or "operator override",
        reassign_to=body.get("reassign_to") or body.get("reassigned_to") or "",
        sort_order=sort_order_value,
        partial_evidence=body.get("partial_evidence") or body.get("evidence") or {},
        notify=body.get("notify") is not False,
        ack_deadline_minutes=float(body.get("ack_deadline_minutes") or 5),
        expected_task_id=task_id,
        actor=actor,
        project=project,
    )
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/tasks/{task_id}/comment")
async def comment(request: Request, task_id: str, body: dict = Body(...), project: str = Query(...)):
    project = _proj(project)
    body = dict(body or {})
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    binding = _resolve_public_write_actor(request, project, body, task_id=task_id)
    _record_public_write_binding(task_id, binding, project)
    t = store.add_comment(task_id, binding["actor"], text, project=project)
    if not t:
        raise HTTPException(404, "task not found")
    return t


@app.get("/api/dispatch/status")
async def dispatch_status(project: str = Query(store.DEFAULT_PROJECT)):
    """Is dispatch wired, and is a work-capable agent host online for this project?"""
    return await asyncio.to_thread(dispatch.status, _proj(project))


@app.post("/api/tasks/{task_id}/dispatch")
async def dispatch_task(task_id: str, body: dict = Body(default={})):
    """Queue a lane-scoped work-session wake for this task (→ a work-capable agent host claims it
    and opens a PR on a claude/ branch — never main). The human-triggered entry."""
    project = _body_project(body)
    res = await asyncio.to_thread(dispatch.dispatch, task_id, (body or {}).get("actor", "user"), project)
    if res.get("error") == "task not found":
        raise HTTPException(404, "task not found")
    return res


@app.get("/api/tasks/{task_id}/dispatch/latest")
async def task_dispatch_latest(task_id: str, project: str = Query(store.DEFAULT_PROJECT)):
    """The current dispatch state for a task (none|queued|claiming|running|pr) for the Dev-tab panel."""
    return await asyncio.to_thread(dispatch.latest, task_id, _proj(project))


@app.post("/api/tasks/{task_id}/chat")
async def chat(task_id: str, body: dict = Body(...), project: str = Query(store.DEFAULT_PROJECT)):
    """Per-task Ask Taikun agent: RAG over the plan docs + propose-then-confirm task edits."""
    project = _proj(project)
    assistant = {"helm": "Helm", "switchboard": "Switchboard"}.get(project, "Maxwell")
    task = store.get_task(task_id, project=project)
    if not task:
        raise HTTPException(404, "task not found")
    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "message required")
    history = []
    for a in task.get("activity", []):
        if a.get("kind") == "chat":
            text = (a.get("payload") or {}).get("text", "")
            if text:
                history.append({"role": "user" if a.get("actor") == "user" else "assistant", "content": text})
    history = history[-8:]
    store.add_comment(task_id, "user", msg, kind="chat", project=project)
    try:
        result = await asyncio.to_thread(agent.run, task, msg, history, project=project)
    except Exception as e:
        store.add_comment(task_id, assistant, f"(agent error: {e})", kind="chat", project=project)
        raise HTTPException(502, f"agent error: {e}")
    answer = result.get("answer") or ""
    store.add_comment(task_id, assistant, answer, kind="chat", project=project)
    return {"answer": answer, "proposal": result.get("proposal"), "sources": result.get("sources", [])}


@app.post("/api/chat")
async def plan_chat(body: dict = Body(...), project: str = Query(store.DEFAULT_PROJECT)):
    """Plan-wide Ask Taikun: the global agent sees the whole board + docs; propose-to-confirm."""
    project = _proj(project)
    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "message required")
    session = body.get("session") or "plan"
    history = [{"role": m["role"], "content": m["content"]}
               for m in store.recent_chat(session, 16, project=project) if m.get("content")]
    store.add_chat(session, "user", msg, project=project)
    try:
        result = await asyncio.to_thread(agent.run, None, msg, history, project=project)
    except Exception as e:
        store.add_chat(session, "assistant", f"(agent error: {e})", project=project)
        raise HTTPException(502, f"agent error: {e}")
    answer = result.get("answer") or ""
    store.add_chat(session, "assistant", answer,
                   {"proposals": result.get("proposals", []), "sources": result.get("sources", [])},
                   project=project)
    return {"answer": answer, "proposal": result.get("proposal"),
            "proposals": result.get("proposals", []), "sources": result.get("sources", [])}


@app.get("/api/chat/history")
async def plan_chat_history(session: str = "plan", project: str = Query(store.DEFAULT_PROJECT)):
    return {"messages": store.recent_chat(session, 100, project=_proj(project))}


@app.delete("/api/chat")
async def clear_plan_chat(session: str = "plan", project: str = Query(store.DEFAULT_PROJECT)):
    store.clear_chat(session, project=_proj(project))
    return {"cleared": session}


def _queue_triage(res, source, subject, project=None):
    """Persist a triage result into the Action Queue (Inbox) as a pending item so its proposed
    changes survive reload and are bulk-confirmable in one place — not just ephemeral chat cards.
    Only queues when there's something to act on. Mutates + returns `res` with inbox_id.
    The item lands on `project`'s inbox (same board the artifact was ingested on)."""
    project = project or store.DEFAULT_PROJECT
    try:
        if res and ((res.get("proposals")) or (res.get("new_tasks"))):
            triage = {"proposals": res.get("proposals", []), "new_tasks": res.get("new_tasks", []),
                      "sources": res.get("sources", []), "summary": res.get("summary", "")}
            res["inbox_id"] = store.add_inbox_item(
                source, source + "-" + os.urandom(6).hex(), "", subject or source,
                res.get("summary", ""), triage, project=project)
    except Exception:
        pass  # queueing is best-effort; the chat cards still work
    return res


@app.post("/api/intake")
async def intake_artifact(body: dict = Body(...), project: str = Query(store.DEFAULT_PROJECT)):
    """Ingest an artifact (transcript/email/document) into `project`'s RAG corpus + triage it
    against that board. Returns {summary, proposals, new_tasks, sources, ingested_chunks, inbox_id}."""
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    try:
        res = await asyncio.to_thread(
            intake.ingest_and_triage, body.get("kind") or "note", body.get("title") or "", text,
            project=project)
        return _queue_triage(res, body.get("kind") or "note", body.get("title") or "", project)
    except Exception as e:
        raise HTTPException(502, f"intake error: {e}")


@app.post("/api/intake/upload")
async def intake_upload(file: UploadFile = File(...), kind: str = Form("document"),
                        title: str = Form(""), project: str = Query(store.DEFAULT_PROJECT)):
    """Drop a file — audio/video, pdf, docx, or text — extract or TRANSCRIBE it, then
    ingest into `project`'s corpus + triage. Media is transcribed via OpenAI (Whisper) through the
    gateway; everything else uses attachments.extract. Same response shape as /api/intake."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    fn = file.filename or "upload"
    label = (title or fn).strip()
    media = transcribe.is_media(fn, file.content_type)
    try:
        if media:
            text = await asyncio.to_thread(transcribe.transcribe, fn, data, file.content_type)
        else:
            text = await asyncio.to_thread(attachments.extract, fn, file.content_type, data)
    except ValueError as e:                       # size limit etc. — user-facing
        raise HTTPException(413, str(e))
    except Exception as e:
        raise HTTPException(502, f"{'transcription' if media else 'extract'} error: {e}")
    if not text or not text.strip():
        raise HTTPException(422, f"could not get text from {fn} (unsupported type or empty)")
    try:
        res = await asyncio.to_thread(intake.ingest_and_triage, kind or "document", label, text,
                                      project=project)
    except Exception as e:
        raise HTTPException(502, f"intake error: {e}")
    res["transcribed"] = media
    res["chars"] = len(text)
    return _queue_triage(res, "transcript" if media else "upload", label, project)


# ---- Live Inbox (Phase 5.5) -------------------------------------------------
@app.get("/api/inbox")
async def get_inbox(status: str = None, project: str = Query(store.DEFAULT_PROJECT)):
    return {"items": store.list_inbox(status, project=project),
            "pending": store.inbox_pending_count(project=project)}


@app.post("/api/inbox/{item_id}/confirm")
async def confirm_inbox(item_id: int, body: dict = Body(default={}),
                        project: str = Query(store.DEFAULT_PROJECT)):
    """Apply the given proposals/new_tasks (default: all of the item's). `keep_proposals` /
    `keep_new_tasks` are held back and the item STAYS pending with just those (used to bulk-
    confirm the safe changes while holding status->Done items that still need evidence).
    Edited proposals are honored — the client sends the modified field values to apply."""
    item = store.get_inbox_item(item_id, project=project)
    if not item:
        raise HTTPException(404, "no such inbox item")
    tri = item.get("triage") or {}
    applied = inbox_mod.apply(body.get("proposals", tri.get("proposals", [])),
                              body.get("new_tasks", tri.get("new_tasks", [])), project=project)
    keep_p = body.get("keep_proposals") or []
    keep_n = body.get("keep_new_tasks") or []
    tri["applied"] = applied
    if keep_p or keep_n:
        tri["proposals"], tri["new_tasks"] = keep_p, keep_n
        store.update_inbox_triage(item_id, tri, project=project)   # stays pending with the held items
    else:
        store.update_inbox_triage(item_id, tri, project=project)
        store.set_inbox_status(item_id, "confirmed", project=project)
    return {"applied": applied, "remaining": len(keep_p) + len(keep_n)}


@app.post("/api/inbox/confirm_all")
async def confirm_all_inbox(body: dict = Body(default={}), project: str = Query(store.DEFAULT_PROJECT)):
    """Bulk-confirm pending queue items. safe_only=True applies everything EXCEPT status->Done
    proposals (which need acceptance evidence), holding those back so the item stays pending."""
    safe_only = bool(body.get("safe_only"))
    ids = body.get("ids")
    items = store.list_inbox("pending", limit=500, project=project)
    if ids:
        idset = set(ids)
        items = [it for it in items if it["id"] in idset]
    tot = {"items": 0, "updated": 0, "created": 0, "held": 0}
    for it in items:
        tri = it.get("triage") or {}
        props = tri.get("proposals", []) or []
        nts = tri.get("new_tasks", []) or []
        if safe_only:
            apply_p = [p for p in props if (p.get("status") or "") != "Done"]
            keep_p = [p for p in props if (p.get("status") or "") == "Done"]
        else:
            apply_p, keep_p = props, []
        if not (apply_p or nts):
            continue
        applied = inbox_mod.apply(apply_p, nts, project=project)
        tri["applied"] = applied
        tot["items"] += 1
        tot["updated"] += len(applied.get("updated", []))
        tot["created"] += len(applied.get("created", []))
        tot["held"] += len(keep_p)
        if keep_p:
            tri["proposals"], tri["new_tasks"] = keep_p, []
            store.update_inbox_triage(it["id"], tri, project=project)
        else:
            store.update_inbox_triage(it["id"], tri, project=project)
            store.set_inbox_status(it["id"], "confirmed", project=project)
    return tot


@app.post("/api/inbox/{item_id}/dismiss")
async def dismiss_inbox(item_id: int, project: str = Query(store.DEFAULT_PROJECT)):
    if not store.get_inbox_item(item_id, project=project):
        raise HTTPException(404, "no such inbox item")
    store.set_inbox_status(item_id, "dismissed", project=project)
    return {"dismissed": item_id}


@app.post("/api/inbox/simulate")
async def simulate_inbox(body: dict = Body(...), project: str = Query(store.DEFAULT_PROJECT)):
    """Inject a fake inbound email to exercise the Live Inbox pipeline without a mailbox. Routes
    to `project` (query param, or a `project` field in the body for explicit cross-board testing)."""
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    project = body.get("project") or project
    sender = body.get("sender") or "tester@taikunai.com"
    headers = {"from": sender, "to": body.get("to") or "", "cc": body.get("cc") or "",
               "date": body.get("date") or "", "message_id": body.get("message_id") or ""}
    try:
        item = await asyncio.to_thread(
            inbox_mod.process, "email-sim", "sim-" + os.urandom(6).hex(),
            sender, body.get("subject") or "(simulated)", text, headers, project)
    except Exception as e:
        raise HTTPException(502, f"inbox error: {e}")
    return item or {"deduped": True}


@app.post("/api/inbox/poll")
async def poll_inbox_now():
    import gmail_source
    return await asyncio.to_thread(gmail_source.poll)


@app.get("/api/signals")
def plan_signals(project: str = Query(store.DEFAULT_PROJECT)):
    """Derived plan health: overdue / due-soon / blocked / ready / critical-slip /
    past-due decisions + each owner's next-best 1-2 tasks.

    Sync (def, not async) on purpose: compute_plan_signals walks the full enriched
    task list synchronously, so FastAPI runs it in the threadpool where its SQLite
    I/O can't block the single worker's event loop — other requests (incl. /health)
    stay responsive (HARDEN-36). The store also serves a short-TTL cached copy."""
    return signals.compute_plan_signals(project=_proj(project))


@app.post("/api/digest")
async def make_digest():
    """Generate + post the weekly chief-of-staff brief (signals + activity deltas)."""
    try:
        return await asyncio.to_thread(digest.generate_digest)
    except Exception as e:
        raise HTTPException(502, f"digest error: {e}")


@app.get("/api/digests")
async def get_digests():
    return {"digests": store.list_digests(20)}


@app.get("/api/notify/status")
async def notify_status():
    """Which channels are wired (configured) vs dry-run."""
    return notify.status()


@app.post("/api/notify/test")
async def notify_test():
    return {"results": notify.send("Project Maxwell — test", "Notify is wired (test message from plan.taikunai.com).")}


@app.post("/api/digest/{digest_id}/send")
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


def _people_of(t, people):
    """Owner-person(s) for a task — match the people list against owner_person_or_role.
    Mirrors the board UI's _peopleOf so 'export = what you see' for the owner filter."""
    owner = (t.get("owner_person_or_role") or "").lower()
    if not owner:
        return ["Unassigned"]
    m = [p for p in people if p.lower() in owner]
    return m or ["Unassigned"]


def _filtered_payload(workstream=None, owner=None, risk=None, blocking=0, q=None, person=None,
                      project="maxwell"):
    """Same filter semantics as the board UI, so 'export = what you see'."""
    p = store.board_payload(_proj(project))
    ql = (q or "").lower()
    people = store.get_meta("people", store.DEFAULT_PEOPLE, project=project) if person else []

    def keep(t):
        if workstream and t.get("_wsId") != workstream:
            return False
        if owner and t.get("owner_org") != owner:
            return False
        if person and person not in _people_of(t, people):
            return False
        if risk and t.get("risk_level") != risk:
            return False
        if blocking and not t.get("is_blocking"):
            return False
        if ql:
            hay = f"{t.get('task_id','')} {t.get('title','')} {t.get('description','')} {t.get('owner_person_or_role','')} {t.get('_wsName','')}".lower()
            if ql not in hay:
                return False
        return True

    p["workstreams"] = [{**w, "tasks": [t for t in w["tasks"] if keep(t)]} for w in p["workstreams"]]
    p["workstreams"] = [w for w in p["workstreams"] if w["tasks"]]
    return p


@app.get("/api/export.xlsx")
async def export_xlsx(workstream: str = None, owner: str = None, risk: str = None, blocking: int = 0, q: str = None, person: str = None, project: str = Query(store.DEFAULT_PROJECT)):
    data = export.export_xlsx(_filtered_payload(workstream, owner, risk, blocking, q, person, _proj(project)))
    return Response(content=data,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="project-plan.xlsx"'})


@app.get("/api/export.xml")
async def export_xml(workstream: str = None, owner: str = None, risk: str = None, blocking: int = 0, q: str = None, person: str = None, project: str = Query(store.DEFAULT_PROJECT)):
    xml = export.export_mspdi(_filtered_payload(workstream, owner, risk, blocking, q, person, _proj(project)))
    return Response(content=xml, media_type="text/xml",
                    headers={"Content-Disposition": 'attachment; filename="project-plan.xml"'})


@app.get("/api/audit/export")
async def audit_export(request: Request, project: str = Query(store.DEFAULT_PROJECT)):
    project = _proj(project)
    _principal(request, project, ("write:system",), dev_actor="auditor")
    data = store.audit_export(project=project)
    return JSONResponse(
        data,
        headers={"Content-Disposition": f'attachment; filename="{project}-audit-export.json"'},
    )


@app.get("/api/cleanup/candidates")
async def cleanup_candidates(request: Request, project: str = Query(store.DEFAULT_PROJECT),
                             kinds: str = "", proof_task_age_days: float = 14):
    project = _proj(project)
    _principal(request, project, ("write:system",), dev_actor="switchboard/operator")
    data = store.cleanup_candidates(
        project=project,
        proof_task_age_days=proof_task_age_days,
        include_kinds=store.coerce_csv_list(kinds),
    )
    if data.get("error"):
        raise HTTPException(400, data)
    return data


@app.post("/api/cleanup/apply")
async def apply_cleanup(request: Request, body: dict = Body(default={})):
    body = body or {}
    project = _body_project(body)
    principal = _principal(request, project, ("write:system",), dev_actor="switchboard/operator")
    result = store.apply_cleanup(
        project=project,
        candidate_ids=store.coerce_csv_list(body.get("candidate_ids") or body.get("ids") or []),
        dry_run=body.get("dry_run") is not False,
        actor=auth.actor(principal),
        reason=body.get("reason") or "operator lifecycle cleanup",
        proof_task_age_days=float(body.get("proof_task_age_days") or 14),
        include_kinds=store.coerce_csv_list(body.get("kinds") or body.get("include_kinds") or []),
    )
    if result.get("error"):
        raise HTTPException(400, result)
    return result


# ---- Deck rebrand (one-stop: drop a .pptx, get it back on-brand) ------------
_REBRAND_MAX = 80 * 1024 * 1024  # 80 MB — protects the small VM

@app.post("/api/rebrand")
async def rebrand_deck(file: UploadFile = File(...)):
    """Upload a .pptx -> download it re-skinned into the Taikun brand. Lossless
    (media/charts/embeds preserved); runs the in-process rebrand.rebrand_bytes."""
    name = file.filename or "deck.pptx"
    if not name.lower().endswith(".pptx"):
        raise HTTPException(400, "Please upload a PowerPoint .pptx file.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file was empty.")
    if len(data) > _REBRAND_MAX:
        raise HTTPException(413, f"File too large (max {_REBRAND_MAX // (1024*1024)} MB).")
    try:
        out = await asyncio.to_thread(rebrand.rebrand_bytes, data)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Rebrand failed: {e}")
    base = name[:-5] if name.lower().endswith(".pptx") else name
    dl = f"{base}-Taikun.pptx"
    return Response(content=out,
                    media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    headers={"Content-Disposition": f'attachment; filename="{dl}"'})


# ---- PDF OCR (one-stop: drop a scanned PDF, get it back searchable) ---------
_OCR_MAX = 40 * 1024 * 1024  # 40 MB — protects the small VM

@app.post("/api/ocr")
async def ocr_pdf(file: UploadFile = File(...)):
    """Upload a scanned/printed .pdf -> download a searchable PDF: the original
    pages are kept pixel-for-pixel and an AI-OCR'd invisible text layer is embedded
    over them. Renders pages -> gateway vision model -> embed, in ocr.ocr_pdf_bytes."""
    name = file.filename or "document.pdf"
    if not ocr.is_pdf(name, file.content_type):
        raise HTTPException(400, "Please upload a PDF file.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file was empty.")
    if len(data) > _OCR_MAX:
        raise HTTPException(413, f"File too large (max {_OCR_MAX // (1024*1024)} MB).")
    try:
        out, _text = await asyncio.to_thread(ocr.ocr_pdf_bytes, data)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(502, f"OCR failed: {e}")
    base = name[:-4] if name.lower().endswith(".pdf") else name
    dl = f"{base}-searchable.pdf"
    return Response(content=out, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{dl}"'})


# ---- Switchboard runtime protocol (IXP core + first TXP/OXP slices) ---------

def _body_project(body: dict) -> str:
    return _proj((body or {}).get("project") or store.DEFAULT_PROJECT)


@app.post("/ixp/v1/register_agent")
async def ixp_register_agent(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "agent")
    agent_id = (body.get("agent_id") or "").strip()
    runtime = (body.get("runtime") or "").strip()
    if not agent_id or not runtime:
        raise HTTPException(400, "agent_id and runtime required")
    return store.register_agent(
        agent_id=agent_id, runtime=runtime, model=body.get("model") or "",
        lane=body.get("lane") or "", task_id=body.get("task") or body.get("task_id") or "",
        ttl_s=int(body.get("ttl_s") or 120), control=body.get("control") or {},
        protocol=body.get("protocol") or {},
        principal_id=principal["id"], actor=auth.actor(principal), project=project)


@app.post("/ixp/v1/heartbeat")
async def ixp_heartbeat(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "agent")
    return store.heartbeat((body.get("agent_id") or "").strip(),
                           actor=auth.actor(principal), project=project)


@app.get("/ixp/v1/agents")
async def ixp_agents(project: str = Query(store.DEFAULT_PROJECT), lane: str = ""):
    return {"agents": store.list_active_agents(lane=lane, project=_proj(project))}


@app.get("/coordination", include_in_schema=False)
async def coordination_page():
    """Standalone, read-only Agent Coordination view (the agent-to-agent war room).
    Unlinked from the board nav on purpose; reachable by URL. Data comes from
    /api/coordination, which is gated by the normal read auth."""
    page = _static / "coordination.html"
    if page.exists():
        return _shell_response(page)
    raise HTTPException(404, "coordination page not found")


@app.get("/api/coordination")
async def api_coordination(project: str = Query(store.DEFAULT_PROJECT), limit: int = 500):
    """Read-only rollup for the Agent Coordination page: presence, the full directed
    message bus, and the decision log — one project's live coordination record."""
    proj = _proj(project)
    return {
        "project": proj,
        "agents": store.list_active_agents(project=proj),
        "messages": store.list_agent_messages(project=proj, limit=limit),
        "decisions": store.list_decisions(project=proj),
    }


# ---- UI-7: operator-facing directed messaging + ack inbox ----
# The /ixp/v1/* message bus authenticates agents by write:ixp bearer; these /api/*
# twins let a browser operator steer a live agent from a task's chip (send, with an
# optional required ack) and watch the ack land — using their normal session scopes.

@app.post("/api/agent_messages/send")
async def api_send_agent_message(request: Request, body: dict = Body(...)):
    """Operator → live agent nudge/redirect. from_agent is the operator's own identity so
    the ack inbox can find it again; requires_ack + a deadline arm a durable monitor.
    Pass ?project= in the query so the auth middleware scopes to the right board."""
    project = _proj(request.query_params.get("project") or body.get("project") or store.DEFAULT_PROJECT)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    to_agent = (body.get("to_agent") or body.get("to") or "").strip()
    if not to_agent:
        raise HTTPException(400, "to_agent is required")
    if not (body.get("message") or "").strip():
        raise HTTPException(400, "message is required")
    deadline = body.get("ack_deadline_minutes")
    return store.send_agent_message(
        from_agent=auth.actor(principal),
        to_agent=to_agent,
        message=body.get("message"),
        task_id=(body.get("task_id") or body.get("task") or None),
        requires_ack=bool(body.get("requires_ack")),
        ack_deadline_minutes=(int(deadline) if deadline not in (None, "", 0, "0") else None),
        priority=int(body.get("priority") or 0),
        principal_id=principal["id"],
        idem_key=body.get("idem_key") or "",
        project=project)


@app.get("/api/agent_messages/pending")
async def api_pending_acks(request: Request, project: str = Query(store.DEFAULT_PROJECT),
                           agent_id: str = ""):
    """The operator's ack inbox: required messages they are party to that are still
    unacked (defaults to the caller's own identity so it survives a reload)."""
    proj = _proj(project)
    principal = _principal(request, proj, ("read",), dev_actor="web")
    return {"project": proj,
            "pending_acks": store.list_pending_acks(
                agent_id=(agent_id or auth.actor(principal)), project=proj)}


@app.get("/api/agent_messages/{message_id}/status")
async def api_message_status(request: Request, message_id: int,
                             project: str = Query(store.DEFAULT_PROJECT)):
    """Poll one message to see whether the recipient has acked it (and delivery state)."""
    proj = _proj(project)
    _principal(request, proj, ("read",), dev_actor="web")
    msg = store.get_message_status(message_id, project=proj)
    if not msg:
        raise HTTPException(404, "message not found")
    return msg


@app.post("/api/agent_messages/ack")
async def api_ack_message(request: Request, body: dict = Body(...)):
    """Operator acks/dismisses a required message on the recipient's behalf.
    Pass ?project= in the query so the auth middleware scopes to the right board."""
    project = _proj(request.query_params.get("project") or body.get("project") or store.DEFAULT_PROJECT)
    principal = _principal(request, project, ("write:tasks",), dev_actor="web")
    mid = body.get("message_id") if body.get("message_id") is not None else body.get("id")
    if mid is None:
        raise HTTPException(400, "message_id is required")
    return store.ack_message(int(mid), response=body.get("response") or "",
                             actor=auth.actor(principal), project=project)


@app.post("/ixp/v1/register_host")
async def ixp_register_host(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or "agent-host")
    return _control_plane_http(store.register_host(
        body, principal_id=principal["id"], actor=auth.actor(principal), project=project))


@app.post("/ixp/v1/heartbeat_host")
async def ixp_heartbeat_host(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or "agent-host")
    return _control_plane_http(store.heartbeat_host(
        (body.get("host_id") or "").strip(),
        active_sessions=body.get("active_sessions"),
        capacity=body.get("capacity") or {},
        status=body.get("status") or "online",
        last_error=body.get("last_error") or "",
        actor=auth.actor(principal), project=project))


@app.get("/ixp/v1/agent_hosts")
async def ixp_agent_hosts(project: str = Query(store.DEFAULT_PROJECT), runtime: str = "",
                          lane: str = "", capability: str = "",
                          include_stale: bool = False):
    hosts = store.list_agent_hosts(runtime=runtime, lane=lane,
                                   capability=capability,
                                   include_stale=include_stale,
                                   project=_proj(project))
    _control_plane_http(hosts)
    return {"hosts": hosts}


@app.get("/ixp/v1/control_plane_probe")
async def ixp_control_plane_probe(project: str = Query(store.DEFAULT_PROJECT), lane: str = "",
                                  include_heavy: bool = False):
    return store.control_plane_probe(project=_proj(project), lane=lane, include_heavy=include_heavy)


@app.get("/ixp/v1/saturation_signals")
def ixp_saturation_signals(project: str = Query(store.DEFAULT_PROJECT)):
    """REST parity for PERF-7 saturation dashboard (PSI + lock-wait + inbox + SLOs)."""
    return _saturation_snapshot(project)


@app.get("/ixp/v1/host_status")
async def ixp_host_status(host_id: str, project: str = Query(store.DEFAULT_PROJECT)):
    status = _control_plane_http(store.host_status(host_id, project=_proj(project)))
    if status.get("error"):
        raise HTTPException(404, status["error"])
    return status


@app.get("/ixp/v1/runner_sessions")
async def ixp_runner_sessions(project: str = Query(store.DEFAULT_PROJECT),
                              host_id: str = "", runtime: str = "",
                              task_id: str = "", status: str = "",
                              include_stale: bool = False):
    return {"sessions": store.list_runner_sessions(
        host_id=host_id, runtime=runtime, task_id=task_id, status=status,
        include_stale=include_stale, project=_proj(project))}


@app.post("/ixp/v1/register_runner_session")
async def ixp_register_runner_session(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or body.get("agent_id") or "runner")
    record = dict(body)
    record.pop("project", None)
    return store.upsert_runner_session(
        record, principal_id=principal["id"], actor=auth.actor(principal), project=project)


@app.get("/ixp/v1/work_sessions")
async def ixp_work_sessions(project: str = Query(store.DEFAULT_PROJECT),
                            task_id: str = "", agent_id: str = "", status: str = "",
                            repo_role: str = "", include_expired: bool = True):
    project = _proj(project)
    return {
        "project": project,
        "contract": store.work_session_contract(project),
        "work_sessions": store.list_work_sessions(
            project=project, task_id=task_id, agent_id=agent_id, status=status,
            repo_role=repo_role, include_expired=include_expired),
    }


@app.post("/ixp/v1/work_sessions")
async def ixp_create_work_session(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "work-session")
    payload = dict(body or {})
    payload.pop("project", None)
    result = store.create_work_session(
        payload, actor=auth.actor(principal), principal_id=principal["id"], project=project)
    if result.get("error"):
        raise HTTPException(400, result)
    return result


@app.post("/ixp/v1/managed_work_sessions")
async def ixp_create_managed_work_session(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "managed-work-session")
    payload = dict(body or {})
    payload.pop("project", None)
    result = store.create_managed_work_session(
        payload, actor=auth.actor(principal), principal_id=principal["id"], project=project)
    if result.get("error"):
        raise HTTPException(400, result)
    return result


@app.get("/ixp/v1/work_sessions/{work_session_id}")
async def ixp_get_work_session(work_session_id: str,
                               project: str = Query(store.DEFAULT_PROJECT)):
    session = store.get_work_session(work_session_id, project=_proj(project))
    if not session:
        raise HTTPException(404, "work_session_not_found")
    return session


@app.get("/ixp/v1/work_sessions/{work_session_id}/health")
async def ixp_get_work_session_health(work_session_id: str,
                                      project: str = Query(store.DEFAULT_PROJECT)):
    health = store.get_work_session_health(work_session_id, project=_proj(project))
    if not health:
        raise HTTPException(404, "work_session_not_found")
    return health


@app.get("/ixp/v1/session_health")
async def ixp_session_health(project: str = Query(store.DEFAULT_PROJECT),
                             task_id: str = "", agent_id: str = "",
                             status: str = "", only_unsafe: bool = False):
    return store.list_session_health(
        project=_proj(project), task_id=task_id, agent_id=agent_id,
        status=status, only_unsafe=only_unsafe)


@app.patch("/ixp/v1/work_sessions/{work_session_id}")
async def ixp_update_work_session(work_session_id: str, request: Request,
                                  body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "work-session")
    payload = dict(body or {})
    payload.pop("project", None)
    result = store.update_work_session(
        work_session_id, payload, actor=auth.actor(principal), project=project)
    if result.get("error"):
        status = 404 if result.get("error") == "work_session_not_found" else 400
        raise HTTPException(status, result)
    return result


@app.post("/ixp/v1/work_sessions/{work_session_id}/archive_workspace")
async def ixp_archive_work_session_workspace(work_session_id: str, request: Request,
                                             body: dict = Body(default={})):
    body = body or {}
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "managed-work-session")
    result = store.archive_work_session_workspace(
        work_session_id,
        remove_workspace=bool(body.get("remove_workspace", False)),
        actor=auth.actor(principal),
        project=project,
    )
    if result.get("error"):
        status = 404 if result.get("error") == "work_session_not_found" else 400
        raise HTTPException(status, result)
    return result


@app.post("/ixp/v1/repo_preflight")
async def ixp_repo_preflight(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    _principal(request, project, ("write:ixp",),
               dev_actor=body.get("agent_id") or "repo-preflight")
    result = store.repo_preflight(
        worktree_path=body.get("worktree_path") or body.get("path") or "",
        project=project,
        task_id=body.get("task_id") or body.get("task") or "",
        agent_id=body.get("agent_id") or "",
        repo_role=body.get("repo_role") or "canonical",
        expected_branch=body.get("expected_branch") or "",
        expected_base_ref=body.get("expected_base_ref") or "",
        scan_conflicts=bool(body.get("scan_conflicts", True)),
    )
    if result.get("verdict") == "deny":
        return result
    return result


@app.post("/ixp/v1/work_sessions/{work_session_id}/preflight")
async def ixp_preflight_work_session(work_session_id: str, request: Request,
                                     body: dict = Body(default={})):
    body = body or {}
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "work-session")
    result = store.preflight_work_session(
        work_session_id, actor=auth.actor(principal), project=project,
        expected_branch=body.get("expected_branch") or "",
        expected_base_ref=body.get("expected_base_ref") or "")
    if result.get("error"):
        status = 404 if result.get("error") == "work_session_not_found" else 400
        raise HTTPException(status, result)
    return result


@app.post("/ixp/v1/pre_tool_check")
async def ixp_pre_tool_check(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "pre-tool")
    payload = dict(body or {})
    payload.pop("project", None)
    return store.pre_tool_check(
        payload, actor=auth.actor(principal), principal_id=principal["id"],
        project=project)


@app.post("/ixp/v1/heartbeat_runner_session")
async def ixp_heartbeat_runner_session(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or body.get("agent_id") or "runner")
    record = dict(body)
    record.pop("project", None)
    return store.upsert_runner_session(
        record, principal_id=principal["id"], actor=auth.actor(principal), project=project)


@app.post("/ixp/v1/request_runner_snapshot")
async def ixp_request_runner_snapshot(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/operator")
    result = store.request_runner_control(
        body.get("runner_session_id") or body.get("id") or "",
        "snapshot",
        reason=body.get("reason") or "",
        options=body.get("options") or {},
        actor=auth.actor(principal), principal_id=principal["id"], project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/request_runner_kill")
async def ixp_request_runner_kill(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/operator")
    result = store.request_runner_control(
        body.get("runner_session_id") or body.get("id") or "",
        "kill",
        reason=body.get("reason") or "",
        options={"grace_seconds": body.get("grace_seconds"),
                 "signal": body.get("signal") or "TERM"},
        actor=auth.actor(principal), principal_id=principal["id"], project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/request_runner_restart")
async def ixp_request_runner_restart(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/operator")
    result = store.request_runner_control(
        body.get("runner_session_id") or body.get("id") or "",
        "restart",
        reason=body.get("reason") or "",
        options=body.get("options") or {},
        actor=auth.actor(principal), principal_id=principal["id"], project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/request_runner_health")
async def ixp_request_runner_health(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/operator")
    result = store.request_runner_control(
        body.get("runner_session_id") or body.get("id") or "",
        "health",
        reason=body.get("reason") or "",
        options=body.get("options") or {},
        actor=auth.actor(principal), principal_id=principal["id"], project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/request_runner_logs")
async def ixp_request_runner_logs(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/operator")
    result = store.request_runner_control(
        body.get("runner_session_id") or body.get("id") or "",
        "logs",
        reason=body.get("reason") or "",
        options=body.get("options") or {},
        actor=auth.actor(principal), principal_id=principal["id"], project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/request_runner_open")
async def ixp_request_runner_open(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/operator")
    result = store.request_runner_control(
        body.get("runner_session_id") or body.get("id") or "",
        "open",
        reason=body.get("reason") or "",
        options=body.get("options") or {},
        actor=auth.actor(principal), principal_id=principal["id"], project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.get("/ixp/v1/runner_controls")
async def ixp_runner_controls(project: str = Query(store.DEFAULT_PROJECT),
                              status: str = "", host_id: str = "",
                              runner_session_id: str = ""):
    return {"requests": store.list_runner_control_requests(
        status=status, host_id=host_id, runner_session_id=runner_session_id,
        project=_proj(project))}


@app.get("/ixp/v1/external_effects")
async def ixp_external_effects(project: str = Query(store.DEFAULT_PROJECT),
                               effect_type: str = "", status: str = "",
                               task_id: str = "", target: str = ""):
    return {"effects": store.list_external_effects(
        effect_type=effect_type, status=status, task_id=task_id,
        target=target, project=_proj(project))}


@app.get("/ixp/v1/external_ci_runs")
async def ixp_external_ci_runs(project: str = Query(store.DEFAULT_PROJECT),
                               task_id: str = "", source_project: str = "",
                               source_sha: str = "", status: str = ""):
    return {"runs": store.list_external_ci_runs(
        task_id=task_id, source_project=source_project,
        source_sha=source_sha, status=status, project=_proj(project))}


@app.get("/ixp/v1/external_ci_runs/{run_id}")
async def ixp_external_ci_run(run_id: str, project: str = Query(store.DEFAULT_PROJECT)):
    run = store.get_external_ci_run(run_id, project=_proj(project))
    if not run:
        raise HTTPException(404, "external_ci_run not found")
    return run


@app.get("/ixp/v1/publication_evidence")
async def ixp_publication_evidence(project: str = Query(store.DEFAULT_PROJECT),
                                   task_id: str = "", source_project: str = "",
                                   source_sha: str = "", public_repo: str = ""):
    return {"publication_evidence": store.list_publication_evidence(
        task_id=task_id, source_project=source_project,
        source_sha=source_sha, public_repo=public_repo, project=_proj(project))}


@app.post("/ixp/v1/publication_evidence")
async def ixp_record_publication_evidence(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "agent")
    payload = dict(body.get("publication") or body)
    payload["principal_id"] = principal["id"]
    result = store.create_publication_evidence(
        payload, actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result)
    return result


@app.post("/ixp/v1/external_ci_mirror/request")
async def ixp_request_external_ci_mirror(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "agent")
    source_path = (body.get("source_path") or body.get("source_checkout") or "").strip()
    payload = dict(body.get("run") or body)
    payload.pop("source_path", None)
    payload.pop("source_checkout", None)
    result = external_ci_mirror.request_external_ci_mirror_run(
        payload, source_path, actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result)
    return result


@app.post("/ixp/v1/external_ci_mirror/poll")
async def ixp_poll_external_ci_mirror(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "agent")
    source_path = (body.get("source_path") or body.get("source_checkout") or "").strip()
    result = external_ci_mirror.poll_external_ci_mirror_run(
        body.get("run_id") or "", source_path, actor=auth.actor(principal), project=project,
        poll_interval_seconds=float(body.get("poll_interval_seconds") or 15),
        timeout_seconds=float(body.get("timeout_seconds") or 1800))
    if result.get("error"):
        raise HTTPException(400, result)
    return result


@app.post("/ixp/v1/merge_gate")
async def ixp_merge_gate(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "agent")
    result = store.merge_gate(
        body,
        actor=auth.actor(principal),
        principal_id=principal["id"],
        project=project,
    )
    if result.get("status") == "blocked":
        return result
    return result


@app.post("/ixp/v1/claim_external_effect")
async def ixp_claim_external_effect(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "agent")
    result = store.claim_external_effect(
        body.get("effect_type") or "",
        body.get("target") or "",
        body.get("resource") or "",
        body.get("payload") or {},
        task_id=body.get("task_id") or body.get("task"),
        claim_id=body.get("claim_id") or "",
        agent_id=body.get("agent_id") or "",
        idem_key=body.get("idem_key") or "",
        idempotency_window_seconds=int(body.get("idempotency_window_seconds") or 0),
        actor=auth.actor(principal), principal_id=principal["id"], project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/mark_external_effect_issued")
async def ixp_mark_external_effect_issued(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="agent")
    result = store.mark_external_effect_issued(
        body.get("effect_key") or body.get("id") or "",
        readback=body.get("readback") or {},
        actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/verify_external_effect")
async def ixp_verify_external_effect(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="agent")
    result = store.verify_external_effect(
        body.get("effect_key") or body.get("id") or "",
        readback=body.get("readback") or {},
        actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/fail_external_effect")
async def ixp_fail_external_effect(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="agent")
    result = store.fail_external_effect(
        body.get("effect_key") or body.get("id") or "",
        error=body.get("error") or "effect_failed",
        readback=body.get("readback") or {},
        dead_letter=bool(body.get("dead_letter")),
        actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/claim_runner_control")
async def ixp_claim_runner_control(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or "agent-host")
    result = store.claim_runner_control_request(
        (body.get("host_id") or "").strip(),
        (body.get("request_id") or body.get("id") or "").strip(),
        actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/complete_runner_control")
async def ixp_complete_runner_control(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or "agent-host")
    result = store.complete_runner_control_request(
        (body.get("request_id") or body.get("id") or "").strip(),
        result=body.get("result") or {},
        snapshot=body.get("snapshot") or {},
        status=body.get("status") or "",
        actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


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
    selector = body.get("selector") or {}
    if not isinstance(selector, dict):
        raise HTTPException(400, "selector must be an object")
    result = store.request_wake(
        selector=selector, reason=body.get("reason") or "",
        source=body.get("source") or auth.actor(principal),
        policy=body.get("policy") or {}, task_id=(body.get("task_id") or None),
        principal_id=principal["id"], actor=auth.actor(principal),
        idem_key=body.get("idem_key") or "", project=project)
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


@app.post("/ixp/v1/send")
async def ixp_send(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("from_agent") or "agent")
    return store.send_agent_message(
        from_agent=body.get("from_agent") or auth.actor(principal),
        to_agent=body.get("to_agent") or body.get("to") or "",
        message=body.get("message") or "",
        task_id=body.get("task") or body.get("task_id"),
        requires_ack=bool(body.get("requires_ack")),
        ack_deadline_minutes=body.get("ack_deadline_minutes"),
        ack_timeout_seconds=(body.get("ack_timeout_seconds")
                             if body.get("ack_timeout_seconds") is not None
                             else body.get("ack_timeout_s")),
        on_ack_timeout=(body.get("on_ack_timeout") or body.get("ack_timeout_action") or
                        "notify_sender"),
        signal=body.get("signal"), priority=int(body.get("priority") or 0),
        principal_id=principal["id"], idem_key=body.get("idem_key") or "",
        project=project)


@app.get("/ixp/v1/inbox")
async def ixp_inbox(project: str = Query(store.DEFAULT_PROJECT),
                    to_agent: str = "", unacked: bool = True, signal: str = ""):
    msgs = store.list_unacked_messages(to_agent, project=_proj(project)) if unacked else []
    if signal:
        msgs = [m for m in msgs if m.get("signal") == signal]
    return {"messages": msgs}


@app.post("/ixp/v1/ack")
async def ixp_ack(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="agent")
    return store.ack_message(int(body.get("message_id") or body.get("id")),
                             response=body.get("response") or "",
                             actor=auth.actor(principal), project=project)


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
    return store.get_working_agreement(project=_proj(project))


@app.post("/ixp/v1/bugs/submit")
async def ixp_submit_bug(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:bug_intake",),
                           dev_actor=body.get("source_agent") or "bug-intake")
    result = store.submit_bug(
        body,
        actor=auth.actor(principal),
        project=project,
    )
    if result.get("error"):
        raise HTTPException(400, result)
    return result


@app.post("/txp/v1/claim_next")
async def txp_claim_next(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor=body.get("agent_id") or "agent")
    lanes = store.coerce_csv_list(body.get("lanes"))
    if not lanes:
        lanes = store.coerce_csv_list(body.get("lane"))
    return store.claim_next(
        agent_id=body.get("agent_id") or auth.actor(principal),
        lanes=lanes,
        capabilities=store.coerce_csv_list(body.get("capabilities")),
        max_risk=body.get("max_risk") or "",
        max_budget_usd=body.get("max_budget_usd"),
        principal_id=principal["id"], actor=auth.actor(principal),
        ttl_seconds=int(body.get("ttl_s") or body.get("ttl_seconds") or 1800),
        idem_key=body.get("idem_key") or "",
        override_identity_risk=bool(body.get("override_identity_risk")),
        work_session_id=body.get("work_session_id") or "",
        work_session=body.get("work_session") or {},
        session_policy_profile=body.get("session_policy_profile") or body.get("policy_profile") or "",
        require_work_session=bool(body.get("require_work_session")),
        project=project,
        deliverable_id=body.get("deliverable_id") or "",
        board_id=body.get("board_id") or "",
        mission_id=body.get("mission_id") or "",
        milestone_id=body.get("milestone_id") or "")


@app.post("/txp/v1/claim_task")
async def txp_claim_task(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor=body.get("agent_id") or "agent")
    return store.claim_task(
        task_id=body.get("task_id") or body.get("task") or "",
        agent_id=body.get("agent_id") or auth.actor(principal),
        principal_id=principal["id"], actor=auth.actor(principal),
        ttl_seconds=int(body.get("ttl_s") or body.get("ttl_seconds") or 1800),
        idem_key=body.get("idem_key") or "",
        override_identity_risk=bool(body.get("override_identity_risk")),
        work_session_id=body.get("work_session_id") or "",
        work_session=body.get("work_session") or {},
        session_policy_profile=body.get("session_policy_profile") or body.get("policy_profile") or "",
        require_work_session=bool(body.get("require_work_session")),
        project=project)


@app.post("/txp/v1/request_wake")
async def txp_request_wake(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("source") or "agent")
    return _control_plane_http(store.request_wake(
        selector=body.get("selector") or {},
        reason=body.get("reason") or "",
        source=body.get("source") or auth.actor(principal),
        policy=body.get("policy") or {},
        task_id=body.get("task") or body.get("task_id"),
        principal_id=principal["id"], actor=auth.actor(principal),
        idem_key=body.get("idem_key") or "", project=project))


@app.get("/txp/v1/list_wake_intents")
async def txp_list_wake_intents(project: str = Query(store.DEFAULT_PROJECT),
                                status: str = "", host_id: str = "",
                                runtime: str = ""):
    wakes = store.list_wake_intents(status=status, host_id=host_id,
                                    runtime=runtime, project=_proj(project))
    _control_plane_http(wakes)
    return {"wake_intents": wakes}


@app.post("/txp/v1/claim_wake")
async def txp_claim_wake(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or "agent-host")
    return _control_plane_http(store.claim_wake(
        (body.get("host_id") or "").strip(),
        (body.get("wake_id") or body.get("id") or "").strip(),
        actor=auth.actor(principal), project=project))


@app.post("/txp/v1/complete_wake")
async def txp_complete_wake(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or body.get("agent_id") or "agent-host")
    return _control_plane_http(store.complete_wake(
        (body.get("wake_id") or body.get("id") or "").strip(),
        runner_session_id=body.get("runner_session_id") or "",
        agent_id=body.get("agent_id") or "",
        result=body.get("result") or {},
        actor=auth.actor(principal), project=project))


@app.post("/txp/v1/cancel_wake")
async def txp_cancel_wake(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="agent")
    return _control_plane_http(store.cancel_wake(
        (body.get("wake_id") or body.get("id") or "").strip(),
        reason=body.get("reason") or "cancelled",
        actor=auth.actor(principal), project=project))


@app.post("/txp/v1/complete_claim")
async def txp_complete_claim(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="agent")
    return store.complete_claim(body.get("claim_id") or "", evidence=body.get("evidence") or {},
                                final_status=body.get("final_status") or "",
                                actor=auth.actor(principal), project=project,
                                mission_project=body.get("mission_project") or "")


@app.post("/txp/v1/abandon_claim")
async def txp_abandon_claim(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="agent")
    return store.abandon_claim(body.get("claim_id") or "", reason=body.get("reason") or "unspecified",
                               actor=auth.actor(principal), project=project)


@app.post("/txp/v1/revoke_claim")
async def txp_revoke_claim(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("operator_agent") or "switchboard/operator")
    sort_order = body.get("sort_order")
    try:
        sort_order_value = int(sort_order) if sort_order not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(400, "sort_order must be an integer")
    return store.revoke_claim(
        body.get("claim_id") or "",
        reason=body.get("reason") or "operator override",
        reassign_to=body.get("reassign_to") or body.get("reassigned_to") or "",
        sort_order=sort_order_value,
        partial_evidence=body.get("partial_evidence") or body.get("evidence") or {},
        notify=body.get("notify") is not False,
        ack_deadline_minutes=float(body.get("ack_deadline_minutes") or 5),
        actor=auth.actor(principal),
        project=project,
    )


@app.post("/tally/v1/spend/ingest")
async def tally_spend_ingest(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor=body.get("agent_id") or "tally")
    return store.report_usage(
        source=body.get("source") or "agent_report",
        confidence=body.get("confidence") or "reported",
        task_id=body.get("task_id"), claim_id=body.get("claim_id"),
        outcome_id=body.get("outcome_id"), agent_id=body.get("agent_id"),
        principal_id=principal["id"], runtime=body.get("runtime") or "",
        call_site=body.get("call_site") or "", provider=body.get("provider") or "",
        model=body.get("model") or "", prompt_tokens=int(body.get("prompt_tokens") or 0),
        completion_tokens=int(body.get("completion_tokens") or 0),
        total_tokens=body.get("total_tokens"), cost_usd=float(body.get("cost_usd") or 0.0),
        latency_ms=body.get("latency_ms"), status=body.get("status") or "ok",
        metadata=body.get("metadata") or {}, request_id=body.get("request_id"),
        project=project)


@app.post("/tally/v1/outcomes")
async def tally_record_outcome(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("actor") or body.get("agent_id") or "tally")
    return store.record_outcome(
        outcome_type=body.get("type") or body.get("outcome_type") or "",
        title=body.get("title") or "",
        task_id=body.get("task_id") or body.get("task"),
        claim_id=body.get("claim_id"),
        epic_id=body.get("epic_id") or body.get("epic"),
        status=body.get("status") or "proposed",
        verifier=body.get("verifier") or "",
        verification=body.get("verification") or "",
        evidence=body.get("evidence") or {},
        value=body.get("value") or {},
        actor=auth.actor(principal),
        project=project)


@app.post("/tally/v1/outcomes/{outcome_id}/verify")
async def tally_verify_outcome(outcome_id: str, request: Request, body: dict = Body(default={})):
    project = _body_project(body or {})
    principal = _principal(request, project, ("write:ixp",), dev_actor="tally")
    return store.verify_outcome(
        outcome_id,
        verifier=body.get("verifier") or auth.actor(principal),
        verification=body.get("verification") or "",
        evidence=body.get("evidence") or {},
        actor=auth.actor(principal),
        project=project)


@app.post("/tally/v1/outcomes/{outcome_id}/reject")
async def tally_reject_outcome(outcome_id: str, request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="tally")
    return store.reject_outcome(
        outcome_id,
        verifier=body.get("verifier") or auth.actor(principal),
        reason=body.get("reason") or "rejected",
        actor=auth.actor(principal),
        project=project)


@app.post("/tally/v1/kpis")
async def tally_create_kpi(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="tally")
    return store.create_kpi(
        name=body.get("name") or "",
        unit=body.get("unit") or "",
        direction=body.get("direction") or "",
        owner=body.get("owner") or "",
        baseline_value=body.get("baseline_value"),
        current_value=body.get("current_value"),
        target_value=body.get("target_value"),
        period=body.get("period") or "",
        actor=auth.actor(principal),
        project=project)


@app.patch("/tally/v1/kpis/{kpi_id}")
async def tally_update_kpi(kpi_id: str, request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="tally")
    if body.get("current_value") is None:
        raise HTTPException(400, "current_value is required")
    return store.update_kpi_value(
        kpi_id,
        current_value=float(body.get("current_value")),
        evidence=body.get("evidence") or {},
        actor=auth.actor(principal),
        project=project)


@app.post("/tally/v1/outcome_kpi_links")
async def tally_link_outcome_kpi(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="tally")
    return store.link_outcome_to_kpi(
        outcome_id=body.get("outcome_id") or "",
        kpi_id=body.get("kpi_id") or "",
        contribution=body.get("contribution"),
        contribution_unit=body.get("contribution_unit") or "",
        confidence=body.get("confidence") or "directional",
        rationale=body.get("rationale") or "",
        actor=auth.actor(principal),
        project=project)


@app.get("/tally/v1/kpis")
async def tally_list_kpis(project: str = Query(store.DEFAULT_PROJECT)):
    return {"kpis": store.list_kpis(project=_proj(project))}


@app.get("/tally/v1/outcomes")
async def tally_list_outcomes(project: str = Query(store.DEFAULT_PROJECT),
                              status: str = Query(""), limit: int = Query(200)):
    return {"outcomes": store.list_outcomes(project=_proj(project),
                                            status=status, limit=limit)}


@app.get("/tally/v1/task/{task_id}")
async def tally_task(task_id: str, project: str = Query(store.DEFAULT_PROJECT)):
    return store.task_tally(task_id, project=_proj(project))


@app.get("/tally/v1/kpi/{kpi_id}")
async def tally_kpi(kpi_id: str, project: str = Query(store.DEFAULT_PROJECT)):
    return store.kpi_tally(kpi_id, project=_proj(project))


@app.get("/tally/v1/project")
async def tally_project(project: str = Query(store.DEFAULT_PROJECT)):
    return store.project_tally(project=_proj(project))


@app.get("/tally/v1/deliverable/{deliverable_id}")
async def tally_deliverable(deliverable_id: str, project: str = Query(store.DEFAULT_PROJECT)):
    result = store.deliverable_tally(deliverable_id, project=_proj(project))
    if result.get("error"):
        raise HTTPException(404, result["error"])
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


# ---- GitHub webhook — §1.2 board↔git auto-sync + §1.3 "main moved" notify ----
# Configure in GitHub → repo Settings → Webhooks:
#   Payload URL: https://<your-host>/api/github/webhook
#   Content type: application/json
#   Secret: match PM_GITHUB_WEBHOOK_SECRET in .env
#   Events: push + pull_request (merged)
#
# Behaviour:
#   push to main/master   → find active leases on changed files, send directed IM
#                           to each lease holder. Does NOT mark tasks Done.
#   PR opened/synced      → record PR provenance + move branch/title/closing-referenced tasks
#                           to In Review; update head SHA after branch pushes. Broad body
#                           mentions are ignored.
#   PR merged             → stamp merged_sha + mark branch/title/closing-referenced tasks Done.

_GH_SECRET = os.environ.get("PM_GITHUB_WEBHOOK_SECRET", "")


def _verify_gh_signature(body: bytes, sig_header: str) -> bool:
    """HMAC-SHA256 signature check — skip if no secret configured (dev mode)."""
    if not _GH_SECRET:
        return True
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = hmac.new(_GH_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", sig_header)


async def _handle_push(payload: dict, project: str):
    return await asyncio.to_thread(github_sync.handle_push, payload, project)


async def _handle_pr(payload: dict, project: str):
    return await asyncio.to_thread(github_sync.handle_pr, payload, project)


async def _drain_webhook_inbox_bg(project: str):
    """Best-effort drain kicked off the request path. Failures are non-fatal: the
    event is already durable in the inbox, and the backstop drain job / reconcile
    will apply it on the next pass."""
    try:
        await asyncio.to_thread(webhook_inbox.drain, project)
    except Exception:
        pass


@app.post("/api/github/webhook")
async def github_webhook(request: Request, project: str = ""):
    """Receive GitHub push/pull_request events (PERF-1: accept-and-ack, never drop).

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
    _proj(project)  # fail-closed on unknown project — a misroute is not a drop class

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


@app.post("/api/github/webhook/drain")
async def github_webhook_drain(project: str = Query(...), limit: int = 200):
    """Operator/backstop: apply pending inbox rows now. Idempotent."""
    _proj(project)
    return JSONResponse(await asyncio.to_thread(
        webhook_inbox.drain, project, limit=limit))


@app.get("/api/github/webhook/inbox")
async def github_webhook_inbox_depth(project: str = Query(...)):
    """Observable inbox depth: counts by status + oldest-pending age."""
    _proj(project)
    return JSONResponse(await asyncio.to_thread(webhook_inbox.inbox_depth, project))


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
