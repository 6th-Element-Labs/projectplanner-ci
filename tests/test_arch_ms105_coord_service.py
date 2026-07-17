#!/usr/bin/env python3
"""ARCH-MS-105: standalone Coord :8123 service and side-by-side parity."""
from __future__ import annotations

import ast
import importlib
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from path_setup import ROOT

TMP = tempfile.mkdtemp(prefix="arch-ms105-coord-service-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(Path(TMP) / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_JWT_SECRET"] = "test-secret-arch-ms105"
Path(os.environ["PM_DYNAMIC_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)

PROJECT = "ms105-alpha"
OTHER = "ms105-beta"
COORD_PACKAGE = ROOT / "src" / "switchboard" / "services" / "coord"
FORBIDDEN_ROOTS = frozenset({"auth", "dispatch", "signals", "store"})
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def forbidden_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in FORBIDDEN_ROOTS:
                    hits.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and not node.level:
            module = node.module or ""
            if module.split(".", 1)[0] in FORBIDDEN_ROOTS:
                hits.append(f"from {module} import")
    return hits


def baseline_client() -> TestClient:
    from switchboard.api import deps
    from switchboard.api.routers import board, coordination, monitors

    app = FastAPI(title="arch-ms105-monolith-baseline")
    app.include_router(board.create_router(
        resolve_project=deps.resolve_project,
        etag_json=deps.etag_json,
        saturation_snapshot=lambda project: {"project": project},
    ))
    app.include_router(coordination.create_router(
        resolve_project=deps.resolve_project,
        resolve_principal=deps.resolve_principal,
    ))
    app.include_router(monitors.create_router(
        resolve_project=deps.resolve_project,
        resolve_principal=deps.resolve_principal,
        resolve_body_project=deps.resolve_body_project,
    ))
    return TestClient(app)


def parity(name: str, baseline, cut) -> None:
    ok(baseline.status_code == cut.status_code,
       f"{name} status parity {baseline.status_code}={cut.status_code}")
    if baseline.status_code == 200:
        ok(baseline.json() == cut.json(), f"{name} response parity")


try:
    for name in (
        "switchboard.services.coord",
        "switchboard.services.coord.settings",
        "switchboard.services.coord.health",
        "switchboard.services.coord.app",
    ):
        try:
            importlib.import_module(name)
            ok(True, f"import {name}")
        except Exception as exc:
            ok(False, f"import {name}: {exc}")

    for path in sorted(COORD_PACKAGE.glob("*.py")):
        hits = forbidden_imports(path)
        ok(not hits, f"{path.name}: no forbidden monolith imports"
           + (f" ({hits})" if hits else ""))

    import store  # noqa: E402
    from switchboard.services.coord import create_app  # noqa: E402
    from switchboard.services.coord.settings import CoordServiceSettings  # noqa: E402

    store.init_project_registry()
    store.create_project("MS105 Alpha", project_id=PROJECT, actor="test")
    store.create_project("MS105 Beta", project_id=OTHER, actor="test")
    # Required-mode bearer lookup deliberately scans every configured project.
    # Initialize the full hermetic registry so a negative lookup fails closed,
    # rather than failing on an unrelated empty project DB.
    for project_id in store.project_ids():
        store.init_db(project_id)
    store.create_task(
        {"workstream_id": "ARCH-MS", "title": "Coord parity fixture"},
        actor="test", project=PROJECT,
    )
    store.register_agent("codex/ms105-fixture", "codex", lane="ARCH-MS", project=PROJECT)

    baseline = baseline_client()
    settings = CoordServiceSettings(
        service_name="arch-ms105-test", host="127.0.0.1", port=8123,
    )
    cut = TestClient(create_app(settings))

    health = cut.get("/health")
    ok(health.status_code == 200, f"cut /health status {health.status_code}")
    ok(health.json() == {"status": "ok", "service": "arch-ms105-test"},
       "cut /health identifies Coord service")

    parity(
        "board",
        baseline.get("/api/board", params={"project": PROJECT}),
        cut.get("/api/board", params={"project": PROJECT}),
    )
    parity(
        "board cards",
        baseline.get("/api/board", params={"project": PROJECT, "view": "cards"}),
        cut.get("/api/board", params={"project": PROJECT, "view": "cards"}),
    )
    parity(
        "signals",
        baseline.get("/api/signals", params={"project": PROJECT}),
        cut.get("/api/signals", params={"project": PROJECT}),
    )
    parity(
        "delta",
        baseline.get("/ixp/v1/delta", params={
            "project": PROJECT, "lane": "ARCH-MS", "since_cursor": 0,
        }),
        cut.get("/ixp/v1/delta", params={
            "project": PROJECT, "lane": "ARCH-MS", "since_cursor": 0,
        }),
    )
    parity(
        "coordination",
        baseline.get("/api/coordination", params={"project": PROJECT, "limit": 25}),
        cut.get("/api/coordination", params={"project": PROJECT, "limit": 25}),
    )
    parity(
        "coordinator decisions",
        baseline.get("/api/coordinator_decisions", params={"project": PROJECT}),
        cut.get("/api/coordinator_decisions", params={"project": PROJECT}),
    )

    for method, path in (
        ("get", "/api/people"),
        ("get", "/api/dispatch/status"),
        ("get", "/ixp/v1/working_agreement"),
        ("get", "/api/coordinator_dispatch/plan"),
        ("post", "/api/coordinator_dispatch"),
    ):
        response = getattr(cut, method)(path, params={"project": PROJECT})
        ok(response.status_code == 404,
           f"non-chartered {method.upper()} {path} stays monolith-owned")

    ok(cut.get("/api/board").status_code == 422,
       "explicit project is required")
    ok(cut.get("/api/board", params={"project": "missing"}).status_code == 400,
       "unknown project fails closed")

    read_token = "ms105-read-token"
    other_token = "ms105-other-token"
    store.create_principal(
        kind="agent", display_name="Coord alpha reader", token=read_token,
        scopes=["read"], principal_id="agent-ms105-alpha", project=PROJECT,
    )
    store.create_principal(
        kind="agent", display_name="Coord beta reader", token=other_token,
        scopes=["read"], principal_id="agent-ms105-beta", project=OTHER,
    )
    os.environ["PM_AUTH_MODE"] = "required"
    ok(cut.get("/api/board", params={"project": PROJECT}).status_code == 401,
       "required mode rejects a missing bearer")
    ok(cut.get(
        "/api/board", params={"project": PROJECT},
        headers={"Authorization": f"Bearer {read_token}"},
    ).status_code == 200, "project-scoped read bearer succeeds")
    cut_cross_project = cut.get(
        "/api/board", params={"project": PROJECT},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    # The monolith's global auth gate uses the same authenticate_request path
    # and reports an out-of-project bearer as unauthorized (401), before its
    # otherwise-public board router runs.
    ok(cut_cross_project.status_code == 401,
       "cross-project bearer preserves monolith auth behavior")
    os.environ["PM_AUTH_MODE"] = "dev-open"

    unit = ROOT / "deploy" / "coord" / "switchboard-coord.service.example"
    fragment = ROOT / "deploy" / "skeleton" / "Caddyfile.coord-fragment.example"
    live_caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    ok(unit.is_file(), "side-by-side systemd example exists")
    unit_text = unit.read_text(encoding="utf-8")
    ok("switchboard.services.coord.app:create_app" in unit_text,
       "systemd example runs the Coord factory")
    ok("8123" in unit_text, "systemd example binds port 8123")
    ok(fragment.is_file() and "8123" in fragment.read_text(encoding="utf-8"),
       "future Caddy fragment is retained as a commented reference")
    ok("127.0.0.1:8123" in live_caddy,
       "successor ARCH-MS-106 promotes the proven service to live Caddy")
    ok(CoordServiceSettings.from_env().port == 8123,
       "default Coord port is 8123")

    print(f"\nARCH-MS-105 Coord service: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
finally:
    shutil.rmtree(TMP, ignore_errors=True)
