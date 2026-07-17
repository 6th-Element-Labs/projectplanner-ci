"""SPA shell routes + content-hashed static asset versioning (ARCH-MS-70).

Owns the board UI shell (``/``, ``/login``, ``/signup``, ``/account``,
``/forgot-password``, ``/reset-password``, ``/coordination``) and the
content-hashed asset versioning that keeps ``index.html`` (and friends)
always-fresh while hashed ``.js``/``.css`` stay long-cached. ``register_spa``
mounts the versioned static files last, so ``/api/*`` and ``/health`` win
route resolution.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

import auth
from switchboard.api.browser_session import current_browser_user


# --- Content-hashed static asset versions ------------------------------------
# The app shell (index.html) and the auth pages load app.js / taikun-*.css with a
# ?v=<n> query whose sole job is cache-busting. That number was bumped by hand and
# kept getting forgotten — three PRs changed app.js without a bump, so returning
# browsers ran week-stale JS and the deliverable map "never loaded" until #199 bumped
# it reactively. We derive ?v= from each asset's content hash at serve time instead:
# edit the file and its URL changes on the next request, with no human step. The HTML
# shell is served no-cache (see the request-observability middleware) so fresh hashes
# always reach the browser; the hashed assets are served immutable + long-cached
# (_VersionedStaticFiles).
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


def register_spa(app: FastAPI, *, static_dir: Path) -> None:
    """Register SPA shell routes and mount ``static_dir`` as versioned static assets."""

    def _shell_response(path: Path) -> Response:
        """Serve an HTML shell with every local .js/.css reference's ?v= rewritten to
        that asset's content hash. CDN/absolute ("/…") URLs are left untouched."""
        def _sub(m: "re.Match") -> bytes:
            asset = m.group(2)
            version = _asset_version(static_dir / asset.decode()).encode()
            return m.group(1) + asset + b"?v=" + version + m.group(3)
        return HTMLResponse(_LOCAL_ASSET_RE.sub(_sub, path.read_bytes()))

    @app.get("/", include_in_schema=False)
    async def root(request: Request):
        index = static_dir / "index.html"
        if await current_browser_user(request):
            return _shell_response(index)
        if auth.auth_mode() == auth.DEV_OPEN:
            return _shell_response(index)
        return _shell_response(static_dir / "login-global.html")

    @app.get("/login", include_in_schema=False)
    async def login_page():
        login = static_dir / "login-global.html"
        if login.exists():
            return _shell_response(login)
        raise HTTPException(404, "login page not found")

    @app.get("/signup", include_in_schema=False)
    async def signup_page():
        page = static_dir / "signup.html"
        if page.exists():
            return _shell_response(page)
        raise HTTPException(404, "signup page not found")

    @app.get("/account", include_in_schema=False)
    async def account_page(request: Request):
        page = static_dir / "account.html"
        if page.exists():
            return _shell_response(page)
        raise HTTPException(404, "account page not found")

    @app.get("/forgot-password", include_in_schema=False)
    async def forgot_password_page():
        page = static_dir / "forgot-password.html"
        if page.exists():
            return _shell_response(page)
        raise HTTPException(404, "forgot-password page not found")

    @app.get("/reset-password", include_in_schema=False)
    async def reset_password_page():
        page = static_dir / "reset-password.html"
        if page.exists():
            return _shell_response(page)
        raise HTTPException(404, "reset-password page not found")

    @app.get("/coordination", include_in_schema=False)
    async def coordination_page():
        """Standalone, read-only Agent Coordination view (the agent-to-agent war room).
        Unlinked from the board nav on purpose; reachable by URL. Data comes from
        /api/coordination, which is gated by the normal read auth."""
        page = static_dir / "coordination.html"
        if page.exists():
            return _shell_response(page)
        raise HTTPException(404, "coordination page not found")

    # Static board UI last, so /api/* and /health win. html=True serves index.html at /.
    if static_dir.exists():
        app.mount("/", _VersionedStaticFiles(directory=str(static_dir), html=True), name="static")
