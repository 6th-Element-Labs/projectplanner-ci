#!/usr/bin/env python3
"""BUG-36 — bulk deliverable links use one MCP call and survive contention."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import types


_TMP = tempfile.mkdtemp(prefix="bulk-deliverable-links-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_SQLITE_TIMEOUT_S"] = "0.02"
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
    store.create_project("Bulk Home", project_id="bulk-home", actor="test")
    store.create_project("Bulk Tasks", project_id="bulk-tasks", actor="test")
    deliverable = store.create_deliverable(
        {"id": "bulk-links", "title": "Bulk links"}, project="bulk-home")
    tasks = [
        store.create_task({"workstream_id": "BULK", "title": f"Task {index}"},
                          actor="test", project="bulk-tasks")
        for index in range(9)
    ]
    links = [{"task_project": "bulk-tasks", "task_id": task["task_id"],
              "role": "contributes"} for task in tasks]

    real_get_deliverable = store.get_deliverable
    store.get_deliverable = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("bulk write fetched full deliverable"))
    try:
        result = json.loads(mcp_server.link_tasks_to_deliverable(
            deliverable["id"], links, None, project="bulk-home"))
    finally:
        store.get_deliverable = real_get_deliverable

    counts = result.get("progress_counts") or {}
    check("nine links are accepted in one MCP call",
          len(result.get("linked") or []) == 9 and counts.get("requested") == 9)
    check("bulk acknowledgement is slim and complete",
          result.get("schema") == "switchboard.deliverable_task_links_ack.v1" and
          counts.get("linked") == 9 and counts.get("skipped") == 0 and
          counts.get("linked_task_count") == 9 and "task_links" not in result)

    bad = store.link_tasks_to_deliverable(deliverable["id"], [
        {"task_project": "bulk-tasks", "task_id": tasks[0]["task_id"]},
        {"task_project": "bulk-tasks", "task_id": "BULK-999"},
    ], project="bulk-home")
    with store._conn("bulk-home") as connection:
        persisted = connection.execute(
            "SELECT COUNT(*) FROM deliverable_task_links WHERE deliverable_id=?",
            (deliverable["id"],)).fetchone()[0]
    check("invalid batches fail atomically", bad.get("error") == "unknown linked task" and
          persisted == 9)

    duplicate = store.link_tasks_to_deliverable(deliverable["id"], [links[0], links[0]],
                                                project="bulk-home")
    check("exact duplicate input is reported as skipped",
          duplicate.get("progress_counts", {}).get("linked") == 1 and
          duplicate.get("progress_counts", {}).get("skipped") == 1 and
          duplicate.get("progress_counts", {}).get("linked_task_count") == 9)

    contended = store.create_deliverable(
        {"id": "contended-links", "title": "Contended links"}, project="bulk-home")
    home_db = store._resolve("bulk-home")["db"]
    locked = threading.Event()

    def hold_write_lock():
        connection = sqlite3.connect(home_db, timeout=1)
        connection.execute("BEGIN IMMEDIATE")
        locked.set()
        time.sleep(0.15)
        connection.commit()
        connection.close()

    locker = threading.Thread(target=hold_write_lock)
    locker.start()
    locked.wait(timeout=1)
    contended_result = json.loads(mcp_server.link_tasks_to_deliverable(
        contended["id"], links, None, project="bulk-home"))
    locker.join(timeout=1)
    check("one bulk call retries cleanly under write contention",
          contended_result.get("progress_counts", {}).get("linked") == 9 and
          contended_result.get("progress_counts", {}).get("linked_task_count") == 9)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
