#!/usr/bin/env python3
"""Focused proof for ARCH-MS-38 register_agent / register_host application commands."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from path_setup import ROOT, entrypoint_source  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms38-register-commands-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from switchboard.application.commands import (  # noqa: E402
    register_agent,
    register_host,
)
from switchboard.application.contracts.agents import (  # noqa: E402
    RegisterAgentCommand,
    RegisterHostCommand,
)
from switchboard.contracts import (  # noqa: E402
    REGISTER_AGENT_COMMAND_SCHEMA,
    REGISTER_HOST_COMMAND_SCHEMA,
    RegisterAgentCommand as WiredAgent,
    RegisterHostCommand as WiredHost,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    # ---- contracts normalize adapter inputs ---------------------------------
    agent_cmd = RegisterAgentCommand.from_mapping({
        "agent_id": " cursor/ARCH-1 ",
        "runtime": " cursor ",
        "project": " switchboard ",
        "model": " grok ",
        "lane": " ARCH-MS ",
        "task": "ARCH-MS-38",
        "ttl_seconds": "90",
        "control_json": '{"mode":"advisory_poll"}',
        "protocol_json": '{"version":"ixp.v1"}',
    })
    ok(agent_cmd.schema_id == REGISTER_AGENT_COMMAND_SCHEMA
       and agent_cmd.agent_id == "cursor/ARCH-1"
       and agent_cmd.runtime == "cursor"
       and agent_cmd.project == "switchboard"
       and agent_cmd.model == "grok"
       and agent_cmd.lane == "ARCH-MS"
       and agent_cmd.task_id == "ARCH-MS-38"
       and agent_cmd.ttl_s == 90
       and agent_cmd.control == {"mode": "advisory_poll"}
       and agent_cmd.protocol == {"version": "ixp.v1"},
       "RegisterAgentCommand normalizes aliases, whitespace, and JSON envelopes")

    host_cmd = RegisterHostCommand.from_mapping({
        "host_id": " host/alpha ",
        "project": " switchboard ",
        "hostname": " box-1 ",
        "repo_root": " /repo ",
        "agent_host_version": "0.2.0",
        "runtimes_json": '[{"runtime":"claude-code","lanes":["ARCH-MS"]}]',
        "limits_json": '{"max_sessions":2}',
        "capacity_json": '{"slots":1}',
        "ttl_s": "30",
        "active_sessions": "1",
    })
    inventory = host_cmd.to_inventory()
    ok(host_cmd.schema_id == REGISTER_HOST_COMMAND_SCHEMA
       and host_cmd.host_id == "host/alpha"
       and host_cmd.hostname == "box-1"
       and host_cmd.heartbeat_ttl_s == 30
       and host_cmd.active_sessions == 1
       and inventory["runtimes"] == [{"runtime": "claude-code", "lanes": ["ARCH-MS"]}]
       and inventory["limits"] == {"max_sessions": 2}
       and inventory["capacity"] == {"slots": 1}
       and inventory["active_sessions"] == 1,
       "RegisterHostCommand normalizes JSON aliases into host inventory")

    host_with_policy = register_host.execute_mapping_result(
        {
            "host_id": "host/policy",
            "project": "switchboard",
            "hostname": "box",
            "policy": {"mode": "lane_scoped", "allow_work": True},
            "runtimes": [{"runtime": "claude-code", "policy": {"mode": "lane_scoped"}}],
            "limits": {"max_sessions": 2},
            "heartbeat_ttl_s": 60,
        },
        actor="tester",
        principal_id="p1",
        register=lambda inventory, **kwargs: {
            "host_id": inventory["host_id"], "ok": True, "inventory": inventory, **kwargs,
        },
    )
    ok(host_with_policy.get("ok") is True
       and host_with_policy.get("host_id") == "host/policy"
       and "error" not in host_with_policy
       and "policy" not in (host_with_policy.get("inventory") or {}),
       "register_host ignores advisory top-level policy like store did")

    ok(WiredAgent.SCHEMA == REGISTER_AGENT_COMMAND_SCHEMA
       and WiredHost.SCHEMA == REGISTER_HOST_COMMAND_SCHEMA,
       "package contracts re-export register command schemas")

    # ---- validation fails closed before persistence -------------------------
    missing_agent = register_agent.execute_mapping_result(
        {"runtime": "cursor"}, actor="tester", principal_id="p1")
    ok(missing_agent.get("error_code") == "invalid_register_agent"
       and "agent_id" in (missing_agent.get("error") or ""),
       "register_agent requires agent_id before persistence")

    missing_runtime = register_agent.execute_mapping_result(
        {"agent_id": "agent/a"}, actor="tester", principal_id="p1")
    ok(missing_runtime.get("error_code") == "invalid_register_agent"
       and "runtime" in (missing_runtime.get("error") or ""),
       "register_agent requires runtime before persistence")

    missing_host = register_host.execute_mapping_result(
        {"project": "switchboard"}, actor="tester", principal_id="p1")
    ok(missing_host.get("error_code") == "invalid_register_host"
       and "host_id" in (missing_host.get("error") or ""),
       "register_host requires host_id before persistence")

    bad_control = register_agent.execute_mapping_result(
        {
            "agent_id": "agent/a",
            "runtime": "cursor",
            "control_json": "not-json",
        },
        actor="tester",
    )
    ok(bad_control.get("error_code") == "invalid_register_agent"
       and "control" in (bad_control.get("error") or "").lower(),
       "invalid control_json fails closed at the command layer")

    bad_runtimes = register_host.execute_mapping_result(
        {
            "host_id": "host/a",
            "runtimes_json": "not-json",
        },
        actor="tester",
    )
    ok(bad_runtimes.get("error_code") == "invalid_register_host"
       and "runtimes" in (bad_runtimes.get("error") or "").lower(),
       "invalid runtimes_json fails closed at the command layer")

    # ---- execute delegates to injected persistence --------------------------
    agent_calls = []

    def fake_register_agent(**kwargs):
        agent_calls.append(kwargs)
        return {"agent_id": kwargs["agent_id"], "ok": True}

    agent_result = register_agent.execute(
        RegisterAgentCommand.from_mapping({
            "agent_id": "agent/z",
            "runtime": "cursor",
            "model": "grok",
            "lane": "ARCH-MS",
            "task_id": "ARCH-MS-38",
            "ttl_s": 180,
            "control": {"mode": "advisory_poll"},
            "protocol": {"version": "ixp.v1"},
            "project": "switchboard",
        }),
        actor="tester",
        principal_id="principal-1",
        register=fake_register_agent,
    )
    ok(agent_result["ok"] is True
       and agent_calls
       and agent_calls[0]["agent_id"] == "agent/z"
       and agent_calls[0]["runtime"] == "cursor"
       and agent_calls[0]["actor"] == "tester"
       and agent_calls[0]["principal_id"] == "principal-1"
       and agent_calls[0]["ttl_s"] == 180
       and agent_calls[0]["control"] == {"mode": "advisory_poll"}
       and agent_calls[0]["protocol"] == {"version": "ixp.v1"}
       and agent_calls[0]["project"] == "switchboard",
       "register_agent forwards normalized fields to persistence")

    host_calls = []

    def fake_register_host(inventory, **kwargs):
        host_calls.append({"inventory": inventory, **kwargs})
        return {"host_id": inventory["host_id"], "ok": True}

    host_result = register_host.execute(
        RegisterHostCommand.from_mapping({
            "host_id": "host/z",
            "hostname": "box",
            "runtimes": [{"runtime": "codex"}],
            "limits": {"max_sessions": 1},
            "heartbeat_ttl_s": 45,
            "project": "switchboard",
        }),
        actor="tester",
        principal_id="principal-1",
        register=fake_register_host,
    )
    ok(host_result["ok"] is True
       and host_calls
       and host_calls[0]["inventory"]["host_id"] == "host/z"
       and host_calls[0]["inventory"]["runtimes"] == [{"runtime": "codex"}]
       and host_calls[0]["inventory"]["heartbeat_ttl_s"] == 45
       and host_calls[0]["actor"] == "tester"
       and host_calls[0]["principal_id"] == "principal-1"
       and host_calls[0]["project"] == "switchboard",
       "register_host forwards inventory to persistence")

    # ---- both adapters invoke the shared application handlers ---------------
    import mcp_server as mcp_mod  # noqa: E402
    from switchboard.api.routers import agents as agents_router  # noqa: E402
    from switchboard.mcp.tools import agents as agents_tools  # noqa: E402

    agents_router_src = (
        ROOT / "src/switchboard/api/routers/agents.py"
    ).read_text(encoding="utf-8")
    mcp_source = (ROOT / "src/switchboard/mcp/tools/agents.py").read_text(encoding="utf-8")
    ok("register_agent_command.execute_mapping_result" in agents_router_src
       and "register_host_command.execute_mapping_result" in agents_router_src
       and "register_agent_command.execute_mapping_result" in mcp_source
       and "register_host_command.execute_mapping_result" in mcp_source
       and "store.register_agent(" not in agents_router_src
       and "store.register_host(" not in agents_router_src
       and "store.register_agent(" not in mcp_source
       and "store.register_host(" not in mcp_source,
       "REST and MCP adapters invoke the same register commands")

    mcp_host = entrypoint_source("mcp_server")
    app_host = entrypoint_source("app")
    ok("register_agent_tools" in mcp_host
       and "def register_agent(" not in mcp_host
       and "def register_host(" not in mcp_host
       and "_create_agents_router" in app_host
       and "async def ixp_register_agent" not in app_host
       and "async def ixp_register_host" not in app_host,
       "monolith hosts register thin agent adapters only")

    body_helper = app_host.find("def _body_project(")
    # Prefer the include_router call site over the earlier import alias.
    agents_include = app_host.find("app.include_router(_create_agents_router(")
    ok(body_helper != -1 and agents_include != -1 and body_helper < agents_include,
       "_body_project is defined before the agents router is included")

    ok(hasattr(agents_router, "create_router"),
       "agents REST router module provides create_router")
    ok({"register_agent", "register_host"}.issubset(set(agents_tools.AGENT_TOOL_NAMES)),
       "agents MCP module registers register_agent and register_host")
    ok(callable(getattr(mcp_mod, "register_agent", None))
       and callable(getattr(mcp_mod, "register_host", None)),
       "mcp_server exports register_agent and register_host callables")

finally:
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
