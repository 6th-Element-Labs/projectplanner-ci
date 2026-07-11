#!/usr/bin/env python3
"""HARDEN-32/33 — /health must stay cheap (no list_tasks on liveness probe)."""
import os
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="plan-health-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_AUTH_MODE"] = "off"

import store  # noqa: E402

store.init_db("switchboard")
for i in range(30):
    store.create_task(
        {"workstream_id": "ENG", "workstream_name": "Engine", "title": f"Perf {i}", "phase": "Build"},
        actor="test",
        project="switchboard",
    )

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  plan health test requires optional dependency: {exc.name}")
    raise SystemExit(0)

client = TestClient(app)
passed = failed = 0


def ok(cond, msg):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    passed += 1 if cond else 0
    failed += 0 if cond else 1


start = time.perf_counter()
r = client.get("/health")
elapsed_ms = (time.perf_counter() - start) * 1000
body = r.json()
ok(r.status_code == 200, "/health returns 200")
ok(body.get("status") == "ok" and body.get("service") == "taikun-pm", "/health returns liveness JSON")
ok("tasks" not in body and "projects" not in body, "/health does not call list_tasks")
ok(elapsed_ms < 500, f"/health responds quickly ({elapsed_ms:.1f}ms)")

# BUG-48: /health/deep is a readiness probe, publicly routed, so it must NOT leak project
# data (task counts, project ids/names) and must fail closed when a project is unready.
deep_r = client.get("/health/deep")
deep = deep_r.json()
ok(deep_r.status_code == 200 and deep.get("ready") is True, "/health/deep is 200/ready when all dbs are healthy")
ok("tasks" not in deep and "projects" not in deep, "/health/deep does not leak task counts or project ids")
ok(deep.get("projects_configured", 0) >= 1 and deep.get("projects_unready") == 0,
   "/health/deep reports project readiness counts without identifiers")

# Fail closed: a configured project that failed to initialize makes readiness 503.
import app as _app_mod  # noqa: E402
_app_mod._PROJECT_INIT_FAILURES["switchboard"] = "RuntimeError: simulated init failure"
try:
    unready_r = client.get("/health/deep")
    unready = unready_r.json()
    ok(unready_r.status_code == 503 and unready.get("ready") is False,
       "/health/deep fails closed (503) when a configured project could not initialize")
    ok(unready.get("projects_unready") >= 1 and "switchboard" not in unready_r.text,
       "/health/deep counts the unready project without naming it")
finally:
    _app_mod._PROJECT_INIT_FAILURES.pop("switchboard", None)

print(f"\nPlan health: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
