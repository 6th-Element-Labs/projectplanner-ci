#!/usr/bin/env python3
"""ARCH-MS-104 executable Coord independence verdict and auth boundary."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from path_setup import ROOT


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


spec = importlib.util.spec_from_file_location(
    "arch_ms104_coord_independence",
    ROOT / "scripts" / "arch_ms104_coord_independence.py",
)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

report = gate.evaluate(ROOT, run_probe=True)
ok(report.get("ok") is True, "executable independence artifact is internally consistent")
ok(report.get("verdict") == "nogo", "verdict is explicit No-Go")
ok(report.get("process_cut_authorized") is False, "No-Go cannot authorize process cut")
ok(report.get("failed_gates") == ["G1_ports_independence"],
   "only unresolved ports/root-import coupling blocks the cut")
ok((report.get("sqlite_probe") or {}).get("ok") is True,
   "WAL reader/writer contention probe passes")
ok((report.get("sqlite_probe") or {}).get("lock_errors") == 0,
   "WAL reader/writer contention has zero lock errors")

verdict = gate.load_verdict(ROOT / "docs" / "coord" / "coord_independence_verdict.json")
ok(verdict.get("writer_inventory") == [], "day-one Coord owns no writers")
ok("ARCH-MS-105" in (verdict.get("go_only_tasks") or []),
   "machine verdict gates the Go-only standalone service task")
ok((verdict.get("tasks_production_acceptance") or {}).get("green") is True,
   "Tasks production acceptance remains green")

# BUG-73: protocol routes bypass global auth, so delta must bind read authority in-handler.
from switchboard.api.routers import monitors as monitors_router  # noqa: E402

principal_calls: list[tuple[str, tuple[str, ...]]] = []


def resolve_project(project: str) -> str:
    if project not in {"alpha", "beta"}:
        raise HTTPException(400, "unknown project")
    return project


def resolve_principal(request, project: str, scopes=("write:ixp",), dev_actor="web"):
    authz = request.headers.get("authorization") or ""
    if not authz:
        raise HTTPException(401, "not authenticated")
    if authz != "Bearer alpha-read" or project != "alpha":
        raise HTTPException(403, "forbidden")
    principal_calls.append((project, tuple(scopes)))
    return {"principal_id": "arch-ms104", "scopes": list(scopes)}


original_delta = monitors_router.store.get_activity_delta
monitors_router.store.get_activity_delta = lambda since_cursor=0, lane="", project="": {
    "project": project, "cursor": since_cursor, "lane": lane, "updates": [],
}
try:
    app = FastAPI()
    app.include_router(monitors_router.create_router(
        resolve_project=resolve_project,
        resolve_principal=resolve_principal,
        resolve_body_project=lambda body: resolve_project(str(body.get("project") or "")),
    ))
    client = TestClient(app)
    missing = client.get("/ixp/v1/delta")
    unauth = client.get("/ixp/v1/delta", params={"project": "alpha"})
    cross_project = client.get(
        "/ixp/v1/delta", params={"project": "beta"},
        headers={"Authorization": "Bearer alpha-read"},
    )
    allowed = client.get(
        "/ixp/v1/delta", params={"project": "alpha", "lane": "ARCH-MS"},
        headers={"Authorization": "Bearer alpha-read"},
    )
finally:
    monitors_router.store.get_activity_delta = original_delta

ok(missing.status_code == 422, f"delta requires explicit project (got {missing.status_code})")
ok(unauth.status_code == 401, f"delta rejects missing bearer (got {unauth.status_code})")
ok(cross_project.status_code == 403,
   f"delta rejects bearer outside project scope (got {cross_project.status_code})")
ok(allowed.status_code == 200 and allowed.json().get("project") == "alpha",
   "delta accepts an authorized project-scoped read")
ok(principal_calls == [("alpha", ("read",))],
   f"delta binds exactly one read principal call ({principal_calls!r})")

print(f"\nARCH-MS-104 Coord independence: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
