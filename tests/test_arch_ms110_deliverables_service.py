#!/usr/bin/env python3
"""ARCH-MS-110: Deliverables :8124 service, parity, Auth, and boundary proof."""
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


TMP = tempfile.mkdtemp(prefix="arch-ms110-deliverables-service-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(Path(TMP) / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_JWT_SECRET"] = "test-secret-arch-ms110"
Path(os.environ["PM_DYNAMIC_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)

PROJECT = "ms110-alpha"
OTHER = "ms110-beta"
DELIVERABLE_ID = "ms110-deliverables-cut"
MISSION_ID = "ms110-mission"
EMPTY_MISSION_ID = "ms110-empty-mission"
DELIVERABLES_PACKAGE = ROOT / "src" / "switchboard" / "services" / "deliverables"
FORBIDDEN_ROOTS = frozenset({
    "app_impl",
    "auth",
    "deliverable_closure",
    "mcp_server",
    "mcp_server_impl",
    "store",
})
ADAPTER_ALLOW = {"switchboard.api.deliverables_port_adapters"}
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def forbidden_imports(path: Path, *, allow_adapter: bool = False) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in FORBIDDEN_ROOTS:
                    hits.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and not node.level:
            module = node.module or ""
            if allow_adapter and module in ADAPTER_ALLOW:
                continue
            if module.split(".", 1)[0] in FORBIDDEN_ROOTS:
                hits.append(f"from {module} import")
    return hits


def baseline_client() -> TestClient:
    from switchboard.api import deps
    from switchboard.api.routers import deliverables

    application = FastAPI(title="arch-ms110-monolith-baseline")
    application.include_router(deliverables.create_router(
        resolve_project=deps.resolve_project,
        resolve_principal=deps.resolve_principal,
        etag_json=deps.etag_json,
    ))
    return TestClient(application)


def parity(name: str, baseline_response, cut_response, *, etag: bool = False) -> None:
    ok(
        baseline_response.status_code == cut_response.status_code,
        f"{name} status parity {baseline_response.status_code}={cut_response.status_code}",
    )
    if baseline_response.status_code == 200:
        ok(baseline_response.json() == cut_response.json(), f"{name} response parity")
        if etag:
            ok(
                baseline_response.headers.get("etag")
                == cut_response.headers.get("etag")
                and bool(cut_response.headers.get("etag")),
                f"{name} source-revision ETag parity",
            )


try:
    for name in (
        "switchboard.services.deliverables",
        "switchboard.services.deliverables.settings",
        "switchboard.services.deliverables.health",
        "switchboard.services.deliverables.ports",
        "switchboard.services.deliverables.router",
        "switchboard.services.deliverables.app",
    ):
        try:
            importlib.import_module(name)
            ok(True, f"import {name}")
        except Exception as exc:
            ok(False, f"import {name}: {exc}")

    for path in sorted(DELIVERABLES_PACKAGE.glob("*.py")):
        hits = forbidden_imports(path, allow_adapter=path.name in {"app.py", "deps.py"})
        ok(
            not hits,
            f"{path.name}: no forbidden monolith imports"
            + (f" ({hits})" if hits else ""),
        )

    import store  # noqa: E402
    from switchboard.services.deliverables import create_app  # noqa: E402
    from switchboard.services.deliverables.settings import (  # noqa: E402
        DeliverablesServiceSettings,
    )

    store.init_project_registry()
    store.create_project("MS110 Alpha", project_id=PROJECT, actor="test")
    store.create_project("MS110 Beta", project_id=OTHER, actor="test")
    # Required-mode bearer lookup scans configured projects. Initialize all so a
    # negative Auth test fails on identity, never on an unrelated empty DB.
    for project_id in store.project_ids():
        store.init_db(project_id)

    mission = store.create_project_board({
        "id": MISSION_ID,
        "title": "MS110 mission",
        "kind": "mission",
        "status": "active",
        "purpose": "Deliverables service parity",
        "end_state": "A bounded read process on 8124",
    }, actor="test", project=PROJECT)
    ok(mission.get("id") == MISSION_ID, "mission fixture created")

    empty_mission = store.create_project_board({
        "id": EMPTY_MISSION_ID,
        "title": "MS110 empty mission",
        "kind": "mission",
        "status": "active",
        "purpose": "Negative mission-status parity",
        "end_state": "No deliverable has been created yet",
    }, actor="test", project=PROJECT)
    ok(empty_mission.get("id") == EMPTY_MISSION_ID,
       "empty mission fixture created")

    deliverable = store.create_deliverable({
        "id": DELIVERABLE_ID,
        "board_id": MISSION_ID,
        "title": "MS110 Deliverables cut",
        "status": "proposed",
        "end_state": "Read parity is exact",
        "acceptance_criteria": ["status, revision, and closure parity"],
    }, actor="test", project=PROJECT)
    ok(deliverable.get("id") == DELIVERABLE_ID, "deliverable fixture created")

    store.add_deliverable_milestone(
        DELIVERABLE_ID,
        {"id": "parity", "title": "Prove parity", "status": "in_progress"},
        actor="test",
        project=PROJECT,
    )
    proposal = store.propose_deliverable_breakdown(
        DELIVERABLE_ID,
        {
            "milestones": [{
                "title": "Cutover later",
                "tasks": [{
                    "project_id": OTHER,
                    "workstream_id": "ARCH-MS",
                    "title": "Future edge cut",
                }],
            }],
        },
        actor="test",
        project=PROJECT,
    )
    proposal_id = (proposal.get("proposal") or {}).get("id") or ""
    ok(bool(proposal_id), "breakdown proposal fixture created")

    closure = store.record_deliverable_closure(
        DELIVERABLE_ID,
        {
            "schema": "switchboard.deliverable_closure_report.v1",
            "report_id": "report-ms110-parity",
            "evidence_hash": "sha256:ms110-parity",
            "grade": "pass",
            "recommendation": "close",
            "generated_at": 1784300000.0,
        },
        actor="test-verifier",
        project=PROJECT,
    )
    ok(closure.get("ok") is True, "committed closure fixture created")

    baseline = baseline_client()
    settings = DeliverablesServiceSettings(
        service_name="arch-ms110-test", host="127.0.0.1", port=8124
    )
    cut = TestClient(create_app(settings))

    health = cut.get("/health")
    ok(health.status_code == 200, f"cut /health status {health.status_code}")
    ok(
        health.json() == {"status": "ok", "service": "arch-ms110-test"},
        "cut /health identifies Deliverables service",
    )
    ready = cut.get("/ready")
    ok(ready.status_code == 200, f"cut /ready status {ready.status_code}")
    ok(set((ready.json().get("checks") or {}).values()) == {"ok"},
       "cut /ready proves DB schema, browser Auth, and repository read")

    requests = [
        ("deliverables", "/api/deliverables", {}, False),
        ("deliverables picker", "/api/deliverables", {"view": "picker"}, False),
        ("deliverable", f"/api/deliverables/{DELIVERABLE_ID}", {}, False),
        ("mission status", "/api/mission_status", {"deliverable_id": DELIVERABLE_ID}, True),
        ("deliverable mission status", f"/api/deliverables/{DELIVERABLE_ID}/mission_status", {}, True),
        ("dependency graph", f"/api/deliverables/{DELIVERABLE_ID}/dependency_graph", {}, True),
        ("closure report", f"/api/deliverables/{DELIVERABLE_ID}/closure_report", {}, False),
        ("breakdown proposals", "/api/deliverables/breakdown_proposals", {}, False),
        ("breakdown proposal", f"/api/deliverables/breakdown_proposals/{proposal_id}", {}, False),
    ]
    for name, path, params, has_etag in requests:
        full_params = {"project": PROJECT, **params}
        parity(
            name,
            baseline.get(path, params=full_params),
            cut.get(path, params=full_params),
            etag=has_etag,
        )

    mission_response = cut.get(
        "/api/mission_status",
        params={"project": PROJECT, "deliverable_id": DELIVERABLE_ID},
    )
    mission_etag = mission_response.headers.get("etag") or ""
    unchanged = cut.get(
        "/api/mission_status",
        params={"project": PROJECT, "deliverable_id": DELIVERABLE_ID},
        headers={"If-None-Match": mission_etag},
    )
    ok(mission_etag and unchanged.status_code == 304,
       "mission revision binding preserves conditional 304")

    closure_body = cut.get(
        f"/api/deliverables/{DELIVERABLE_ID}/closure_report",
        params={"project": PROJECT},
    ).json()
    report = closure_body.get("report") or {}
    ok(
        report.get("report_id") == "report-ms110-parity"
        and report.get("evidence_hash") == "sha256:ms110-parity",
        "closure read preserves immutable report/revision identity",
    )

    ok(cut.get("/api/deliverables").status_code == 422,
       "explicit project is required")
    ok(cut.get("/api/deliverables", params={"project": "missing"}).status_code == 400,
       "unknown project fails closed")
    parity(
        "missing deliverable",
        baseline.get("/api/deliverables/missing-id", params={"project": PROJECT}),
        cut.get("/api/deliverables/missing-id", params={"project": PROJECT}),
    )
    empty_mission_params = {"project": PROJECT, "board_id": EMPTY_MISSION_ID}
    baseline_empty_mission = baseline.get(
        "/api/mission_status", params=empty_mission_params
    )
    cut_empty_mission = cut.get(
        "/api/mission_status", params=empty_mission_params
    )
    parity(
        "existing board without deliverable",
        baseline_empty_mission,
        cut_empty_mission,
    )
    ok(cut_empty_mission.status_code == 404,
       "existing board without deliverable preserves monolith 404")

    read_token = "ms110-alpha-read"
    other_token = "ms110-beta-read"
    denied_token = "ms110-alpha-no-read"
    store.create_principal(
        kind="agent", display_name="MS110 alpha reader", token=read_token,
        scopes=["read"], principal_id="agent-ms110-alpha", project=PROJECT,
    )
    store.create_principal(
        kind="agent", display_name="MS110 beta reader", token=other_token,
        scopes=["read"], principal_id="agent-ms110-beta", project=OTHER,
    )
    store.create_principal(
        kind="agent", display_name="MS110 alpha denied", token=denied_token,
        scopes=["write:tasks"], principal_id="agent-ms110-alpha-denied", project=PROJECT,
    )
    os.environ["PM_AUTH_MODE"] = "required"
    ok(cut.get("/api/deliverables", params={"project": PROJECT}).status_code == 401,
       "required mode rejects a missing bearer")
    ok(cut.get(
        "/api/deliverables",
        params={"project": PROJECT},
        headers={"Authorization": f"Bearer {read_token}"},
    ).status_code == 200, "project-scoped read bearer succeeds")
    ok(cut.get(
        "/api/deliverables",
        params={"project": PROJECT},
        headers={"Authorization": f"Bearer {denied_token}"},
    ).status_code == 403, "Bearer without read scope is denied")
    ok(cut.get(
        "/api/deliverables",
        params={"project": PROJECT},
        headers={"Authorization": f"Bearer {other_token}"},
    ).status_code == 401, "cross-project bearer fails closed")

    import auth as root_auth  # noqa: E402
    from switchboard.api.routers.auth import session as auth_session  # noqa: E402
    from switchboard.api.routers.auth import store as auth_store  # noqa: E402

    auth_store.init()
    browser_user = auth_store.create_user(
        "ms110-browser@test.com",
        "MS110 Browser",
        root_auth.password_hash("ms110-browser-password"),
    )
    store.grant_project_role(
        PROJECT, "user", browser_user["id"], "viewer",
        created_by="test", scopes=["read"],
    )
    browser_token, _ = auth_session.issue(browser_user)
    browser = TestClient(create_app(settings))
    browser.cookies.set(auth_session.COOKIE_NAME, browser_token)
    ok(browser.get(
        "/api/deliverables", params={"project": PROJECT}
    ).status_code == 200, "cookie-backed browser session preserves monolith read parity")
    ok(browser.get(
        "/api/deliverables", params={"project": OTHER}
    ).status_code == 403, "cookie session remains deny-by-default across projects")
    no_grant_user = auth_store.create_user(
        "ms110-no-grant@test.com", "MS110 No Grant",
        root_auth.password_hash("ms110-no-grant-password"),
    )
    no_grant_token, _ = auth_session.issue(no_grant_user)
    no_grant = TestClient(create_app(settings))
    no_grant.cookies.set(auth_session.COOKIE_NAME, no_grant_token)
    ok(no_grant.get(
        "/api/deliverables", params={"project": PROJECT}
    ).status_code == 403, "cookie without project grant fails closed")
    auth_session.revoke(browser_token)
    ok(browser.get(
        "/api/deliverables", params={"project": PROJECT}
    ).status_code == 401, "revoked/logout cookie fails closed")
    os.environ["PM_AUTH_MODE"] = "dev-open"

    for method, path in (
        ("post", "/api/deliverables"),
        ("post", f"/api/deliverables/{DELIVERABLE_ID}/closure_verify"),
        ("post", f"/api/deliverables/{DELIVERABLE_ID}/closure_request"),
        ("post", f"/api/deliverables/{DELIVERABLE_ID}/archive"),
        ("post", f"/api/deliverables/{DELIVERABLE_ID}/coordinator_tick"),
        ("patch", f"/api/deliverables/{DELIVERABLE_ID}/narrative"),
    ):
        response = getattr(cut, method)(path, params={"project": PROJECT}, json={})
        ok(response.status_code in {404, 405},
           f"non-chartered {method.upper()} {path} stays monolith-owned")

    unit = ROOT / "deploy" / "deliverables" / "switchboard-deliverables.service.example"
    fragment = ROOT / "deploy" / "skeleton" / "Caddyfile.deliverables-fragment.example"
    live_caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    ok(unit.is_file(), "side-by-side systemd example exists")
    unit_text = unit.read_text(encoding="utf-8")
    ok("switchboard.services.deliverables.app:create_app" in unit_text,
       "systemd example runs the Deliverables factory")
    ok("8124" in unit_text, "systemd example binds port 8124")
    fragment_text = fragment.read_text(encoding="utf-8") if fragment.is_file() else ""
    ok(fragment.is_file() and "8124" in fragment_text,
       "future Caddy fragment is retained as a commented reference")
    ok("method GET" in fragment_text and "@deliverables_day_one_reads" in fragment_text,
       "future edge fragment cannot route monolith-owned writers to the read service")
    # ARCH-MS-111 is the cutover successor: once present, the live fragment must
    # retain the same GET-only boundary ARCH-MS-110 prepared.
    live_cut_is_bounded = (
        "127.0.0.1:8124" not in live_caddy
        or ("@deliverables_day_one_reads" in live_caddy
            and "method GET" in live_caddy
            and "handle /api/deliverables*" not in live_caddy)
    )
    ok(live_cut_is_bounded,
       "live Caddy is absent or activated by the bounded cutover successor")
    ok(DeliverablesServiceSettings.from_env().port == 8124,
       "default Deliverables port is 8124")

    print(f"\nARCH-MS-110 Deliverables service: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
finally:
    shutil.rmtree(TMP, ignore_errors=True)
