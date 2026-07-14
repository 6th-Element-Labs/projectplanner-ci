#!/usr/bin/env python3
"""Focused proof for ARCH-MS-66 work-session REST+MCP co-drain."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

TMP = tempfile.mkdtemp(prefix="arch-ms66-work-sessions-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from switchboard.application.commands import work_sessions as work_session_commands  # noqa: E402
from switchboard.mcp.tools import work_sessions as work_session_tools  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def expanded_routes(routes):
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from expanded_routes(included.routes)
        else:
            yield route


WORK_SESSION_PATHS = {
    "/ixp/v1/work_sessions",
    "/ixp/v1/managed_work_sessions",
    "/ixp/v1/work_sessions/{work_session_id}",
    "/ixp/v1/work_sessions/{work_session_id}/health",
    "/ixp/v1/session_health",
    "/ixp/v1/work_sessions/{work_session_id}/archive_workspace",
    "/ixp/v1/repo_preflight",
    "/ixp/v1/work_sessions/{work_session_id}/preflight",
    "/ixp/v1/pre_tool_check",
}

MUTATING_COMMANDS = (
    "create",
    "create_managed",
    "update",
    "preflight",
    "archive",
)


try:
    app_impl_source = (ROOT / "app_impl.py").read_text(encoding="utf-8")
    router_source = (
        ROOT / "src/switchboard/api/routers/ixp_work_sessions.py"
    ).read_text(encoding="utf-8")
    mcp_source = (
        ROOT / "src/switchboard/mcp/tools/work_sessions.py"
    ).read_text(encoding="utf-8")

    ok("from switchboard.api.routers.ixp_work_sessions import create_router"
       in app_impl_source
       or "ixp_work_sessions import create_router" in app_impl_source,
       "app_impl imports packaged ixp_work_sessions router")
    ok("app.include_router(_create_ixp_work_sessions_router(" in app_impl_source,
       "app_impl mounts ixp_work_sessions router")
    ok("async def ixp_create_work_session(" not in app_impl_source
       and "async def ixp_preflight_work_session(" not in app_impl_source
       and "async def ixp_pre_tool_check(" not in app_impl_source,
       "work-session IXP handlers left app_impl residual")

    endpoints = [
        route for route in expanded_routes(app.routes)
        if getattr(route, "path", "") in WORK_SESSION_PATHS
    ]
    ok(len(endpoints) >= len(WORK_SESSION_PATHS) and all(
        route.endpoint.__module__ == "switchboard.api.routers.ixp_work_sessions"
        for route in endpoints
    ), "every work-session IXP endpoint is owned by packaged router")

    for name in MUTATING_COMMANDS:
        ok(f"work_session_commands.{name}(" in router_source,
           f"REST adapter calls work_session_commands.{name}")
        ok(hasattr(work_session_commands, name),
           f"application command {name} is exported")

    ok("work_session_commands.create(" in mcp_source
       and "work_session_commands.update(" in mcp_source
       and "work_session_commands.preflight(" in mcp_source
       and "work_session_commands.archive(" in mcp_source,
       "MCP tools call application commands for mutations")
    ok("store.create_work_session(" not in mcp_source
       and "store.update_work_session(" not in mcp_source
       and "store.preflight_work_session(" not in mcp_source
       and "store.archive_work_session_workspace(" not in mcp_source,
       "MCP mutating tools no longer call store policy helpers directly")

    client = TestClient(app)
    listed = client.get("/ixp/v1/work_sessions", params={"project": "switchboard"})
    ok(listed.status_code == 200 and isinstance(listed.json().get("work_sessions"), list),
       "GET /ixp/v1/work_sessions still works through packaged router")

    health = client.get("/ixp/v1/session_health", params={"project": "switchboard"})
    health_body = health.json() if health.status_code == 200 else {}
    ok(health.status_code == 200 and "session_health" in health_body,
       "GET /ixp/v1/session_health still works through packaged router")

    created = work_session_commands.create(
        {
            "agent_id": "test/arch-ms-66",
            "repo_role": "canonical",
            "branch": "cursor/ARCH-MS-66-co-drain-work-sessions",
            "storage_mode": "external",
            "status": "active",
            "dirty_status": "clean",
            "policy_profile": "docs_review",
        },
        actor="test/arch-ms-66",
        principal_id="test-arch-ms-66",
        project="switchboard",
    )
    ok(bool(created.get("created") or created.get("work_session_id")
            or (created.get("work_session") or {}).get("work_session_id")),
       "application create command persists a work session")

    ok(hasattr(work_session_tools, "register_work_session_tools"),
       "MCP module still exposes register_work_session_tools")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
