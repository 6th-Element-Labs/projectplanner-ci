#!/usr/bin/env python3
"""UI-58: the deliverable Autopilot command service, and REST<->MCP parity.

The deliverable Start/Pause/Resume/Stop controls had a REST surface calling
``store.*`` directly and NO command layer or MCP tool, so "displays the same
state returned through MCP" was unprovable. This adds one service both
transports adapt to, and proves they answer identically. Assertions are pinned
to the REAL store contract (SUPPORTED_RUNTIMES, the actual start/pause/resume/
stop transitions, the actual error strings) — not to the command's own success.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="ui58-autopilot-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

from path_setup import ROOT  # noqa: E402,F401

import store  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from switchboard.application.commands import autopilot  # noqa: E402
from switchboard.storage.repositories import autopilot_scopes  # noqa: E402
from switchboard.mcp import authorization as mcp_authorization  # noqa: E402
from switchboard.mcp.tools import autopilot as mcp_autopilot  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


class _StubMcp:
    def tool(self):
        return lambda function: function


def _expand(routes):
    for route in routes:
        inc = getattr(route, "original_router", None)
        if inc is not None:
            yield from _expand(inc.routes)
        else:
            yield route


VOLATILE = ("created_at", "updated_at", "last_tick_at")


def stable(value):
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


def seed():
    store.init_project_registry()
    store.init_db(P)
    store.create_task({"workstream_id": "UI", "title": "autopilot target task"},
                      actor="ui58-test", project=P)
    store.create_deliverable({
        "id": "ui58-deliv", "title": "UI-58 autopilot",
        "status": "approved", "end_state": "drains"}, actor="ui58-test", project=P)
    store.link_task_to_deliverable(
        "ui58-deliv", P, "UI-1",
        data={"role": "contributes", "blocks_deliverable": True},
        actor="ui58-test", project=P)


try:
    seed()
    client = TestClient(__import__("app").app)
    mcp_autopilot.register_autopilot_tools(
        _StubMcp(),
        mcp_autopilot.AutopilotToolServices(
            dumps=lambda v: json.dumps(v, sort_keys=True, default=str),
            require_write=lambda ctx, project, scopes=None: {
                "id": "user-ui58", "display_name": "web"}))

    # ---- 1) the command surface exists on both transports -------------------
    ok(set(autopilot.COMMANDS) == {"get_autopilot", "control_autopilot"},
       "the service declares get_autopilot + control_autopilot")
    ok(set(mcp_autopilot.AUTOPILOT_TOOL_NAMES) == {"get_autopilot", "control_autopilot"},
       "MCP registers a tool for each command")
    routes = {(m, r.path) for r in _expand(client.app.routes) if hasattr(r, "path")
              for m in (getattr(r, "methods", None) or [])}
    for method, path in (
        ("GET", "/api/deliverables/{deliverable_id}/autopilot"),
        ("POST", "/api/deliverables/{deliverable_id}/autopilot"),
        ("POST", "/api/deliverables/{deliverable_id}/tasks/{task_id}/autopilot"),
    ):
        ok((method, path) in routes, f"REST exposes {method} {path}")
    census = mcp_authorization.READ_TOOLS | mcp_authorization.WRITE_TOOLS
    ok("get_autopilot" in mcp_authorization.READ_TOOLS,
       "get_autopilot is a read tool in the census")
    ok("control_autopilot" in mcp_authorization.WRITE_TOOLS,
       "control_autopilot is a write tool in the census")

    # ---- 2) start creates a real active scope (store contract) --------------
    started = autopilot.control_autopilot(
        "ui58-deliv", project=P, action="start", scope_type="deliverable",
        runtime="codex", actor="ui58-test")
    scope = started.get("scope") or {}
    ok(scope.get("schema") == autopilot_scopes.AUTOPILOT_SCOPE_SCHEMA,
       "start returns the real autopilot_scope.v1 row")
    ok(scope.get("status") == "active" and scope.get("scope_type") == "deliverable",
       "the started deliverable scope is active")
    ok(started.get("action") == "start" and started.get("command") == "control_autopilot",
       "the start envelope names the command and action")

    # ---- 3) pause/resume/stop map to the REAL store transitions -------------
    paused = autopilot.control_autopilot(
        "ui58-deliv", project=P, action="pause", actor="ui58-test")
    ok((paused.get("scope") or {}).get("status") == "paused",
       "pause moves the scope to paused (real store transition)")
    resumed = autopilot.control_autopilot(
        "ui58-deliv", project=P, action="resume", actor="ui58-test")
    ok((resumed.get("scope") or {}).get("status") == "active",
       "resume moves it back to active")
    stopped = autopilot.control_autopilot(
        "ui58-deliv", project=P, action="stop", actor="ui58-test")
    ok((stopped.get("scope") or {}).get("status") == "stopped",
       "stop moves it to stopped")

    # ---- 4) typed errors pinned to the real store failures ------------------
    unknown = autopilot.execute_mapping_result(
        "control_autopilot", "no-such-deliv", project=P, action="start",
        actor="ui58-test")
    ok(unknown.get("error_code") == "deliverable_not_found"
       and autopilot.error_status(unknown) == 404,
       "an unknown deliverable is a typed 404, not a bare store string")
    bad_runtime = autopilot.execute_mapping_result(
        "control_autopilot", "ui58-deliv", project=P, action="start",
        runtime="gpt-9-turbo", actor="ui58-test")
    ok(bad_runtime.get("error_code") == "invalid_input"
       and "codex" in (bad_runtime.get("supported_runtimes") or []),
       "an unsupported runtime carries the store's real supported_runtimes list")
    bad_action = autopilot.execute_mapping_result(
        "control_autopilot", "ui58-deliv", project=P, action="detonate",
        actor="ui58-test")
    ok(bad_action.get("error_code") == "invalid_input",
       "an action outside start/pause/resume/stop is rejected before the store")
    no_scope = autopilot.execute_mapping_result(
        "control_autopilot", "ui58-deliv", project=P, action="pause",
        actor="ui58-test")
    ok(no_scope.get("error_code") == "no_active_scope"
       and autopilot.error_status(no_scope) == 409,
       "controlling a deliverable with no live scope is a typed 409")
    ok(set(autopilot.ERROR_FAILURE_CLASS) == set(autopilot.ERROR_STATUS),
       "every error code has both an HTTP status and a fail_fix failure_class")

    # ---- 5) task scope: start, control, and not-linked refusal --------------
    task_started = autopilot.control_autopilot(
        "ui58-deliv", project=P, action="start", scope_type="task",
        task_project=P, task_id="UI-1", runtime="codex", actor="ui58-test",
        task_starter=lambda *_args, **_kwargs: {
            "schema": "switchboard.task_execution.v1", "command": "start_task",
            "started": True, "action": "started", "wake_id": "wake-ui58",
        })
    ok((task_started.get("scope") or {}).get("scope_type") == "task"
       and (task_started.get("scope") or {}).get("task_id") == "UI-1",
       "a task scope starts against a linked task")
    ok((task_started.get("task_start") or {}).get("command") == "start_task",
       "task Autopilot Start crosses the audited start_task boundary")
    not_linked = autopilot.execute_mapping_result(
        "control_autopilot", "ui58-deliv", project=P, action="start",
        scope_type="task", task_project=P, task_id="UI-999", actor="ui58-test")
    ok(not_linked.get("error_code") in {"task_not_linked", "deliverable_not_found"},
       "starting a task scope on an unlinked task is a typed refusal")

    # ---- 6) REST and MCP return byte-identical bodies -----------------------
    def rest(method, path, **kw):
        r = getattr(client, method)(path, **kw)
        return r.status_code, r.json()

    def mcp(raw):
        return json.loads(raw)

    # A read of the same underlying scope: identical rows, only clocks differ.
    autopilot.control_autopilot("ui58-deliv", project=P, action="start",
                                scope_type="deliverable", actor="ui58-test")
    status_code, rest_get = rest(
        "get", "/api/deliverables/ui58-deliv/autopilot", params={"project": P})
    mcp_get = mcp(mcp_autopilot.get_autopilot("ui58-deliv", project=P))
    ok(stable(rest_get) == stable(mcp_get),
       "REST and MCP agree on get_autopilot")

    parity_cases = [
        ("unknown-deliverable-start",
         lambda: rest("post", "/api/deliverables/nope/autopilot",
                      params={"project": P}, json={"action": "start"}),
         lambda: mcp(mcp_autopilot.control_autopilot(
             "nope", None, project=P, action="start"))),
        ("bad-runtime-start",
         lambda: rest("post", "/api/deliverables/ui58-deliv/autopilot",
                      params={"project": P},
                      json={"action": "start", "runtime": "gpt-9-turbo"}),
         lambda: mcp(mcp_autopilot.control_autopilot(
             "ui58-deliv", None, project=P, action="start", runtime="gpt-9-turbo"))),
    ]
    for name, rest_call, mcp_call in parity_cases:
        code, rest_body = rest_call()
        # Refusals use JSONResponse (not HTTPException), so the typed envelope is
        # the top-level body, never wrapped under "detail" — byte-identical to MCP.
        mcp_body = mcp_call()
        ok(stable(rest_body) == stable(mcp_body),
           f"REST and MCP agree on {name}")
        ok(code == autopilot.error_status(mcp_body),
           f"{name} maps its typed error to the declared HTTP status ({code})")

    # ---- 7) the browser cannot smuggle a runner id / wake through here ------
    import inspect
    for name in ("get_autopilot", "control_autopilot"):
        params = set(inspect.signature(getattr(mcp_autopilot, name)).parameters)
        ok(not (params & {"runner_session_id", "host_id", "wake_id", "scope_id"}),
           f"MCP {name} takes no caller-chosen runner/host/wake/scope id")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nUI-58 autopilot commands: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
