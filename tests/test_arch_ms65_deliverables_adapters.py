#!/usr/bin/env python3
"""Focused proof for ARCH-MS-65 deliverables/mission REST+MCP adapter drain."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="arch-ms65-adapters-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from path_setup import ROOT, entrypoint_source  # noqa: E402
from switchboard.application.commands import create_deliverable as create_deliverable_command  # noqa: E402
from switchboard.mcp.tools import deliverables as deliverable_tools  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


EXTRACTED_MCP = {
    "create_deliverable": deliverable_tools,
    "get_deliverable": deliverable_tools,
    "list_deliverables": deliverable_tools,
    "add_deliverable_milestone": deliverable_tools,
    "link_task_to_deliverable": deliverable_tools,
    "link_tasks_to_deliverable": deliverable_tools,
    "create_board": deliverable_tools,
    "create_mission": deliverable_tools,
    "unlink_task_from_deliverable": deliverable_tools,
    "get_mission_status": deliverable_tools,
    "get_deliverable_dependency_graph": deliverable_tools,
    "mission_status": deliverable_tools,
    "update_mission_narrative": deliverable_tools,
    "verify_deliverable_closure": deliverable_tools,
    "get_deliverable_closure_report": deliverable_tools,
    "request_deliverable_closure_verification": deliverable_tools,
    "generate_mission_brief": deliverable_tools,
    "run_mission_coordinator": deliverable_tools,
    "get_mission_brief": deliverable_tools,
    "propose_deliverable_breakdown": deliverable_tools,
    "approve_deliverable_breakdown": deliverable_tools,
    "submit_deliverable_outcome": deliverable_tools,
    "get_deliverable_breakdown_proposal": deliverable_tools,
    "list_deliverable_breakdown_proposals": deliverable_tools,
    "update_deliverable_breakdown_proposal": deliverable_tools,
    "reject_deliverable_breakdown": deliverable_tools,
    "defer_deliverable_breakdown": deliverable_tools,
}

REST_RESIDUALS = (
    "async def create_deliverable",
    "async def list_deliverable_breakdown_proposals",
    "def mission_status_query",
    "async def verify_deliverable_closure_route",
    "async def defer_deliverable_breakdown",
)

DELIVERABLE_PATHS = {
    "/api/deliverables",
    "/api/deliverables/breakdown_proposals",
    "/api/deliverables/breakdown_proposals/{proposal_id}",
    "/api/deliverables/{deliverable_id}",
    "/api/mission_status",
    "/api/deliverables/{deliverable_id}/mission_status",
    "/api/deliverables/{deliverable_id}/dependency_graph",
    "/api/deliverables/{deliverable_id}/closure_verify",
    "/api/deliverables/{deliverable_id}/closure_report",
    "/api/deliverables/{deliverable_id}/closure_request",
    "/api/deliverables/{deliverable_id}/coordinator_tick",
    "/api/deliverables/{deliverable_id}/mission_brief",
    "/api/deliverables/{deliverable_id}/narrative",
    "/api/deliverables/{deliverable_id}/breakdown_proposals",
    "/api/deliverables/breakdown_proposals/{proposal_id}/approve",
    "/api/deliverables/{deliverable_id}/outcome",
    "/api/deliverables/{deliverable_id}/archive",
    "/api/deliverables/breakdown_proposals/{proposal_id}/reject",
    "/api/deliverables/breakdown_proposals/{proposal_id}/defer",
}


def expanded_routes(routes):
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from expanded_routes(included.routes)
        else:
            yield route


try:
    server_source = entrypoint_source("mcp_server")
    ok("deliverable_tools.register_deliverable_tools(" in server_source,
       "mcp_server registers deliverables tool module")

    impl_source = (ROOT / "mcp_server_impl.py").read_text(encoding="utf-8")
    for name in EXTRACTED_MCP:
        ok(f"def {name}(" not in impl_source,
           f"{name} implementation left the MCP residual")
    ok("import deliverable_closure" not in impl_source,
       "mcp_server_impl no longer imports deliverable_closure directly")

    app_source = (ROOT / "app_impl.py").read_text(encoding="utf-8")
    ok("_create_deliverables_router(" in app_source
       and "app.include_router(_create_deliverables_router" in app_source,
       "app_impl mounts deliverables router")
    for needle in REST_RESIDUALS:
        ok(needle not in app_source, f"{needle} left the REST residual")
    ok("import deliverable_closure" not in app_source,
       "app_impl no longer imports deliverable_closure directly")

    router_source = (
        ROOT / "src/switchboard/api/routers/deliverables.py"
    ).read_text(encoding="utf-8")
    mcp_source = (
        ROOT / "src/switchboard/mcp/tools/deliverables.py"
    ).read_text(encoding="utf-8")
    ok("create_deliverable_command.execute_mapping_result" in router_source,
       "REST create_deliverable uses shared command")
    ok("create_deliverable_command.execute_mapping_result" in mcp_source,
       "MCP create_deliverable uses shared command")
    breakdown_idx = router_source.find('@router.get("/api/deliverables/breakdown_proposals")')
    deliverable_idx = router_source.find('@router.get("/api/deliverables/{deliverable_id}")')
    ok(breakdown_idx != -1 and deliverable_idx != -1 and breakdown_idx < deliverable_idx,
       "breakdown_proposals routes registered before /{deliverable_id}")

    cmd_source = (
        ROOT / "src/switchboard/application/commands/create_deliverable.py"
    ).read_text(encoding="utf-8")
    ok("store.create_deliverable" in cmd_source,
       "create_deliverable command delegates to store.create_deliverable")

    from app import app  # noqa: E402

    mounted = {getattr(route, "path", "") for route in expanded_routes(app.routes)}
    ok(DELIVERABLE_PATHS.issubset(mounted),
       "deliverables router paths are mounted on the FastAPI app")

    import mcp_server  # noqa: E402
    import store  # noqa: E402

    store.init_db("switchboard")
    registered = set(mcp_server.mcp._tool_manager._tools)
    ok(set(EXTRACTED_MCP).issubset(registered),
       "FastMCP exposes every extracted deliverable/mission tool")
    ok(all(getattr(mcp_server, name) is getattr(mod, name)
           for name, mod in EXTRACTED_MCP.items()),
       "mcp_server retains direct-call compatibility aliases")

    listed = json.loads(mcp_server.list_deliverables(project="switchboard"))
    ok(isinstance(listed.get("deliverables"), list),
       "list_deliverables returns a JSON object with deliverables list")

    created = create_deliverable_command.execute_mapping_result(
        {"title": "ARCH-MS-65 smoke"},
        actor="arch-ms-65-test",
        project="switchboard",
    )
    ok(bool(created.get("id")) and not created.get("error"),
       "create_deliverable command persists via store")

    app_lines = sum(1 for _ in (ROOT / "app_impl.py").open(encoding="utf-8"))
    mcp_lines = sum(1 for _ in (ROOT / "mcp_server_impl.py").open(encoding="utf-8"))
    ok(app_lines < 1309, f"app_impl residual shrank this PR ({app_lines} < 1309)")
    ok(mcp_lines < 1589, f"mcp_server_impl residual shrank this PR ({mcp_lines} < 1589)")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
