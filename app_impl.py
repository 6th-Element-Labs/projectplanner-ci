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
import hashlib
import os
from pathlib import Path

# Load a local .env if present (SMTP/gateway config for later slices). No core import.
_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

import auth  # noqa: E402
import request_observability  # noqa: E402
import saturation_signals  # noqa: E402
import store  # noqa: E402
app = FastAPI(title="Taikun PM", version="0.1.0")
_req_obs = request_observability.RequestObservability()

# Auth HTTP routes: production monolith sets PM_AUTH_HTTP_PRIMARY=service so the
# edge-owned Auth process (:8121) is the sole HTTP surface (ARCH-MS-77). Hermetic
# TestClient suites leave the env unset and mount the *same* shared router
# in-process — not a second implementation.
import scripts.switchboard_path  # noqa: E402,F401
from switchboard.api import deps  # noqa: E402
from switchboard.api.auth_port_adapters import configure_auth_ports  # noqa: E402
from switchboard.api.tasks_port_adapters import configure_tasks_ports  # noqa: E402
from switchboard.api.middleware import _write_required_scopes  # noqa: E402,F401
from switchboard.api.middleware import register_middleware  # noqa: E402
from switchboard.api.routers.auth import service as _auth_service, session as _auth_session, store as _auth_store  # noqa: E402
from switchboard.api.routers.auth.routes import router as _global_auth_router  # noqa: E402
from switchboard.api.routers.auth.routes import create_me_router as _create_me_router  # noqa: E402
from switchboard.api.routers.plan_chat import create_router as _create_plan_chat_router  # noqa: E402
from switchboard.api.routers.projects import create_router as _create_project_router  # noqa: E402
from switchboard.api.routers.provider_credentials import create_router as _create_provider_credentials_router  # noqa: E402
from switchboard.api.routers.tasks import create_router as _create_task_router  # noqa: E402
from switchboard.api.routers.claims import create_router as _create_claims_router  # noqa: E402
from switchboard.api.routers.wakes import create_router as _create_wakes_router  # noqa: E402
from switchboard.api.routers.agents import create_router as _create_agents_router  # noqa: E402
from switchboard.api.routers.messaging import create_router as _create_messaging_router  # noqa: E402
from switchboard.api.routers.access import create_router as _create_access_router  # noqa: E402
from switchboard.api.routers.board import create_router as _create_board_router  # noqa: E402
from switchboard.api.routers.resource_leases import create_router as _create_resource_leases_router  # noqa: E402
from switchboard.api.routers.monitors import create_router as _create_monitors_router  # noqa: E402
from switchboard.api.routers.health import create_router as _create_health_router  # noqa: E402
from switchboard.api.routers.tally import create_router as _create_tally_router  # noqa: E402
from switchboard.api.routers.ixp_work_sessions import create_router as _create_ixp_work_sessions_router  # noqa: E402
from switchboard.api.routers.runner import create_router as _create_runner_router  # noqa: E402
from switchboard.api.routers.runner_pty import create_router as _create_runner_pty_router  # noqa: E402
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
from switchboard.api.routers.spa import _asset_version  # noqa: E402,F401
from switchboard.api.routers.spa import register_spa  # noqa: E402
from switchboard.domain.projects import ProjectLifecycleWriteBlocked  # noqa: E402

configure_auth_ports()
configure_tasks_ports()
_auth_store.init()
# ARCH-MS-77: when production sets PM_AUTH_HTTP_PRIMARY=service, do not dual-mount
# Auth HTTP (Caddy → :8121). Unset/empty keeps the shared router for hermetic tests.
if (os.environ.get("PM_AUTH_HTTP_PRIMARY") or "").strip().lower() != "service":
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


ADMIN_SCOPES = deps.ADMIN_SCOPES


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

# Shared trust-boundary helpers (src/switchboard/api/deps.py) — thin wrappers kept
# at call sites below so router include_router(...) calls stay readable, and so
# source-grep proof tests (and test_access_private_projects.py's direct attribute
# access to the ACCESS-15 web gate helpers) keep finding these names on the
# composition root.
def _proj(project: str) -> str:
    return deps.resolve_project(project)


def _principal(request: Request, project: str, scopes=("write:ixp",), dev_actor: str = "web") -> dict:
    return deps.resolve_principal(request, project, scopes, dev_actor=dev_actor)


def _body_project(body: dict) -> str:
    return deps.resolve_body_project(body)


def _control_plane_http(result):
    return deps.control_plane_http(result)


def _etag_json(request: Request, payload, *, max_age: int):
    return deps.etag_json(request, payload, max_age=max_age)


_global_user_scopes = deps.global_user_scopes
_global_principal = deps.global_principal


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


app.include_router(_create_me_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    global_user_scopes=deps.global_user_scopes,
    global_principal=deps.global_principal,
    default_project=store.DEFAULT_PROJECT,
    auth_mode=auth.auth_mode,
    public_principal=auth.public_principal,
    principal_project_roles=store.principal_project_roles,
))
# ARCH-MS-92: when production sets PM_TASKS_HTTP_PRIMARY=service, Mode A CRUD +
# claim TXP are owned by switchboard-tasks (:8122) via Caddy. Monolith keeps only
# sibling BC subpaths (dispatch/chat/review_*). Hermetic TestClient leaves unset.
_TASKS_HTTP_PRIMARY = (os.environ.get("PM_TASKS_HTTP_PRIMARY") or "").strip().lower()
_COORD_HTTP_PRIMARY = (os.environ.get("PM_COORD_HTTP_PRIMARY") or "").strip().lower()
if _TASKS_HTTP_PRIMARY == "service":
    app.include_router(_create_task_router(
        resolve_project=_proj,
        resolve_principal=_principal,
        sibling_bc_only=True,
    ))
else:
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
app.include_router(_create_board_router(
    resolve_project=_proj,
    etag_json=_etag_json,
    saturation_snapshot=_saturation_snapshot,
    sibling_bc_only=_COORD_HTTP_PRIMARY == "service",
))
app.include_router(_create_resource_leases_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    resolve_body_project=_body_project,
))
app.include_router(_create_monitors_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    resolve_body_project=_body_project,
    omit_coord_delta=_COORD_HTTP_PRIMARY == "service",
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
app.include_router(_create_runner_pty_router(
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
app.include_router(_create_digest_notify_router(resolve_project=_proj))
app.include_router(_create_ops_export_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    resolve_body_project=_body_project,
))
app.include_router(_create_github_webhook_router(resolve_project=_proj))
app.include_router(_create_coordination_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    sibling_bc_only=_COORD_HTTP_PRIMARY == "service",
))
app.include_router(_create_deliverables_router(
    resolve_project=_proj,
    resolve_principal=_principal,
    etag_json=_etag_json,
))

register_middleware(
    app,
    req_obs=_req_obs,
    saturation_snapshot=_saturation_snapshot,
    global_user_scopes=deps.global_user_scopes,
    global_principal=deps.global_principal,
    admin_scopes=ADMIN_SCOPES,
)

_static = Path(__file__).parent / "static"
register_spa(app, static_dir=_static)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PM_PORT", "8110")))
