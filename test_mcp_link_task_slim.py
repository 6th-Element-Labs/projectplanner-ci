#!/usr/bin/env python3
"""BUG-33 — MCP link writes default to a constant-size acknowledgement."""
import json
import os
import shutil
import sys
import tempfile
import types


_TMP = tempfile.mkdtemp(prefix="mcp-link-task-slim-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ.pop("PM_MCP_TOKEN", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _stub_heavy_imports():
    class _FastMCP:
        def __init__(self, *args, **kwargs):
            pass

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
            TransportSecuritySettings=type(
                "TSS", (), {"__init__": lambda self, *args, **kwargs: None}))
    _module("agent", _task_brief=lambda task, full=False: task,
            run=lambda *args, **kwargs: {}, _search_tasks=lambda *args, **kwargs: [])
    for name in ("digest", "intake", "notify", "rag", "signals"):
        _module(name)


_stub_heavy_imports()
import store
import mcp_server


passed = failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")


try:
    store.init_project_registry()
    store.create_project("MCP Link Home", project_id="mcp-link-home", actor="test")
    store.create_project("MCP Link Tasks", project_id="mcp-link-tasks", actor="test")
    deliverable = store.create_deliverable(
        {"id": "bulk-link", "title": "Bulk link"}, project="mcp-link-home")
    tasks = [
        store.create_task(
            {"workstream_id": "BULK", "title": f"Bulk task {index}"},
            actor="test", project="mcp-link-tasks")
        for index in range(9)
    ]

    real_get_deliverable = store.get_deliverable

    def forbidden_full_read(*args, **kwargs):
        raise AssertionError("default MCP link write fetched the full deliverable")

    store.get_deliverable = forbidden_full_read
    acknowledgements = []
    try:
        for task in tasks[:8]:
            result = json.loads(mcp_server.link_task_to_deliverable(
                deliverable["id"], "mcp-link-tasks", task["task_id"], None,
                project="mcp-link-home"))
            acknowledgements.append(result)
    finally:
        store.get_deliverable = real_get_deliverable

    check("default MCP writes never fetch the full deliverable",
          len(acknowledgements) == 8)
    check("default response is the versioned slim acknowledgement",
          all(row.get("schema") == "switchboard.deliverable_task_link_ack.v1" and
              row.get("linked") is True for row in acknowledgements))
    check("ack contains only the new link and count, not the growing rollup",
          all("task_links" not in row and "milestones" not in row and
              row.get("task_link", {}).get("task_id") == tasks[index]["task_id"] and
              row.get("progress", {}).get("linked_task_count") == index + 1
              for index, row in enumerate(acknowledgements)))
    sizes = [len(json.dumps(row, sort_keys=True)) for row in acknowledgements]
    check("bulk-link response size stays effectively constant",
          max(sizes) - min(sizes) < 10)

    full = json.loads(mcp_server.link_task_to_deliverable(
        deliverable["id"], "mcp-link-tasks", tasks[8]["task_id"], None,
        project="mcp-link-home", include_task_snapshots=True))
    check("explicit compatibility flag still returns the full decorated deliverable",
          len(full.get("task_links") or []) == 9 and
          all((link.get("task") or {}).get("title") for link in full["task_links"]))

    fetched = json.loads(mcp_server.get_deliverable(
        deliverable["id"], project="mcp-link-home"))
    check("get_deliverable remains the full-read tool",
          len(fetched.get("task_links") or []) == 9)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
