#!/usr/bin/env python3
"""ARCH-MS-19: board reads register through the packaged MCP adapter."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

from path_setup import ROOT
from switchboard.mcp.tools import board as board_tools


TMP = tempfile.mkdtemp(prefix="arch-ms19-board-tools-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"


def ok(condition, message):
    if not condition:
        raise AssertionError(message)
    print("  PASS  " + message)


BOARD_TOOLS = (
    "list_projects", "board_summary", "get_lane_delta", "get_plan_signals",
)

server_source = (ROOT / "mcp_server.py").read_text(encoding="utf-8")
adapter_path = ROOT / "src/switchboard/mcp/tools/board.py"
adapter_source = adapter_path.read_text(encoding="utf-8")

try:
    ok(adapter_path.is_file(), "board MCP adapter exists in the target package")
    ok("board_tools.register_board_tools(" in server_source,
       "mcp_server registers the packaged board tool set")
    for name in BOARD_TOOLS:
        ok(f"def {name}(" not in server_source,
           f"{name} implementation left the MCP monolith")
        ok(f"def {name}(" in adapter_source,
           f"{name} implementation lives in the board adapter")

    import mcp_server  # noqa: E402

    registered = set(mcp_server.mcp._tool_manager._tools)
    ok(set(BOARD_TOOLS).issubset(registered),
       "FastMCP exposes every packaged board tool")
    ok(all(getattr(mcp_server, name) is getattr(board_tools, name) for name in BOARD_TOOLS),
       "mcp_server retains direct-call compatibility aliases")

    projects = json.loads(mcp_server.list_projects())
    ok(projects.get("default") == "maxwell" and projects.get("projects"),
       "packaged list_projects keeps the routing contract")
    ok(mcp_server.board_summary(project="maxwell").startswith("Project: "),
       "packaged board_summary keeps the text contract")
    delta = json.loads(mcp_server.get_lane_delta(project="maxwell"))
    ok("cursor" in delta and "updates" in delta,
       "packaged get_lane_delta keeps the polling contract")
    plan_signals = json.loads(mcp_server.get_plan_signals(project="maxwell"))
    ok(isinstance(plan_signals.get("counts"), dict),
       "packaged get_plan_signals keeps the health contract")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("ARCH-MS-19 MCP board-tool extraction checks passed")
