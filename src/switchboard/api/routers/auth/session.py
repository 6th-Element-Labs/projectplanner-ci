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

# Hardcoded fallback is DEV_OPEN-only. Production (PM_AUTH_MODE=required) must set
# PM_JWT_SECRET — ARCH-MS-83 forbids silent DEV JWT material in required mode.
_DEV_JWT_FALLBACK = "dev-insecure-jwt-secret-change-me"


class AuthSecretError(RuntimeError):
    """Raised when production auth is missing a signing secret (fail-fast)."""


def _auth_mode_is_required() -> bool:
    """Mirror root ``auth.auth_mode()`` without importing the monolith (ARCH-MS-82)."""
    mode = (os.environ.get("PM_AUTH_MODE") or "required").strip().lower()
    if mode in {"dev", "open", "local", "dev-open"}:
        return False
    return True


def _secret() -> str:
    """Return the JWT HS256 signing secret.

    * ``PM_AUTH_MODE=required`` (default / production): require non-empty
      ``PM_JWT_SECRET``. No ``PM_AUTH_TOKEN`` fallback and no silent DEV string.
    * ``PM_AUTH_MODE=dev-open``: allow ``PM_JWT_SECRET``, then ``PM_AUTH_TOKEN``,
      then the explicit local DEV fallback (tests / laptop only).
    """
    jwt_secret = (os.environ.get("PM_JWT_SECRET") or "").strip()
    if jwt_secret:
        return jwt_secret
    if _auth_mode_is_required():
        raise AuthSecretError(
            "PM_JWT_SECRET is required when PM_AUTH_MODE=required "
            "(refusing silent DEV JWT fallback / PM_AUTH_TOKEN substitute)."
        )
    return (os.environ.get("PM_AUTH_TOKEN") or "").strip() or _DEV_JWT_FALLBACK


def require_production_secret() -> str:
    """Boot-time check: resolve the signing secret or raise ``AuthSecretError``."""
    return _secret()


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
    if not (token or "").strip():
        return None  # unauthenticated; do not require JWT secret to reject empty cookies
    payload, _reason = jwt_util.decode(token, _secret())
    if not payload:
        return None
    sid = payload.get("sid")
    if not sid:
        return None
    return auth_store.user_for_session(sid)  # validates row + reloads user


def revoke(token: str) -> bool:
    if not (token or "").strip():
        return False
    payload, _ = jwt_util.decode(token, _secret())
    sid = (payload or {}).get("sid")
    return auth_store.revoke_session(sid) if sid else False


def sid_of(token: str) -> Optional[str]:
    """The server-side session id (sid) inside a token, or None if unparseable."""
    if not (token or "").strip():
        return None
    payload, _ = jwt_util.decode(token, _secret())
    return (payload or {}).get("sid")


def cookie_kwargs(expires_at: float, secure: bool) -> Dict[str, Any]:
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "secure": secure,
        "samesite": "lax",
        "path": "/",
        "max_age": max(60, int(expires_at - time.time())),
    }
