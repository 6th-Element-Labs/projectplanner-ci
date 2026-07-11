#!/usr/bin/env python3
"""BUG-37 — MCP deliverable listing stays slim while detail remains explicit."""
import json
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="mcp-deliverables-slim-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class _FastMCP:
    def __init__(self, *args, **kwargs):
        self.settings = types.SimpleNamespace(host="127.0.0.1", port=8111, log_level="INFO")

    def tool(self, *args, **kwargs):
        return lambda fn: fn

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


_module("mcp")
_module("mcp.server")
_module("mcp.server.fastmcp", Context=object, FastMCP=_FastMCP)
_module("mcp.server.transport_security",
        TransportSecuritySettings=type("TSS", (), {"__init__": lambda self, *a, **k: None}))
_module("agent", _task_brief=lambda task, full=False: task,
        _search_tasks=lambda args, project="maxwell": [],
        board_summary_text=lambda project="maxwell": "", run=lambda *a, **k: {})
for dependency in ("digest", "intake", "notify", "rag", "signals"):
    _module(dependency)

import store  # noqa: E402
import mcp_server  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


try:
    store.create_project("MCP List Home", project_id="mcp-list-home", actor="test")
    store.create_project("MCP List Tasks", project_id="mcp-list-tasks", actor="test")
    task = store.create_task(
        {"workstream_id": "WORK", "title": "Linked task"},
        actor="test", project="mcp-list-tasks")
    deliverable = store.create_deliverable(
        {"id": "slim-list", "title": "Slim list"},
        actor="test", project="mcp-list-home")
    store.link_task_to_deliverable(
        deliverable["id"], "mcp-list-tasks", task["task_id"],
        actor="test", project="mcp-list-home")

    observed = []
    original_list = store.list_deliverables

    def recording_list(*args, **kwargs):
        observed.append(kwargs.get("include_task_snapshots"))
        return original_list(*args, **kwargs)

    store.list_deliverables = recording_list
    try:
        listed = json.loads(mcp_server.list_deliverables(project="mcp-list-home"))
    finally:
        store.list_deliverables = original_list

    row = listed["deliverables"][0]
    check(observed == [False], "MCP list explicitly selects the slim store path")
    check("task" not in row["task_links"][0], "MCP list omits linked-task snapshots")
    check(row["progress"]["status_counts"] == {"Not Started": 1},
          "MCP list retains truthful progress counts")

    detail = json.loads(mcp_server.get_deliverable(
        "slim-list", project="mcp-list-home"))
    check(detail["task_links"][0]["task"]["task_id"] == task["task_id"],
          "MCP get_deliverable retains the explicit full-detail path")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("MCP slim deliverables tests passed")
