#!/usr/bin/env python3
"""Focused proof for ARCH-MS-39 wake application commands."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT, entrypoint_source

TMP = tempfile.mkdtemp(prefix="arch-ms39-wake-commands-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from switchboard.application.commands import (  # noqa: E402
    claim_wake,
    complete_wake,
    request_wake,
)
from switchboard.application.contracts.wakes import (  # noqa: E402
    ClaimWakeCommand,
    CompleteWakeCommand,
    RequestWakeCommand,
)
from switchboard.contracts import (  # noqa: E402
    CLAIM_WAKE_COMMAND_SCHEMA,
    COMPLETE_WAKE_COMMAND_SCHEMA,
    REQUEST_WAKE_COMMAND_SCHEMA,
    ClaimWakeCommand as WiredClaim,
    CompleteWakeCommand as WiredComplete,
    RequestWakeCommand as WiredRequest,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    # ---- contracts normalize adapter inputs ---------------------------------
    request_cmd = RequestWakeCommand.from_mapping({
        "selector_json": '{"runtime":"claude-code","agent_id":"claude/a"}',
        "reason": " resume ",
        "source": " coordinator ",
        "policy_json": '{"prefer":"local"}',
        "task": " ARCH-MS-39 ",
        "idem_key": " idem-1 ",
        "project": " switchboard ",
    })
    ok(request_cmd.schema_id == REQUEST_WAKE_COMMAND_SCHEMA
       and request_cmd.selector == {"runtime": "claude-code", "agent_id": "claude/a"}
       and request_cmd.reason == "resume"
       and request_cmd.source == "coordinator"
       and request_cmd.policy == {"prefer": "local"}
       and request_cmd.task_id == "ARCH-MS-39"
       and request_cmd.idem_key == "idem-1"
       and request_cmd.project == "switchboard",
       "RequestWakeCommand normalizes JSON aliases, whitespace, and task alias")

    claim_cmd = ClaimWakeCommand.from_mapping({
        "host_id": " host/a ",
        "id": " wake-1 ",
        "project": " switchboard ",
    })
    ok(claim_cmd.schema_id == CLAIM_WAKE_COMMAND_SCHEMA
       and claim_cmd.host_id == "host/a"
       and claim_cmd.wake_id == "wake-1"
       and claim_cmd.project == "switchboard",
       "ClaimWakeCommand accepts wake_id alias `id` and strips text")

    complete_cmd = CompleteWakeCommand.from_mapping({
        "wake_id": " wake-9 ",
        "runner_session_id": " runner-1 ",
        "agent_id": " agent/z ",
        "result_json": '{"started":true,"pid":42}',
        "project": " switchboard ",
    })
    ok(complete_cmd.schema_id == COMPLETE_WAKE_COMMAND_SCHEMA
       and complete_cmd.wake_id == "wake-9"
       and complete_cmd.runner_session_id == "runner-1"
       and complete_cmd.agent_id == "agent/z"
       and complete_cmd.result == {"started": True, "pid": 42}
       and complete_cmd.project == "switchboard",
       "CompleteWakeCommand normalizes result_json and strips text")

    ok(WiredRequest.SCHEMA == REQUEST_WAKE_COMMAND_SCHEMA
       and WiredClaim.SCHEMA == CLAIM_WAKE_COMMAND_SCHEMA
       and WiredComplete.SCHEMA == COMPLETE_WAKE_COMMAND_SCHEMA,
       "package contracts re-export wake command schemas")

    # ---- validation fails closed before persistence -------------------------
    missing_selector = request_wake.execute_mapping_result(
        {"project": "switchboard"}, actor="tester", principal_id="p1")
    ok(missing_selector.get("error_code") == "invalid_request_wake"
       and "selector" in (missing_selector.get("error") or "").lower(),
       "request_wake requires selector before persistence")

    missing_runtime = request_wake.execute_mapping_result(
        {"selector": {"lane": "ARCH"}, "project": "switchboard"},
        actor="tester", principal_id="p1")
    ok(missing_runtime.get("error_code") == "invalid_request_wake"
       and "runtime" in (missing_runtime.get("error") or "").lower(),
       "request_wake requires selector.runtime or selector.agent_id")

    missing_host = claim_wake.execute_mapping_result(
        {"wake_id": "wake-1"}, actor="tester")
    ok(missing_host.get("error_code") == "invalid_claim_wake"
       and missing_host.get("claimed") is False
       and "host_id" in (missing_host.get("error") or ""),
       "claim_wake requires host_id before persistence")

    missing_wake = complete_wake.execute_mapping_result(
        {"project": "switchboard"}, actor="tester")
    ok(missing_wake.get("error_code") == "invalid_complete_wake"
       and "wake_id" in (missing_wake.get("error") or ""),
       "complete_wake requires wake_id before persistence")

    bad_selector = request_wake.execute_mapping_result(
        {"selector_json": "not-json", "project": "switchboard"},
        actor="tester",
    )
    ok(bad_selector.get("error_code") == "invalid_request_wake"
       and "selector" in (bad_selector.get("error") or "").lower(),
       "invalid selector_json fails closed at the command layer")

    bad_result = complete_wake.execute_mapping_result(
        {"wake_id": "wake-1", "result_json": "not-json"},
        actor="tester",
    )
    ok(bad_result.get("error_code") == "invalid_complete_wake"
       and "result" in (bad_result.get("error") or "").lower(),
       "invalid result_json fails closed at the command layer")

    # ---- execute delegates to injected persistence --------------------------
    request_calls = []

    def fake_request_wake(**kwargs):
        request_calls.append(kwargs)
        return {"wake_id": "wake-x", **kwargs}

    request_result = request_wake.execute(
        RequestWakeCommand.from_mapping({
            "selector": {"runtime": "codex"},
            "reason": "need agent",
            "source": "dispatcher",
            "policy": {"prefer": "local"},
            "task_id": "ARCH-MS-39",
            "idem_key": "idem-9",
            "project": "switchboard",
        }),
        actor="tester",
        principal_id="principal-1",
        request=fake_request_wake,
    )
    ok(request_result["wake_id"] == "wake-x"
       and request_calls
       and request_calls[0]["selector"] == {"runtime": "codex"}
       and request_calls[0]["reason"] == "need agent"
       and request_calls[0]["source"] == "dispatcher"
       and request_calls[0]["policy"] == {"prefer": "local"}
       and request_calls[0]["task_id"] == "ARCH-MS-39"
       and request_calls[0]["actor"] == "tester"
       and request_calls[0]["principal_id"] == "principal-1"
       and request_calls[0]["idem_key"] == "idem-9"
       and request_calls[0]["project"] == "switchboard",
       "request_wake forwards normalized fields to persistence")

    claim_calls = []

    def fake_claim_wake(host_id, wake_id, **kwargs):
        claim_calls.append((host_id, wake_id, kwargs))
        return {"claimed": True, "host_id": host_id, "wake_id": wake_id, **kwargs}

    claim_result = claim_wake.execute(
        ClaimWakeCommand.from_mapping({
            "host_id": "host/z",
            "wake_id": "wake-z",
            "project": "switchboard",
        }),
        actor="tester",
        claim=fake_claim_wake,
    )
    ok(claim_result["claimed"] is True
       and claim_calls
       and claim_calls[0][0] == "host/z"
       and claim_calls[0][1] == "wake-z"
       and claim_calls[0][2]["actor"] == "tester"
       and claim_calls[0][2]["project"] == "switchboard",
       "claim_wake forwards host/wake ids to persistence")

    complete_calls = []

    def fake_complete(wake_id, **kwargs):
        complete_calls.append((wake_id, kwargs))
        return {"wake_id": wake_id, "status": "completed", **kwargs}

    complete_result = complete_wake.execute(
        CompleteWakeCommand.from_mapping({
            "wake_id": "wake-done",
            "runner_session_id": "rs-1",
            "agent_id": "agent/done",
            "result": {"started": True},
            "project": "switchboard",
        }),
        actor="tester",
        complete=fake_complete,
    )
    ok(complete_result["status"] == "completed"
       and complete_calls
       and complete_calls[0][0] == "wake-done"
       and complete_calls[0][1]["actor"] == "tester"
       and complete_calls[0][1]["runner_session_id"] == "rs-1"
       and complete_calls[0][1]["agent_id"] == "agent/done"
       and complete_calls[0][1]["result"] == {"started": True}
       and complete_calls[0][1]["project"] == "switchboard",
       "complete_wake forwards result evidence to persistence")

    # ---- both adapters invoke the shared application handlers ---------------
    wakes_router = (
        ROOT / "src/switchboard/api/routers/wakes.py"
    ).read_text(encoding="utf-8")
    mcp_source = (ROOT / "src/switchboard/mcp/tools/wakes.py").read_text(encoding="utf-8")
    ok("request_wake_command.execute_mapping_result" in wakes_router
       and "claim_wake_command.execute_mapping_result" in wakes_router
       and "complete_wake_command.execute_mapping_result" in wakes_router
       and "request_wake_command.execute_mapping_result" in mcp_source
       and "claim_wake_command.execute_mapping_result" in mcp_source
       and "complete_wake_command.execute_mapping_result" in mcp_source
       and "store.request_wake(" not in wakes_router
       and "store.claim_wake(" not in wakes_router
       and "store.complete_wake(" not in wakes_router
       and "store.request_wake(" not in mcp_source
       and "store.claim_wake(" not in mcp_source
       and "store.complete_wake(" not in mcp_source,
       "REST and MCP adapters invoke the same wake commands")

    mcp_host = entrypoint_source("mcp_server")
    app_host = entrypoint_source("app")
    # ARCH-MS-70 finished draining the inline IXP wake routes (request_wake/cancel_wake/
    # wake_intents) out of app_impl.py into the wakes router alongside their TXP siblings —
    # the shared command call is proven against wakes_router above, not the composition root.
    ok("register_wake_tools" in mcp_host
       and "def request_wake(" not in mcp_host
       and "def claim_wake(" not in mcp_host
       and "def complete_wake(" not in mcp_host
       and "_create_wakes_router" in app_host
       and "def txp_request_wake" not in app_host
       and "def txp_claim_wake" not in app_host
       and "def txp_complete_wake" not in app_host
       and "def ixp_request_wake" not in app_host
       and "def ixp_cancel_wake" not in app_host
       and "request_wake_command.execute_mapping_result" not in app_host,
       "monolith hosts register thin wake adapters only")

    body_helper = app_host.find("def _body_project(")
    wakes_include = app_host.find("_create_wakes_router(")
    ok(body_helper != -1 and wakes_include != -1 and body_helper < wakes_include,
       "_body_project is defined before the wakes router is included")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
