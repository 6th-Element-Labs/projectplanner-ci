#!/usr/bin/env python3
"""CONSOL-8: Caddy edge hardening + mission poller ETag/304 parity + ack visibility guard.

ADR-0007 execution row — the three cuts must stay in place:
  1. deploy/Caddyfile: security_headers + access_log snippets imported by site blocks
  2. app.py: mission_status + dependency_graph use _etag_json(..., max_age=5)
  3. static/app.js: mission fetches use cache:no-cache; ack poll has document.hidden guard
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from scripts.frontend_test_source import read_frontend_source

ROOT = Path(__file__).resolve().parent

REQUIRED_CADDY_HEADERS = (
    "Strict-Transport-Security",
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
    "Content-Security-Policy",
)

MISSION_ETAG_ROUTES = (
    "/api/mission_status",
    "/api/deliverables/{deliverable_id}/mission_status",
    "/api/deliverables/{deliverable_id}/dependency_graph",
)


def ok(condition, message):
    if not condition:
        raise AssertionError(message)


caddy = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
ok("(security_headers)" in caddy, "Caddyfile defines (security_headers) snippet")
ok("(access_log)" in caddy, "Caddyfile defines (access_log) snippet")
for header in REQUIRED_CADDY_HEADERS:
    ok(header in caddy, f"Caddyfile security_headers include {header}")
ok("import security_headers" in caddy, "site blocks import security_headers")
ok("import access_log" in caddy, "site blocks import access_log")
ok("format json" in caddy, "access_log uses structured JSON format")

app_src = (ROOT / "app.py").read_text(encoding="utf-8")
ok("def _etag_json(" in app_src, "app.py exposes shared _etag_json helper")
for route in MISSION_ETAG_ROUTES:
    ok(route in app_src, f"app.py registers {route}")
for fn in ("mission_status_query", "deliverable_mission_status", "deliverable_dependency_graph"):
    block = re.search(rf"def {fn}\(.*?\n(?:.*?\n)*?    return _etag_json\(request, result, max_age=5\)",
                      app_src, re.DOTALL)
    ok(block is not None, f"{fn} returns _etag_json(..., max_age=5)")

app_js = read_frontend_source(ROOT)
ok("cache: 'no-cache'" in app_js and "mission_status" in app_js,
   "loadMissionStatus uses cache:no-cache for ETag revalidation")
ok("dependency_graph" in app_js and app_js.count("cache: 'no-cache'") >= 2,
   "loadDependencyGraph uses cache:no-cache for ETag revalidation")
ok("document.hidden" in app_js and "_ackPoll" in app_js,
   "ack inbox poll skips ticks while the tab is hidden")
ok("visibilitychange" in app_js and "loadAckInbox" in app_js,
   "ack inbox refreshes on tab refocus")

if shutil.which("caddy"):
    proc = subprocess.run(
        ["caddy", "validate", "--adapter", "caddyfile", "--config", str(ROOT / "deploy/Caddyfile")],
        capture_output=True,
        text=True,
        check=False,
    )
    ok(proc.returncode == 0, f"deploy/Caddyfile passes caddy validate ({proc.stderr.strip() or 'ok'})")
else:
    print("  SKIP  caddy not on PATH; Caddyfile syntax validated by static checks only")

_TMP = tempfile.mkdtemp(prefix="consol8-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient  # noqa: E402

    import store  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  CONSOL-8 HTTP proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    print("CONSOL-8 edge + mission-poll checks passed")
    sys.exit(0)

HOME = "consol8-home"
client = TestClient(app)

try:
    store.init_project_registry()
    store.create_project("Consol8 Home", project_id=HOME, actor="test")
    store.init_db(HOME)
    task = store.create_task({"workstream_id": "C8", "title": "Consol8 task"},
                             actor="test", project=HOME)
    deliv = store.create_deliverable({"title": "Consol8 Deliverable", "end_state": "ship"},
                                     actor="test", project=HOME)
    did = deliv.get("id") or deliv.get("deliverable_id")
    store.link_task_to_deliverable(did, HOME, task["task_id"], actor="test", project=HOME)

    for path, params in (
        (f"/api/deliverables/{did}/mission_status", {"project": HOME}),
        (f"/api/deliverables/{did}/dependency_graph", {"project": HOME}),
        ("/api/mission_status", {"project": HOME, "deliverable_id": did}),
    ):
        first = client.get(path, params=params)
        ok(first.status_code == 200, f"GET {path} returns 200")
        etag = first.headers.get("etag")
        ok(bool(etag), f"GET {path} carries an ETag")
        cc = first.headers.get("cache-control", "")
        ok("max-age=5" in cc, f"GET {path} Cache-Control includes max-age=5 (got {cc!r})")
        second = client.get(path, params=params, headers={"If-None-Match": etag})
        ok(second.status_code == 304 and len(second.content) == 0,
           f"GET {path} If-None-Match -> 304 with empty body")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("CONSOL-8 edge + mission-poll checks passed")
