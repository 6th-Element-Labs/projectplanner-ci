#!/usr/bin/env python3
"""Focused proof for the ARCH-MS-16 task REST router extraction."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT


TMP = tempfile.mkdtemp(prefix="arch-ms16-task-router-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def expanded_routes(routes):
    """Flatten FastAPI 0.139's lazy _IncludedRouter entries for inspection."""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from expanded_routes(included.routes)
        else:
            yield route


try:
    expected = {
        ("GET", "/api/tasks"),
        ("GET", "/api/tasks/{task_id}"),
        ("POST", "/api/tasks"),
        ("PATCH", "/api/tasks/{task_id}"),
        ("POST", "/api/tasks/{task_id}/review_verdict"),
        ("GET", "/api/tasks/{task_id}/review_verdict"),
        ("GET", "/api/tasks/{task_id}/review_findings"),
        ("POST", "/api/tasks/{task_id}/review_findings/{finding_id}/resolution"),
        ("GET", "/api/tasks/{task_id}/review_remediations"),
        ("POST", "/api/tasks/{task_id}/verify_offline"),
        ("DELETE", "/api/tasks/{task_id}"),
        ("POST", "/api/tasks/{task_id}/archive"),
        ("POST", "/api/tasks/{task_id}/move"),
        ("POST", "/api/tasks/{task_id}/claims/{claim_id}/revoke"),
        ("POST", "/api/tasks/{task_id}/comment"),
        ("POST", "/api/tasks/{task_id}/dispatch"),
        ("GET", "/api/tasks/{task_id}/dispatch/latest"),
        ("POST", "/api/tasks/{task_id}/resume-review"),
        ("POST", "/api/tasks/{task_id}/chat"),
    }
    actual = {
        (method, route.path)
        for route in expanded_routes(app.routes)
        if getattr(route, "path", "").startswith("/api/tasks")
        for method in (route.methods or set())
        if method != "HEAD"
    }
    ok(actual == expected,
       "composition root exposes every task route exactly once with unchanged methods")

    task_endpoints = [
        route.endpoint for route in expanded_routes(app.routes)
        if getattr(route, "path", "").startswith("/api/tasks")
    ]
    ok(task_endpoints and all(
        endpoint.__module__ == "switchboard.api.routers.tasks"
        for endpoint in task_endpoints
    ), "every /api/tasks endpoint is owned by switchboard.api.routers.tasks")

    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    router_source = (
        ROOT / "src/switchboard/api/routers/tasks.py"
    ).read_text(encoding="utf-8")
    ok('@app.get("/api/tasks' not in app_source
       and '@app.post("/api/tasks' not in app_source
       and '@app.patch("/api/tasks' not in app_source
       and '@app.delete("/api/tasks' not in app_source,
       "app.py contains no duplicate task route decorators")
    ok("get_task_query.execute_for" in router_source
       and "create_task_command.execute_mapping_result" in router_source
       and "update_task_command.execute_mapping_result" in router_source,
       "CRUD routes preserve the shared application command/query boundary")

    client = TestClient(app)
    created_response = client.post(
        "/api/tasks", params={"project": "switchboard"},
        json={"workstream_id": "ARCH", "title": "router smoke"})
    created = created_response.json()
    task_id = created.get("task_id")
    ok(created_response.status_code == 200 and bool(task_id),
       "extracted create route persists a task")

    fetched = client.get(
        f"/api/tasks/{task_id}", params={"project": "switchboard"})
    ok(fetched.status_code == 200 and fetched.json().get("title") == "router smoke",
       "extracted get route returns full task detail")

    patched = client.patch(
        f"/api/tasks/{task_id}", params={"project": "switchboard"},
        json={"description": "through the package router"})
    ok(patched.status_code == 200
       and patched.json().get("description") == "through the package router",
       "extracted patch route delegates through the update command")

    listed = client.get("/api/tasks", params={"project": "switchboard"})
    ok(listed.status_code == 200 and any(
        task.get("task_id") == task_id for task in listed.json().get("tasks", [])
    ), "extracted list route preserves project-aware filtering")

    missing_key = client.post(
        "/api/tasks", params={"project": "switchboard"},
        json={"title": "no workstream id"})
    missing_detail = (missing_key.json() or {}).get("detail") or {}
    ok(missing_key.status_code == 400
       and missing_detail.get("error_code") == "invalid_create_task",
       "BUG-55: create missing workstream_id is a structured 400, not a 500")

    junk_typed = client.post(
        "/api/tasks", params={"project": "switchboard"},
        json={"workstream_id": "ARCH", "title": "junk effort",
              "effort_days": "abc"})
    junk_detail = (junk_typed.json() or {}).get("detail") or {}
    ok(junk_typed.status_code == 400
       and junk_detail.get("error_code") == "invalid_create_task"
       and "effort_days" in str(junk_detail.get("message")),
       "BUG-55: type-invalid create field is a structured 400, not a 500")

    empty_optionals = client.post(
        "/api/tasks", params={"project": "switchboard"},
        json={"workstream_id": "ARCH", "title": "empty optionals",
              "description": "", "owner_org": "", "effort_days": ""})
    refetched = client.get(
        f"/api/tasks/{empty_optionals.json().get('task_id')}",
        params={"project": "switchboard"}).json()
    ok(empty_optionals.status_code == 200
       and refetched.get("description") is None
       and refetched.get("owner_org") is None
       and refetched.get("effort_days") is None,
       "BUG-55: '' optional fields persist as NULL (dataclass-era parity)")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
