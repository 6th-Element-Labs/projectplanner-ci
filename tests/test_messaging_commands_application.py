#!/usr/bin/env python3
"""Focused proof for ARCH-MS-37 messaging application commands."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT, entrypoint_source

TMP = tempfile.mkdtemp(prefix="arch-ms37-messaging-commands-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from switchboard.application.commands import (  # noqa: E402
    ack_message,
    send_agent_message,
)
from switchboard.application.contracts.messaging import (  # noqa: E402
    AckMessageCommand,
    SendAgentMessageCommand,
)
from switchboard.contracts import (  # noqa: E402
    ACK_MESSAGE_COMMAND_SCHEMA,
    SEND_AGENT_MESSAGE_COMMAND_SCHEMA,
    AckMessageCommand as WiredAck,
    SendAgentMessageCommand as WiredSend,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    # ---- contracts normalize adapter inputs ---------------------------------
    send_cmd = SendAgentMessageCommand.from_mapping({
        "from_agent": " agent/a ",
        "to": " agent/b ",
        "message": " hello ",
        "project": " switchboard ",
        "task": " ARCH-1 ",
        "requires_ack": "true",
        "ack_timeout_s": "90",
        "ack_timeout_action": "notify_sender",
        "signal": " heads_up ",
        "priority": "2",
        "idem_key": " idem-1 ",
    })
    ok(send_cmd.schema_id == SEND_AGENT_MESSAGE_COMMAND_SCHEMA
       and send_cmd.from_agent == "agent/a"
       and send_cmd.to_agent == "agent/b"
       and send_cmd.message == "hello"
       and send_cmd.project == "switchboard"
       and send_cmd.task_id == "ARCH-1"
       and send_cmd.requires_ack is True
       and send_cmd.ack_timeout_seconds == 90.0
       and send_cmd.on_ack_timeout == "notify_sender"
       and send_cmd.signal == "heads_up"
       and send_cmd.priority == 2
       and send_cmd.idem_key == "idem-1",
       "SendAgentMessageCommand normalizes aliases, whitespace, and timeouts")

    ack_cmd = AckMessageCommand.from_mapping({
        "id": "42",
        "project": " switchboard ",
        "response": " seen ",
    })
    ok(ack_cmd.schema_id == ACK_MESSAGE_COMMAND_SCHEMA
       and ack_cmd.message_id == 42
       and ack_cmd.project == "switchboard"
       and ack_cmd.response == "seen",
       "AckMessageCommand accepts id alias and strips text")

    ok(WiredSend.SCHEMA == SEND_AGENT_MESSAGE_COMMAND_SCHEMA
       and WiredAck.SCHEMA == ACK_MESSAGE_COMMAND_SCHEMA,
       "package contracts re-export messaging command schemas")

    extras_ok = SendAgentMessageCommand.from_mapping({
        "from_agent": "agent/a",
        "to_agent": "agent/b",
        "message": "hi",
        "project": "switchboard",
        "client_trace_id": "ignore-me",
        "nested": {"x": 1},
    })
    ok(extras_ok.message == "hi" and extras_ok.to_agent == "agent/b",
       "SendAgentMessageCommand ignores unknown adapter body keys")

    ack_extras = AckMessageCommand.from_mapping({
        "message_id": 3,
        "project": "switchboard",
        "response": "ok",
        "extra_flag": True,
    })
    ok(ack_extras.message_id == 3 and ack_extras.response == "ok",
       "AckMessageCommand ignores unknown adapter body keys")

    # ---- validation fails closed before persistence -------------------------
    missing_to = send_agent_message.execute_mapping_result(
        {"from_agent": "a", "message": "hi"}, principal_id="p1")
    ok(missing_to.get("error_code") == "invalid_send_agent_message"
       and "to_agent" in (missing_to.get("error") or ""),
       "send_agent_message requires to_agent before persistence")

    missing_message = send_agent_message.execute_mapping_result(
        {"from_agent": "a", "to_agent": "b"}, principal_id="p1")
    ok(missing_message.get("error_code") == "invalid_send_agent_message"
       and "message" in (missing_message.get("error") or ""),
       "send_agent_message requires message before persistence")

    missing_id = ack_message.execute_mapping_result(
        {"project": "switchboard"}, actor="tester")
    ok(missing_id.get("error_code") == "invalid_ack_message"
       and "message_id" in (missing_id.get("error") or ""),
       "ack_message requires message_id before persistence")

    # ---- execute delegates to injected persistence --------------------------
    send_calls = []

    def fake_send(**kwargs):
        send_calls.append(kwargs)
        return {"id": 7, **kwargs}

    send_result = send_agent_message.execute(
        SendAgentMessageCommand.from_mapping({
            "from_agent": "agent/z",
            "to_agent": "agent/y",
            "message": "ping",
            "project": "switchboard",
            "task_id": "ARCH-9",
            "requires_ack": True,
            "ack_deadline_minutes": 5,
            "ack_timeout_seconds": 2,
            "on_ack_timeout": "notify_sender",
            "signal": "redirect",
            "priority": 1,
            "idem_key": "idem-9",
        }),
        principal_id="principal-1",
        send=fake_send,
    )
    ok(send_result["id"] == 7
       and send_calls
       and send_calls[0]["from_agent"] == "agent/z"
       and send_calls[0]["to_agent"] == "agent/y"
       and send_calls[0]["message"] == "ping"
       and send_calls[0]["task_id"] == "ARCH-9"
       and send_calls[0]["requires_ack"] is True
       and send_calls[0]["ack_deadline_minutes"] == 5.0
       and send_calls[0]["ack_timeout_seconds"] == 2.0
       and send_calls[0]["principal_id"] == "principal-1"
       and send_calls[0]["idem_key"] == "idem-9"
       and send_calls[0]["project"] == "switchboard",
       "send_agent_message forwards normalized fields to persistence")

    ack_calls = []

    def fake_ack(message_id, **kwargs):
        ack_calls.append((message_id, kwargs))
        return {"id": message_id, "acked": True, **kwargs}

    ack_result = ack_message.execute(
        AckMessageCommand.from_mapping({
            "message_id": 9,
            "response": "ok",
            "project": "switchboard",
        }),
        actor="tester",
        ack=fake_ack,
    )
    ok(ack_result["acked"] is True
       and ack_calls
       and ack_calls[0][0] == 9
       and ack_calls[0][1]["actor"] == "tester"
       and ack_calls[0][1]["response"] == "ok"
       and ack_calls[0][1]["project"] == "switchboard",
       "ack_message forwards response and actor to persistence")

    # ---- both adapters invoke the shared application handlers ---------------
    messaging_router = (
        ROOT / "src/switchboard/api/routers/messaging.py"
    ).read_text(encoding="utf-8")
    mcp_source = (ROOT / "src/switchboard/mcp/tools/messaging.py").read_text(
        encoding="utf-8")
    ok("send_agent_message_command.execute_mapping_result" in messaging_router
       and "ack_message_command.execute_mapping_result" in messaging_router
       and "send_agent_message_command.execute_mapping_result" in mcp_source
       and "ack_message_command.execute_mapping_result" in mcp_source
       and "store.send_agent_message(" not in messaging_router
       and "store.ack_message(" not in messaging_router
       and "store.send_agent_message(" not in mcp_source
       and "store.ack_message(" not in mcp_source
       and 'body.get("from_agent") or auth.actor(principal)' in messaging_router
       and "setdefault(\"from_agent\"" not in messaging_router,
       "REST and MCP adapters invoke the same messaging commands")

    mcp_host = entrypoint_source("mcp_server")
    app_host = entrypoint_source("app")
    ok("register_messaging_tools" in mcp_host
       and "def send_agent_message(" not in mcp_host
       and "def ack_message(" not in mcp_host
       and "_create_messaging_router" in app_host
       and "async def api_send_agent_message" not in app_host
       and "async def api_ack_message" not in app_host
       and "async def ixp_send" not in app_host
       and "async def ixp_ack" not in app_host,
       "monolith hosts register thin messaging adapters only")

    body_helper = app_host.find("def _body_project(")
    messaging_include = app_host.find("_create_messaging_router(")
    ok(body_helper != -1 and messaging_include != -1 and body_helper < messaging_include,
       "_body_project is defined before the messaging router is included")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
