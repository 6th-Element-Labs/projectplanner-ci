#!/usr/bin/env python3
"""ARCH-MS-125 / BUG-69: the Tasks service (:8122) must reject anonymous reads
exactly like the monolith it replaces.

BUG-69 shipped twice (2026-07-15, 2026-07-17): switchboard.services.tasks.app::
create_app never registered the global auth middleware, so an anonymous GET
against /api/tasks* returned 200 with live task data once Caddy routed Mode A
traffic to :8122. Route-level auth still gated writes, which is exactly why
test_arch_ms91_tasks_parity.py's write-only checks never caught it — reads are
a materially different code path with no route-level check at all.

This file pins three independent layers so the gap cannot reopen silently:
  1. The fix itself: create_app calls register_auth_gate.
  2. The extraction didn't change monolith behavior: register_middleware still
     wires the same auth boundary, same order.
  3. The deploy-time proof (scripts/verify_runtime_deploy.py, which gates every
     deploy/redeploy.sh run) actually checks anonymous rejection, not just
     which port answers an authenticated request.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

from path_setup import ROOT

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# --- 1. the fix: create_app wires the auth gate ------------------------------
tasks_app_src = (ROOT / "src" / "switchboard" / "services" / "tasks" / "app.py").read_text(encoding="utf-8")
ok("register_auth_gate" in tasks_app_src,
   "tasks/app.py imports/calls register_auth_gate")
ok("from switchboard.api.middleware import register_auth_gate" in tasks_app_src,
   "tasks/app.py imports it from the shared middleware module, not a local copy")

# --- 2. the extraction is behavior-preserving for the monolith ---------------
middleware_src = (ROOT / "src" / "switchboard" / "api" / "middleware.py").read_text(encoding="utf-8")
ok("def register_auth_gate(" in middleware_src, "middleware.py exposes register_auth_gate")
ok("def register_middleware(" in middleware_src, "middleware.py still exposes register_middleware")
ok(middleware_src.count("async def _auth_boundary") == 1,
   "the auth boundary handler exists in exactly one place (extracted, not duplicated)")
rm_body = middleware_src.split("def register_middleware(", 1)[1]
ok("register_auth_gate(app" in rm_body,
   "register_middleware still registers the auth gate against the same app")

# --- 3. live proof: anonymous reads are actually rejected, both directly and
#        through the same TestClient harness ARCH-MS-91's parity suite uses ---
TMP = tempfile.mkdtemp(prefix="archms125-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(Path(TMP) / "projects")
os.environ["PM_JWT_SECRET"] = "test-secret-archms125"
Path(os.environ["PM_DYNAMIC_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)

os.environ["PM_AUTH_MODE"] = "dev-open"
import store  # noqa: E402

store.init_project_registry()
PROJECT = "archms125-alpha"
store.create_project("ARCH-MS-125 Alpha", project_id=PROJECT, actor="test")
store.init_db(PROJECT)

from fastapi.testclient import TestClient  # noqa: E402
from switchboard.services.tasks import create_app  # noqa: E402
from switchboard.services.tasks.settings import TasksServiceSettings  # noqa: E402

os.environ["PM_AUTH_MODE"] = "required"
client = TestClient(create_app(TasksServiceSettings(
    service_name="archms125-test", host="127.0.0.1", port=8122,
)))
anon_list = client.get("/api/tasks", params={"project": PROJECT})
ok(anon_list.status_code == 401,
   f"anonymous GET /api/tasks is 401 on the real Tasks app (got {anon_list.status_code})")
ok(anon_list.status_code != 200,
   "the exact BUG-69 symptom (200 with data) does not reproduce")
ok(anon_list.status_code != 403,
   "rejection is 401, not 403 (matches the monolith's write-parity contract)")

anon_health = client.get("/health")
ok(anon_health.status_code == 200,
   f"/health stays open under required auth (got {anon_health.status_code})")

anon_write = client.post("/api/tasks", params={"project": PROJECT},
                         json={"workstream_id": "ARCH-MS", "title": "archms125 unauth"})
ok(anon_write.status_code == 401,
   f"anonymous write is still 401, unchanged by this fix (got {anon_write.status_code})")

os.environ["PM_AUTH_MODE"] = "dev-open"

# --- 4. the deploy-time proof actually exercises this, not just route owner --
spec = importlib.util.spec_from_file_location(
    "archms125_verify_runtime_deploy", ROOT / "scripts" / "verify_runtime_deploy.py",
)
assert spec and spec.loader
verify = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verify
spec.loader.exec_module(verify)

ok(hasattr(verify, "check_anon_read_rejected"),
   "verify_runtime_deploy.py exposes an anonymous-rejection check")
ok("check_anon_read_rejected(" in Path(verify.__file__).read_text(encoding="utf-8").split("def build_evidence")[1],
   "build_evidence actually calls it, not just defines it")


def rejecting_probe(url, **_):
    return {"url": url, "method": "GET", "http_status": 401,
            "body_sha256": "x", "body_semantic_sha256": "x"}


def leaking_probe(url, **_):
    return {"url": url, "method": "GET", "http_status": 200,
            "body_sha256": "y", "body_semantic_sha256": "y"}


original = verify.http_fingerprint
try:
    verify.http_fingerprint = rejecting_probe
    result = verify.check_anon_read_rejected(
        base_url="https://plan.example", path="/api/tasks?project=switchboard",
    )
    ok(result.ok, "the deploy-time check passes when the edge correctly rejects (401)")

    verify.http_fingerprint = leaking_probe
    result = verify.check_anon_read_rejected(
        base_url="https://plan.example", path="/api/tasks?project=switchboard",
    )
    ok(not result.ok,
       "the deploy-time check FAILS when the edge leaks data (200) -- this is the "
       "exact BUG-69 condition that shipped twice with no automated proof to catch it")
finally:
    verify.http_fingerprint = original

# http_fingerprint itself must actually go anonymous when no token is given --
# a check that quietly still sent Authorization would prove nothing.
captured = {}
real_request_cls = verify.urllib.request.Request


def capturing_request(url, *args, **kwargs):
    req = real_request_cls(url, *args, **kwargs)
    captured["has_auth_header"] = "Authorization" in req.headers
    return req  # let it proceed to a real (refused) connection -- http_fingerprint
                # already catches that as {"error": ...}, no need to fake it


verify.urllib.request.Request = capturing_request
try:
    # Port 1 is a privileged, essentially-never-listening port -- urlopen will fail
    # with a real connection error, which http_fingerprint's own except clause
    # already handles. What matters here is only the header the request carried.
    verify.http_fingerprint("http://127.0.0.1:1/nope")
finally:
    verify.urllib.request.Request = real_request_cls
ok(captured.get("has_auth_header") is False,
   "http_fingerprint with no token sends no Authorization header (genuinely anonymous)")

print(f"\nARCH-MS-125 / BUG-69 Tasks auth gate: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
