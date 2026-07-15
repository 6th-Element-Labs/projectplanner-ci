"""Global auth routes.

Registered before the monolith's routes. Authenticates via the JWT
taikun_session cookie.
"""
from __future__ import annotations

import os
from typing import Callable

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import contracts, service, session

router = APIRouter()

ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
GlobalUserScopes = Callable[[dict, str], list]
GlobalPrincipal = Callable[[dict, list], dict]
AuthModeFn = Callable[[], str]
PublicPrincipalFn = Callable[[dict], dict]
PrincipalProjectRolesFn = Callable[[str, str], list]


def _is_secure(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return proto == "https"


def _public_base_url(request: Request) -> str:
    """The externally-visible origin, for building emailed links. Honors an explicit
    PM_PUBLIC_BASE_URL, else reconstructs from the proxy's forwarded headers."""
    explicit = (os.environ.get("PM_PUBLIC_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host") or request.url.netloc)
    return f"{proto}://{host}"


def _client(request: Request) -> tuple:
    return (request.client.host if request.client else "",
            request.headers.get("user-agent", ""))


def _with_cookie(payload: dict, request: Request, token: str, expires_at: float) -> JSONResponse:
    resp = JSONResponse(payload)
    resp.set_cookie(value=token, **session.cookie_kwargs(expires_at, _is_secure(request)))
    return resp


def _http_error(e: "service.AuthError") -> HTTPException:
    """Map an AuthError to an HTTPException, adding Retry-After on 429 throttles."""
    retry_after = getattr(e, "retry_after", None)
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return HTTPException(e.status, e.message, headers=headers)


@router.post("/api/auth/register")
async def register(request: Request, body: contracts.RegisterBody):
    ip, ua = _client(request)
    try:
        user, token, exp = service.register(body.email, body.display_name, body.password, ip=ip, user_agent=ua)
    except service.AuthError as e:
        raise _http_error(e)
    return _with_cookie({"user": user}, request, token, exp)


@router.post("/api/auth/login")
async def login(request: Request, body: contracts.LoginBody):
    b = body
    ip, ua = _client(request)
    try:
        user, token, exp = service.login(
            b.email, b.password, remember_me=b.remember_me, ip=ip, user_agent=ua)
    except service.AuthError as e:
        raise _http_error(e)
    return _with_cookie({"user": user}, request, token, exp)


@router.get("/api/auth/session")
async def whoami(request: Request):
    token = request.cookies.get(session.COOKIE_NAME, "")
    user = service.current_user(token)
    if not user:
        raise HTTPException(401, "not authenticated")
    return {"authenticated": True, "user": user}


@router.post("/api/auth/logout")
async def logout(request: Request):
    token = request.cookies.get(session.COOKIE_NAME, "")
    revoked = service.logout(token)
    resp = JSONResponse({"logged_out": True, "revoked": revoked})
    resp.delete_cookie(session.COOKIE_NAME, path="/")
    return resp


@router.post("/api/auth/change-password")
async def change_password(request: Request, body: contracts.ChangePasswordBody):
    """Self-service password change for the signed-in user (Account settings)."""
    token = request.cookies.get(session.COOKIE_NAME, "")
    try:
        user = service.change_password(token, body.current_password, body.new_password)
    except service.AuthError as e:
        raise _http_error(e)
    return {"user": user, "changed": True}


@router.post("/api/auth/forgot-password")
async def forgot_password(request: Request, body: contracts.ForgotPasswordBody):
    """Email a single-use reset link. Always 200 with the same message so the
    endpoint can't reveal whether an email is registered (anti-enumeration).
    Throttled per-IP/per-email (429) to stop reset-email flooding."""
    ip, ua = _client(request)
    try:
        service.request_password_reset(body.email, _public_base_url(request), ip=ip, user_agent=ua)
    except service.AuthError as e:
        raise _http_error(e)
    return {"ok": True,
            "message": "If an account exists for that email, a reset link is on its way."}


@router.post("/api/auth/reset-password")
async def reset_password(request: Request, body: contracts.ResetPasswordBody):
    """Complete a reset: spend the token, set the new password, sign out everywhere."""
    ip, ua = _client(request)
    try:
        service.reset_password(body.token, body.new_password, ip=ip, user_agent=ua)
    except service.AuthError as e:
        raise _http_error(e)
    return {"ok": True, "message": "Your password has been reset. You can now sign in."}

# NOTE: Cookie sessions still get deny-by-default project lists via
# /api/auth/session["user"]["projects"] and the cookie branch of GET /api/projects.
# Bearer callers (env MCP/auth tokens, scoped agents) use the same route's
# principal branch (ACCESS-25) so the boot picker matches /api/board.


def create_me_router(*, resolve_project: ProjectResolver,
                     resolve_principal: PrincipalResolver,
                     global_user_scopes: GlobalUserScopes,
                     global_principal: GlobalPrincipal,
                     default_project: str,
                     auth_mode: AuthModeFn,
                     public_principal: PublicPrincipalFn,
                     principal_project_roles: PrincipalProjectRolesFn) -> APIRouter:
    """Build the ``/api/auth/me`` router — maps the global session (or a bearer
    token) to per-project effective scopes for UI compatibility.

    Monolith auth/store helpers are injected by the composition root so this
    package never imports root ``auth`` / ``store`` (ARCH-MS-82).
    """
    me_router = APIRouter()

    @me_router.get("/api/auth/me")
    async def auth_me(request: Request, project: str = Query(default_project)):
        """UI compatibility: map the global session to per-project effective scopes."""
        user = service.current_user(request.cookies.get(session.COOKIE_NAME, ""))
        if user:
            proj = resolve_project(project)
            scopes = global_user_scopes(user, proj)
            principal = global_principal(user, scopes)
            principal["project_roles"] = principal_project_roles(proj, user["id"])
            return {"principal": principal, "mode": auth_mode(), "project": proj}
        principal = resolve_principal(request, project, ("read",), dev_actor="web")
        return {"principal": public_principal(principal),
                "mode": auth_mode(), "project": resolve_project(project)}

    return me_router
