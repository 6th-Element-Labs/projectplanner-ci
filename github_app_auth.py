"""GitHub App installation tokens — the fleet's own rate-limit bucket.

Every server-side GitHub caller (open_prs, the PR gates, push verification, the
merge coordinator, deployment status, the orphan sweep) used to read the same
personal access token out of the environment. A user PAT is billed **per GitHub
account**, 5,000 REST requests/hour shared across every token that account owns —
so the batch timers, the Fleet dock's sweeps and an operator's local `gh` all drew
on one budget. On 2026-07-26 prod sat at `x-ratelimit-remaining: 0` for hours and
every GitHub read returned `403 API rate limit exceeded for user ID …`, which is
what blanked the dock's PR and Deployment panes.

An App installation token is billed to the **installation**, not to a human, so the
fleet stops competing with itself (and with the operator). This module owns that
exchange: App JWT (RS256, ≤10 min) → `POST /app/installations/{id}/access_tokens`
→ a ~1-hour `ghs_` token, cached in-process and refreshed before it expires.

Configuration (trusted runtime only — the PEM never goes near the board DB, MCP,
wakes, or logs; see docs/SCM-GITHUB-APP-ONBOARDING.md):

    PM_GITHUB_APP_ID                 App id from the App's settings page
    PM_GITHUB_APP_PRIVATE_KEY_PATH   path to the downloaded .pem (preferred)
    PM_GITHUB_APP_PRIVATE_KEY        inline PEM (CI/containers; `\\n` accepted)
    PM_GITHUB_APP_INSTALLATION_ID    optional: pin one installation for every repo
    PM_GITHUB_APP_INSTALLATIONS      optional: {"<owner>": <installation_id>} map

Nothing is required. With no App configured this module resolves the same PAT
chain as before, so an un-migrated deployment behaves exactly as it did.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

APP_ID_ENV = "PM_GITHUB_APP_ID"
PRIVATE_KEY_PATH_ENV = "PM_GITHUB_APP_PRIVATE_KEY_PATH"
PRIVATE_KEY_ENV = "PM_GITHUB_APP_PRIVATE_KEY"
INSTALLATION_ID_ENV = "PM_GITHUB_APP_INSTALLATION_ID"
INSTALLATION_MAP_ENV = "PM_GITHUB_APP_INSTALLATIONS"

#: Default PAT precedence. Callers that historically used a different order pass
#: their own ``env_order`` so this module never silently re-ranks their secrets.
DEFAULT_ENV_ORDER: Tuple[str, ...] = (
    "PM_GITHUB_TOKEN", "GITHUB_TOKEN", "SWITCHBOARD_CI_GITHUB_TOKEN")

API_ROOT = "https://api.github.com"
#: GitHub caps App JWTs at 10 minutes; stay under it and backdate for clock skew.
JWT_TTL_SECONDS = 540
JWT_BACKDATE_SECONDS = 60
#: Re-mint this long before the installation token actually expires, so a request
#: in flight never carries a token that dies mid-call.
REFRESH_SKEW_SECONDS = 300

_lock = threading.Lock()
_token_cache: Dict[int, Tuple[str, float]] = {}      # installation id -> (token, expires_at)
_installation_cache: Dict[str, int] = {}             # owner -> installation id
_last_error: str = ""
_last_source: str = ""


def reset_cache() -> None:
    """Drop every cached token/installation id (tests, and key rotation)."""
    global _last_error, _last_source
    with _lock:
        _token_cache.clear()
        _installation_cache.clear()
        _last_error = ""
        _last_source = ""


def last_error() -> str:
    """Why the last App mint failed, if it did. Kept so callers can report a cause
    instead of a blank 'GitHub is unavailable'."""
    return _last_error


def token_source() -> str:
    """``github_app``, ``env:<VAR>``, or '' — which credential the last resolve used."""
    return _last_source


def _private_key() -> str:
    path = (os.environ.get(PRIVATE_KEY_PATH_ENV) or "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError as exc:
            _record_error(f"private key unreadable at {path}: {exc}")
            return ""
    inline = (os.environ.get(PRIVATE_KEY_ENV) or "").strip()
    # Env vars in systemd units / CI secrets commonly carry literal backslash-n.
    return inline.replace("\\n", "\n") if inline else ""


def _app_id() -> str:
    return (os.environ.get(APP_ID_ENV) or "").strip()


def app_configured() -> bool:
    """True when an App id and a readable private key are both present."""
    return bool(_app_id() and _private_key())


def _record_error(message: str) -> None:
    global _last_error
    _last_error = str(message)


def app_jwt(*, now: Optional[float] = None) -> str:
    """Sign the short-lived App JWT that authenticates as the App itself."""
    import jwt  # pinned in requirements.txt (pyjwt), already on the prod venv

    now = time.time() if now is None else float(now)
    payload = {
        "iat": int(now) - JWT_BACKDATE_SECONDS,
        "exp": int(now) + JWT_TTL_SECONDS,
        "iss": _app_id(),
    }
    return jwt.encode(payload, _private_key(), algorithm="RS256")


def _default_request(method: str, url: str, *, token: str = "",
                     now: Optional[float] = None) -> Any:
    request = urllib.request.Request(url, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "switchboard-github-app")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 — fixed API host
        body = response.read().decode("utf-8") or "{}"
    return json.loads(body)


def _configured_installations() -> Mapping[str, int]:
    raw = (os.environ.get(INSTALLATION_MAP_ENV) or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        _record_error(f"{INSTALLATION_MAP_ENV} is not valid JSON: {exc}")
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    out: Dict[str, int] = {}
    for owner, value in parsed.items():
        try:
            out[str(owner).lower()] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _installation_id(repo: str, request_fn: Callable[..., Any],
                     *, now: Optional[float] = None) -> int:
    """Which installation covers ``repo``.

    A pinned id wins, then a configured owner→id map, then GitHub itself. The
    lookup matters because the fleet spans owners (the org repos plus a personal
    account's Helm) and each install is a separate token *and* a separate bucket.
    """
    pinned = (os.environ.get(INSTALLATION_ID_ENV) or "").strip()
    if pinned:
        return int(pinned)
    owner = (repo or "").split("/", 1)[0].lower()
    configured = _configured_installations()
    if owner and owner in configured:
        return configured[owner]
    with _lock:
        cached = _installation_cache.get(owner)
    if cached:
        return cached
    if not repo or "/" not in repo:
        raise ValueError(
            f"cannot resolve a GitHub App installation for {repo!r}: set "
            f"{INSTALLATION_ID_ENV} or {INSTALLATION_MAP_ENV}")
    payload = request_fn("GET", f"{API_ROOT}/repos/{repo}/installation",
                         token=app_jwt(now=now), now=now)
    installation_id = int((payload or {}).get("id") or 0)
    if not installation_id:
        raise ValueError(f"GitHub returned no installation for {repo}")
    with _lock:
        _installation_cache[owner] = installation_id
    return installation_id


def _parse_expiry(value: Any, *, now: float) -> float:
    """GitHub returns ISO-8601 Z; fall back to the documented 1-hour lifetime."""
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1]
    try:
        parsed = time.strptime(text, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return now + 3600.0
    import calendar
    return float(calendar.timegm(parsed))


def installation_token(repo: str = "", *, request_fn: Optional[Callable[..., Any]] = None,
                       now: Optional[float] = None) -> str:
    """A cached installation access token for ``repo``'s installation.

    Raises on failure — ``resolve_token`` is the forgiving entry point that callers
    on a read path should use.
    """
    request_fn = request_fn or _default_request
    now = time.time() if now is None else float(now)
    installation_id = _installation_id(repo, request_fn, now=now)
    with _lock:
        cached = _token_cache.get(installation_id)
    if cached and cached[1] - REFRESH_SKEW_SECONDS > now:
        return cached[0]
    payload = request_fn(
        "POST", f"{API_ROOT}/app/installations/{installation_id}/access_tokens",
        token=app_jwt(now=now), now=now)
    token = str((payload or {}).get("token") or "")
    if not token:
        raise ValueError(f"GitHub minted no token for installation {installation_id}")
    expires_at = _parse_expiry((payload or {}).get("expires_at"), now=now)
    with _lock:
        _token_cache[installation_id] = (token, expires_at)
    return token


def _env_token(env_order: Sequence[str]) -> Tuple[str, str]:
    for name in env_order:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, f"env:{name}"
    return "", ""


def resolve_token(*, repo: str = "", explicit: str = "",
                  env_order: Sequence[str] = DEFAULT_ENV_ORDER,
                  request_fn: Optional[Callable[..., Any]] = None,
                  now: Optional[float] = None) -> str:
    """The one credential resolver every GitHub caller should use.

    Order: an explicit token, then an App installation token, then the caller's PAT
    chain. A mint failure degrades to the PAT and keeps the reason in
    ``last_error()`` — a read path that already tolerates a missing token must not
    start hard-failing just because the App is misconfigured.
    """
    global _last_source
    if explicit:
        _last_source = "explicit"
        return explicit
    if app_configured():
        try:
            token = installation_token(repo, request_fn=request_fn, now=now)
            if token:
                _last_source = "github_app"
                return token
        except Exception as exc:  # noqa: BLE001 — reason is kept, not swallowed
            _record_error(f"{type(exc).__name__}: {exc}")
    token, source = _env_token(env_order)
    _last_source = source
    return token


def status() -> Dict[str, Any]:
    """Non-secret summary for health surfaces. Never includes the key or a token."""
    return {
        "configured": app_configured(),
        "app_id_set": bool(_app_id()),
        "private_key_set": bool(_private_key()),
        "installations_cached": len(_installation_cache),
        "tokens_cached": len(_token_cache),
        "source": _last_source,
        "last_error": _last_error,
    }


__all__ = [
    "APP_ID_ENV",
    "DEFAULT_ENV_ORDER",
    "INSTALLATION_ID_ENV",
    "INSTALLATION_MAP_ENV",
    "PRIVATE_KEY_ENV",
    "PRIVATE_KEY_PATH_ENV",
    "app_configured",
    "app_jwt",
    "installation_token",
    "last_error",
    "reset_cache",
    "resolve_token",
    "status",
    "token_source",
]
