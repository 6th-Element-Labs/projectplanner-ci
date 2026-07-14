#!/usr/bin/env python3
"""Focused proof for ARCH-MS-36 claim application commands."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT, entrypoint_source

TMP = tempfile.mkdtemp(prefix="arch-ms36-claim-commands-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from switchboard.application.commands import (  # noqa: E402
    claim_next,
    claim_task,
    complete_claim,
)
from switchboard.application.contracts.claims import (  # noqa: E402
    ClaimNextCommand,
    ClaimTaskCommand,
    CompleteClaimCommand,
)
from switchboard.contracts import (  # noqa: E402
    CLAIM_NEXT_COMMAND_SCHEMA,
    CLAIM_TASK_COMMAND_SCHEMA,
    COMPLETE_CLAIM_COMMAND_SCHEMA,
    ClaimNextCommand as WiredNext,
    ClaimTaskCommand as WiredTask,
    CompleteClaimCommand as WiredComplete,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    # ---- contracts normalize adapter inputs ---------------------------------
    task_cmd = ClaimTaskCommand.from_mapping({
        "task_id": " ARCH-1 ",
        "agent_id": " agent/a ",
        "project": " switchboard ",
        "ttl_s": "120",
        "work_session_json": '{"workspace_root":"/tmp/ws"}',
        "policy_profile": "code_strict",
        "override_identity_risk": "true",
    })
    ok(task_cmd.schema_id == CLAIM_TASK_COMMAND_SCHEMA
       and task_cmd.task_id == "ARCH-1"
       and task_cmd.agent_id == "agent/a"
       and task_cmd.project == "switchboard"
       and task_cmd.ttl_seconds == 120
       and task_cmd.work_session == {"workspace_root": "/tmp/ws"}
       and task_cmd.session_policy_profile == "code_strict"
       and task_cmd.override_identity_risk is True,
       "ClaimTaskCommand normalizes aliases, whitespace, and work_session JSON")

    next_cmd = ClaimNextCommand.from_mapping({
        "agent_id": "agent/b",
        "lanes": "arch, coord",
        "capabilities": "write\nreview",
        "max_budget_usd": "",
        "lane": "ignored-when-lanes-set",
    })
    ok(next_cmd.schema_id == CLAIM_NEXT_COMMAND_SCHEMA
       and next_cmd.lanes == ("ARCH", "COORD")
       and next_cmd.capabilities == ("write", "review")
       and next_cmd.max_budget_usd is None,
       "ClaimNextCommand normalizes lanes/capabilities and empty budget")

    complete_cmd = CompleteClaimCommand.from_mapping({
        "claim_id": " claim-1 ",
        "project": " switchboard ",
        "final_status": " In Review ",
        "mission_project": " switchboard ",
        "evidence": {"branch": "feature/x"},
    })
    ok(complete_cmd.schema_id == COMPLETE_CLAIM_COMMAND_SCHEMA
       and complete_cmd.claim_id == "claim-1"
       and complete_cmd.final_status == "In Review"
       and complete_cmd.evidence == {"branch": "feature/x"},
       "CompleteClaimCommand strips text and keeps evidence payloads")

    ok(WiredTask.SCHEMA == CLAIM_TASK_COMMAND_SCHEMA
       and WiredNext.SCHEMA == CLAIM_NEXT_COMMAND_SCHEMA
       and WiredComplete.SCHEMA == COMPLETE_CLAIM_COMMAND_SCHEMA,
       "package contracts re-export claim command schemas")

    # ---- validation fails closed before persistence -------------------------
    missing_task = claim_task.execute_mapping_result(
        {"agent_id": "agent/a"}, actor="tester", principal_id="p1")
    ok(missing_task.get("error_code") == "invalid_claim_task"
       and "task_id" in (missing_task.get("error") or ""),
       "claim_task requires task_id before persistence")

    missing_agent = claim_next.execute_mapping_result(
        {"project": "switchboard"}, actor="tester", principal_id="p1")
    ok(missing_agent.get("error_code") == "invalid_claim_next"
       and "agent_id" in (missing_agent.get("error") or ""),
       "claim_next requires agent_id before persistence")

    missing_claim = complete_claim.execute_mapping_result(
        {"project": "switchboard"}, actor="tester")
    ok(missing_claim.get("error_code") == "invalid_complete_claim"
       and "claim_id" in (missing_claim.get("error") or ""),
       "complete_claim requires claim_id before persistence")

    bad_ws = claim_task.execute_mapping_result(
        {
            "task_id": "ARCH-1",
            "agent_id": "agent/a",
            "work_session_json": "not-json",
        },
        actor="tester",
    )
    ok(bad_ws.get("error_code") == "invalid_claim_task"
       and "work_session" in (bad_ws.get("error") or "").lower(),
       "invalid work_session_json fails closed at the command layer")

    # ---- execute delegates to injected persistence --------------------------
    claim_calls = []

    def fake_claim_task(**kwargs):
        claim_calls.append(kwargs)
        return {"claimed": True, **kwargs}

    task_result = claim_task.execute(
        ClaimTaskCommand.from_mapping({
            "task_id": "ARCH-9",
            "agent_id": "agent/z",
            "project": "switchboard",
            "ttl_seconds": 900,
            "idem_key": "idem-1",
            "work_session_id": "ws-1",
            "require_work_session": True,
        }),
        actor="tester",
        principal_id="principal-1",
        claim=fake_claim_task,
    )
    ok(task_result["claimed"] is True
       and claim_calls
       and claim_calls[0]["task_id"] == "ARCH-9"
       and claim_calls[0]["agent_id"] == "agent/z"
       and claim_calls[0]["actor"] == "tester"
       and claim_calls[0]["principal_id"] == "principal-1"
       and claim_calls[0]["ttl_seconds"] == 900
       and claim_calls[0]["idem_key"] == "idem-1"
       and claim_calls[0]["work_session_id"] == "ws-1"
       and claim_calls[0]["require_work_session"] is True
       and claim_calls[0]["project"] == "switchboard",
       "claim_task forwards normalized fields to persistence")

    next_calls = []

    def fake_claim_next(**kwargs):
        next_calls.append(kwargs)
        return {"claimed": True, **kwargs}

    next_result = claim_next.execute(
        ClaimNextCommand.from_mapping({
            "agent_id": "agent/z",
            "lanes": "ARCH",
            "capabilities": "write",
            "deliverable_id": "d1",
            "board_id": "b1",
            "mission_id": "m1",
            "milestone_id": "ms1",
            "project": "switchboard",
        }),
        actor="tester",
        principal_id="principal-1",
        claim=fake_claim_next,
    )
    ok(next_result["claimed"] is True
       and next_calls
       and next_calls[0]["lanes"] == ["ARCH"]
       and next_calls[0]["capabilities"] == ["write"]
       and next_calls[0]["deliverable_id"] == "d1"
       and next_calls[0]["board_id"] == "b1"
       and next_calls[0]["mission_id"] == "m1"
       and next_calls[0]["milestone_id"] == "ms1",
       "claim_next forwards scheduler filters to persistence")

    complete_calls = []

    def fake_complete(claim_id, **kwargs):
        complete_calls.append((claim_id, kwargs))
        return {"completed": True, "claim_id": claim_id, **kwargs}

    complete_result = complete_claim.execute(
        CompleteClaimCommand.from_mapping({
            "claim_id": "claim-9",
            "evidence": {"branch": "feat"},
            "final_status": "In Review",
            "project": "switchboard",
            "mission_project": "switchboard",
        }),
        actor="tester",
        complete=fake_complete,
    )
    ok(complete_result["completed"] is True
       and complete_calls
       and complete_calls[0][0] == "claim-9"
       and complete_calls[0][1]["actor"] == "tester"
       and complete_calls[0][1]["evidence"] == {"branch": "feat"}
       and complete_calls[0][1]["final_status"] == "In Review"
       and complete_calls[0][1]["mission_project"] == "switchboard",
       "complete_claim forwards evidence and status to persistence")

    # ---- both adapters invoke the shared application handlers ---------------
    claims_router = (
        ROOT / "src/switchboard/api/routers/claims.py"
    ).read_text(encoding="utf-8")
    mcp_source = (ROOT / "src/switchboard/mcp/tools/claims.py").read_text(encoding="utf-8")
    ok("claim_task_command.execute_mapping_result" in claims_router
       and "claim_next_command.execute_mapping_result" in claims_router
       and "complete_claim_command.execute_mapping_result" in claims_router
       and "claim_task_command.execute_mapping_result" in mcp_source
       and "claim_next_command.execute_mapping_result" in mcp_source
       and "complete_claim_command.execute_mapping_result" in mcp_source
       and "store.claim_task(" not in claims_router
       and "store.claim_next(" not in claims_router
       and "store.complete_claim(" not in claims_router
       and "store.claim_task(" not in mcp_source
       and "store.claim_next(" not in mcp_source
       and "store.complete_claim(" not in mcp_source,
       "REST and MCP adapters invoke the same claim commands")

    mcp_host = entrypoint_source("mcp_server")
    app_host = entrypoint_source("app")
    ok("register_claim_tools" in mcp_host
       and "def claim_next(" not in mcp_host
       and "def claim_task(" not in mcp_host
       and "def complete_claim(" not in mcp_host
       and "_create_claims_router" in app_host
       and "def txp_claim_next" not in app_host
       and "def txp_claim_task" not in app_host
       and "def txp_complete_claim" not in app_host,
       "monolith hosts register thin claim adapters only")

    body_helper = app_host.find("def _body_project(")
    claims_include = app_host.find("_create_claims_router(")
    ok(body_helper != -1 and claims_include != -1 and body_helper < claims_include,
       "_body_project is defined before the claims router is included")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
