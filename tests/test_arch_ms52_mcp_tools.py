#!/usr/bin/env python3
"""Focused proof for the ARCH-MS-52 leftover MCP tool drain."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

# Isolate DBs before any store/constants import binds path constants.
TMP = tempfile.mkdtemp(prefix="arch-ms52-mcp-tools-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from path_setup import ROOT, entrypoint_source  # noqa: E402
from switchboard.mcp.tools import agents as agent_tools  # noqa: E402
from switchboard.mcp.tools import claims as claim_tools  # noqa: E402
from switchboard.mcp.tools import leases as lease_tools  # noqa: E402
from switchboard.mcp.tools import resources as resource_tools  # noqa: E402
from switchboard.mcp.tools import tally as tally_tools  # noqa: E402
from switchboard.mcp.tools import wakes as wake_tools  # noqa: E402
from switchboard.mcp.tools import work_sessions as work_session_tools  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


EXTRACTED = {
    "claim_files": lease_tools,
    "release_files": lease_tools,
    "check_files": lease_tools,
    "list_active_leases": lease_tools,
    "create_work_session": work_session_tools,
    "pre_tool_check": work_session_tools,
    "claim_resource": resource_tools,
    "list_active_resource_leases": resource_tools,
    "report_usage": tally_tools,
    "get_kpi_tally": tally_tools,
    "heartbeat": agent_tools,
    "host_status": agent_tools,
    "list_wake_intents": wake_tools,
    "cancel_wake": wake_tools,
    "abandon_claim": claim_tools,
    "verify_offline_completion": claim_tools,
}

try:
    server_source = entrypoint_source("mcp_server")
    ok("lease_tools.register_lease_tools(" in server_source
       and "work_session_tools.register_work_session_tools(" in server_source
       and "resource_tools.register_resource_tools(" in server_source
       and "tally_tools.register_tally_tools(" in server_source,
       "mcp_server registers the new packaged tool modules")

    impl_source = (ROOT / "mcp_server_impl.py").read_text(encoding="utf-8")
    for name in EXTRACTED:
        ok(f"def {name}(" not in impl_source,
           f"{name} implementation left the MCP residual")

    import mcp_server  # noqa: E402

    registered = set(mcp_server.mcp._tool_manager._tools)
    ok(set(EXTRACTED).issubset(registered),
       "FastMCP exposes every extracted leftover tool")
    ok(all(getattr(mcp_server, name) is getattr(mod, name)
           for name, mod in EXTRACTED.items()),
       "mcp_server retains direct-call compatibility aliases")

    ok(json.loads(mcp_server.list_active_leases(project="switchboard")) == [],
       "packaged list_active_leases keeps the empty-list contract")
    ok(isinstance(json.loads(mcp_server.list_active_agents(project="switchboard")), list),
       "packaged list_active_agents keeps the list contract")
    ok(isinstance(json.loads(mcp_server.list_wake_intents(project="switchboard")), list),
       "packaged list_wake_intents keeps the list contract")
    ok(isinstance(json.loads(mcp_server.list_work_sessions(project="switchboard")), dict),
       "packaged list_work_sessions keeps the object contract")

    impl_lines = sum(1 for _ in (ROOT / "mcp_server_impl.py").open(encoding="utf-8"))
    ok(impl_lines < 2888,
       f"mcp_server_impl residual shrank this PR ({impl_lines} < 2888)")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
