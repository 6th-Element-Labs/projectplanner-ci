#!/usr/bin/env python3
"""Focused proof for the ARCH-MS-51 access/health/tally/agent route extraction."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT


TMP = tempfile.mkdtemp(prefix="arch-ms51-app-routers-")
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


ACCESS_PREFIX = "/api/access"
HEALTH_PATHS = {
    "/health",
    "/health/deep",
    "/health/saturation",
    "/api/saturation",
    "/api/narration/health",
    "/api/narration/narrate-now",
    "/api/narration/reactivate",
}
TALLY_PREFIX = "/tally/v1"
AGENT_IXP_PATHS = {
    "/ixp/v1/register_agent",
    "/ixp/v1/register_host",
    "/ixp/v1/heartbeat",
    "/ixp/v1/agents",
    "/ixp/v1/heartbeat_host",
    "/ixp/v1/agent_hosts",
    "/ixp/v1/control_plane_probe",
    "/ixp/v1/host_status",
}


try:
    access_endpoints = [
        route for route in expanded_routes(app.routes)
        if getattr(route, "path", "").startswith(ACCESS_PREFIX)
    ]
    ok(access_endpoints and all(
        route.endpoint.__module__ == "switchboard.api.routers.access"
        for route in access_endpoints
    ), "every /api/access endpoint is owned by switchboard.api.routers.access")

    health_endpoints = [
        route for route in expanded_routes(app.routes)
        if getattr(route, "path", "") in HEALTH_PATHS
    ]
    ok(len(health_endpoints) == len(HEALTH_PATHS) and all(
        route.endpoint.__module__ == "switchboard.api.routers.health"
        for route in health_endpoints
    ), "every health/saturation/narration endpoint is owned by switchboard.api.routers.health")

    tally_endpoints = [
        route for route in expanded_routes(app.routes)
        if getattr(route, "path", "").startswith(TALLY_PREFIX)
    ]
    ok(tally_endpoints and all(
        route.endpoint.__module__ == "switchboard.api.routers.tally"
        for route in tally_endpoints
    ), "every /tally/v1 endpoint is owned by switchboard.api.routers.tally")

    agent_endpoints = [
        route for route in expanded_routes(app.routes)
        if getattr(route, "path", "") in AGENT_IXP_PATHS
    ]
    ok(len(agent_endpoints) == len(AGENT_IXP_PATHS) and all(
        route.endpoint.__module__ == "switchboard.api.routers.agents"
        for route in agent_endpoints
    ), "agent/host IXP endpoints are owned by switchboard.api.routers.agents")

    app_impl_source = (ROOT / "app_impl.py").read_text(encoding="utf-8")
    duplicate_needles = (
        '@app.get("/api/access/',
        '@app.post("/api/access/',
        '@app.get("/health")',
        '@app.get("/health/deep")',
        '@app.get("/health/saturation")',
        '@app.get("/api/saturation")',
        '@app.get("/api/narration/',
        '@app.post("/api/narration/',
        '@app.get("/tally/v1/',
        '@app.post("/tally/v1/',
        '@app.patch("/tally/v1/',
        '@app.post("/ixp/v1/heartbeat")',
        '@app.get("/ixp/v1/agents")',
        '@app.get("/ixp/v1/agent_hosts")',
        '@app.get("/ixp/v1/host_status")',
        '@app.get("/ixp/v1/control_plane_probe")',
    )
    ok(all(needle not in app_impl_source for needle in duplicate_needles),
       "app_impl.py contains no duplicate access/health/tally/agent route decorators")

    ok("_create_access_router" in app_impl_source
       and "_create_health_router" in app_impl_source
       and "_create_tally_router" in app_impl_source
       and "app.include_router(_create_access_router" in app_impl_source
       and "app.include_router(_create_health_router" in app_impl_source
       and "app.include_router(_create_tally_router" in app_impl_source,
       "composition root mounts the extracted access/health/tally routers")

    client = TestClient(app)
    health = client.get("/health")
    ok(health.status_code == 200 and health.json().get("status") == "ok",
       "extracted /health liveness returns ok")

    access = client.get("/api/access/model", params={"project": "switchboard"})
    ok(access.status_code == 200 and "access" in access.json(),
       "extracted /api/access/model returns the access model")

    tally = client.get("/tally/v1/kpis", params={"project": "switchboard"})
    ok(tally.status_code == 200 and "kpis" in tally.json(),
       "extracted /tally/v1/kpis lists KPIs")

    agents = client.get("/ixp/v1/agents", params={"project": "switchboard"})
    ok(agents.status_code == 200 and "agents" in agents.json(),
       "extracted /ixp/v1/agents lists agents")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
