"""Resolve browser sessions at the Auth/board process boundary.

The browser Auth endpoints may be owned by the standalone Auth service while
the board remains on the monolith.  Both normally validate the same signed,
server-revocable ``taikun_session`` cookie locally.  If a rolling deploy or a
badly-reloaded process leaves the board verifier out of sync, do one bounded,
loopback-only validation against the Auth owner instead of treating a freshly
issued browser login as anonymous.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastapi import Request

from switchboard.api.routers.auth import service, session


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _auth_service_session_url() -> str:
    """Return a safe local Auth session endpoint, or an empty string.

    This fallback is meaningful only when Auth HTTP is process-owned.  Never
    forward a browser cookie to an arbitrary configured host.
    """
    if (os.environ.get("PM_AUTH_HTTP_PRIMARY") or "").strip().lower() != "service":
        return ""
    raw = (os.environ.get("PM_AUTH_SESSION_URL")
           or "http://127.0.0.1:8121/api/auth/session").strip()
    parsed = urllib.parse.urlparse(raw)
    if (parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS
            or parsed.path != "/api/auth/session"):
        return ""
    return raw


def _user_from_auth_owner(cookie: str) -> dict[str, Any] | None:
    """Ask the loopback Auth owner to validate one already-present cookie."""
    url = _auth_service_session_url()
    if not url or not cookie or any(char in cookie for char in "\r\n;"):
        return None
    request = urllib.request.Request(
        url,
        headers={"Cookie": f"{session.COOKIE_NAME}={cookie}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=1.0) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.HTTPError, urllib.error.URLError):
        return None
    user = payload.get("user") if isinstance(payload, dict) else None
    return user if isinstance(user, dict) and user.get("id") else None


async def current_browser_user(request: Request) -> dict[str, Any] | None:
    """Return the current browser user, with a safe split-service fallback."""
    cookie = request.cookies.get(session.COOKIE_NAME, "")
    user = service.current_user(cookie)
    if user or not cookie:
        return user
    return await asyncio.to_thread(_user_from_auth_owner, cookie)
