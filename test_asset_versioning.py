#!/usr/bin/env python3
"""Auto-derived static asset versions.

The app shell and auth pages reference app.js / taikun-*.css with a ?v= query
whose only job is cache-busting. That number used to be bumped by hand and kept
getting forgotten, so returning browsers ran stale JS (the deliverable map "never
loaded" until #199). ?v= is now the asset's content hash, injected at serve time:
edit the file and its served URL changes on the next request. index.html stays
no-cache so fresh hashes always reach the browser; the hashed assets are served
immutable + long-cached.

Run:
    python3 test_asset_versioning.py
"""
import hashlib
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="asset-ver-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from fastapi.testclient import TestClient  # noqa: E402
    import app as appmod  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  asset versioning proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

passed = failed = 0


def ok(cond, msg):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    passed += 1 if cond else 0
    failed += 0 if cond else 1


def content_hash(name):
    return hashlib.sha256((appmod._static / name).read_bytes()).hexdigest()[:10]


try:
    c = TestClient(app)

    # ---- _asset_version is a content hash that tracks content changes -------
    probe = Path(_TMP) / "probe.js"
    probe.write_bytes(b"console.log('a')")
    h1 = appmod._asset_version(probe)
    ok(h1 == hashlib.sha256(b"console.log('a')").hexdigest()[:10],
       "_asset_version returns sha256[:10] of the file content")
    probe.write_bytes(b"console.log('a change of a different length')")
    ok(appmod._asset_version(probe) != h1,
       "_asset_version changes when the asset content changes")
    ok(appmod._asset_version(Path(_TMP) / "does-not-exist.js") == "0",
       "_asset_version returns '0' for a missing asset")

    # ---- served shell: ?v= == content hash, and stays no-cache --------------
    r = c.get("/")
    ok(r.status_code == 200, "/ returns 200")
    ok(r.headers.get("content-type", "").startswith("text/html"), "/ is served as HTML")
    ok(r.headers.get("cache-control") == "no-cache", "/ (index shell) is served no-cache")

    html = r.text
    for name in ("app.js", "taikun-ui.js", "taikun-tabler.css", "taikun-ui.css"):
        ok(f"{name}?v={content_hash(name)}" in html,
           f"{name} ?v= equals its content hash")

    local_refs = re.findall(
        r'(?:src|href)="(?!https?://|//|/)([^"?]+\.(?:js|css))(\?v=[0-9a-f]+)?"', html)
    ok(bool(local_refs) and all(ver for _, ver in local_refs),
       "no local .js/.css reference is left without a content ?v=")
    ok("https://cdn.jsdelivr.net/npm/bootstrap" in html,
       "absolute/CDN references are left untouched (not rewritten)")

    # auth shells share the same treatment (login.html under non-global auth)
    ok(f"taikun-tabler.css?v={content_hash('taikun-tabler.css')}" in c.get("/login").text,
       "/login shell also gets content-hash asset versions")

    # ---- hashed assets are immutable + long-cached; bare URLs are not -------
    cc = c.get(f"/app.js?v={content_hash('app.js')}").headers.get("cache-control", "")
    ok("immutable" in cc and "max-age=31536000" in cc,
       "hashed app.js is served immutable + long-cached")
    ok("immutable" not in c.get("/app.js").headers.get("cache-control", ""),
       "un-versioned app.js request is NOT marked immutable")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nasset versioning: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
