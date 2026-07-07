"""Global auth routes (Service #1) — mounted only when PM_GLOBAL_AUTH is on.

Registered before the monolith's routes, so when the flag is on these override
/api/auth/login, /api/auth/logout and /api/projects; /api/auth/register and
/api/auth/session are new. The router is self-contained — it authenticates via
the JWT taikun_session cookie, not the monolith's per-project middleware.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from . import contracts
from . import service
from . import session

router = APIRouter()


def _is_secure(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return proto == "https"


def _client(request: Request) -> tuple:
    return (request.client.host if request.client else "",
            request.headers.get("user-agent", ""))


def _with_cookie(payload: dict, request: Request, token: str, expires_at: float) -> JSONResponse:
    resp = JSONResponse(payload)
    resp.set_cookie(value=token, **session.cookie_kwargs(expires_at, _is_secure(request)))
    return resp


@router.post("/api/auth/register")
async def register(request: Request, body: contracts.RegisterBody):
    ip, ua = _client(request)
    try:
        user, token, exp = service.register(body.email, body.display_name, body.password, ip=ip, user_agent=ua)
    except service.AuthError as e:
        raise HTTPException(e.status, e.message)
    return _with_cookie({"user": user}, request, token, exp)


@router.post("/api/auth/login")
async def login(request: Request, body: contracts.LoginBody):
    b = body
    ip, ua = _client(request)
    try:
        user, token, exp = service.login(
            b.email, b.password, remember_me=b.remember_me, ip=ip, user_agent=ua)
    except service.AuthError as e:
        raise HTTPException(e.status, e.message)
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

# NOTE: /api/projects filtering (deny-by-default project list) is delivered via
# /api/auth/session["user"]["projects"] today. Overriding the monolith's
# /api/projects route also requires teaching its HTTP auth middleware about the
# taikun_session JWT — that's the cutover step, not this flag-gated increment.
