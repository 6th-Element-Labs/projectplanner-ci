"""SessionManager — issue/verify the global taikun_session cookie.

The cookie holds an HS256 JWT ({sub, sid, email, is_superadmin, iat, exp}) for
ActionEngine parity, and `sid` references a server-side row so sessions are
*revocable* (pure JWT can't be). Verify checks the signature + expiry AND that
the sid row is still live, then reloads the user fresh from the DB.
"""
from __future__ import annotations

import os
import secrets
import time
from typing import Any, Dict, Optional, Tuple

from . import jwt_util
from . import store as auth_store

COOKIE_NAME = os.environ.get("PM_SESSION_COOKIE_NAME", "taikun_session")


def _secret() -> str:
    return (os.environ.get("PM_JWT_SECRET")
            or os.environ.get("PM_AUTH_TOKEN")
            or "dev-insecure-jwt-secret-change-me")


def _ttl_seconds(remember_me: bool) -> int:
    hours = (os.environ.get("PM_JWT_REMEMBER_ME_HOURS", "720") if remember_me
             else os.environ.get("PM_JWT_SESSION_HOURS", "168"))
    try:
        return int(float(hours) * 3600)
    except Exception:
        return (720 if remember_me else 168) * 3600


def issue(user: Dict[str, Any], *, remember_me: bool = False,
          ip: str = "", user_agent: str = "") -> Tuple[str, float]:
    """Create a session row + signed JWT. Returns (jwt, expires_at)."""
    ttl = _ttl_seconds(remember_me)
    sid = secrets.token_urlsafe(32)
    session = auth_store.create_session(user["id"], sid, ttl, ip=ip, user_agent=user_agent)
    now = int(time.time())
    payload = {
        "sub": user["id"],
        "sid": sid,
        "email": user.get("email"),
        "is_superadmin": bool(user.get("is_superadmin")),
        "iat": now,
        "exp": int(session["expires_at"]),
    }
    return jwt_util.encode(payload, _secret()), session["expires_at"]


def verify(token: str) -> Optional[Dict[str, Any]]:
    """Return the fresh user for a valid, unrevoked session, else None."""
    payload, _reason = jwt_util.decode(token or "", _secret())
    if not payload:
        return None
    sid = payload.get("sid")
    if not sid:
        return None
    return auth_store.user_for_session(sid)  # validates row + reloads user


def revoke(token: str) -> bool:
    payload, _ = jwt_util.decode(token or "", _secret())
    sid = (payload or {}).get("sid")
    return auth_store.revoke_session(sid) if sid else False


def cookie_kwargs(expires_at: float, secure: bool) -> Dict[str, Any]:
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "secure": secure,
        "samesite": "lax",
        "path": "/",
        "max_age": max(60, int(expires_at - time.time())),
    }
