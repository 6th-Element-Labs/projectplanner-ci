#!/usr/bin/env python3
"""ARCH-MS-68: prepare_agent_session / contract boot logic lives in application/.

AC:
  - prompt / first_calls / contract builders unit-testable without FastMCP
  - prepare_agent_session / get_project_contract are thin MCP tools
  - mcp_server_impl residual shrinks (no boot helpers / tools inline)
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="arch-ms68-session-boot-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ.pop("PM_MCP_TOKEN", None)
os.environ.pop("PM_TOP_LEVEL_PROJECTS", None)

from path_setup import ROOT, entrypoint_source  # noqa: E402
from switchboard.application import session_boot  # noqa: E402
from switchboard.mcp.tools import boot as boot_tools  # noqa: E402
import store  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


BOOT_TOOLS = ("get_project_contract", "prepare_agent_session")

try:
    # --- structural: application owns builders; MCP residual has no defs ---
    app_path = ROOT / "src/switchboard/application/session_boot.py"
    adapter_path = ROOT / "src/switchboard/mcp/tools/boot.py"
    impl_path = ROOT / "mcp_server_impl.py"
    ok(app_path.is_file(), "application/session_boot.py exists")
    ok(adapter_path.is_file(), "mcp/tools/boot.py exists")

    app_source = app_path.read_text(encoding="utf-8")
    adapter_source = adapter_path.read_text(encoding="utf-8")
    impl_source = impl_path.read_text(encoding="utf-8")
    server_source = entrypoint_source("mcp_server")

    ok("def build_startup_prompt(" in app_source, "startup prompt builder in application")
    ok("def build_first_calls(" in app_source, "first_calls builder in application")
    ok("def prepare_agent_session(" in app_source, "prepare_agent_session in application")
    ok("project_contract" in app_source and "build(" in app_source,
       "session_boot reuses project_contract")

    ok("from switchboard.application import session_boot" in adapter_source,
       "boot MCP adapter calls application.session_boot")
    ok(adapter_source.count("session_boot.") >= 2,
       "boot tools delegate to application (not inline policy)")
    for name in BOOT_TOOLS:
        ok(f"def {name}(" in adapter_source, f"{name} lives in mcp/tools/boot.py")
        ok(f"def {name}(" not in impl_source, f"{name} left mcp_server_impl")

    for dead in (
        "def _project_contract(",
        "def _agent_bootstrap_prompt(",
        "def _first_calls(",
        "def _suggest_agent_id(",
        "def _project_ids_for_task(",
        "def _task_boot_brief(",
    ):
        ok(dead not in impl_source, f"{dead} removed from mcp_server_impl")

    ok("boot_tools.register_boot_tools(" in server_source
       or "boot_tools.register_boot_tools(" in impl_source,
       "mcp_server registers packaged boot tools")

    impl_lines = sum(1 for _ in impl_path.open(encoding="utf-8"))
    ok(impl_lines < 2428,
       f"mcp_server_impl residual shrank ({impl_lines} < 2428)")

    # --- FastMCP-free unit tests on builders ---
    store.init_project_registry()
    store.init_db("switchboard")
    store.init_db("helm")
    created = store.create_task({
        "workstream_id": "BOOT",
        "workstream_name": "Boot lane",
        "title": "Move prepare_agent_session/contract boot logic",
        "description": "session_profile:code_strict hermetic fixture",
        "deliverable": "application/session_boot.py",
    }, project="switchboard")
    tid = created["task_id"]
    ok(tid.startswith("BOOT-"), f"hermetic boot fixture task created ({tid})")

    agreement = {"protocol": {"name": "switchboard", "version": "ixp.v1"}}
    prompt = session_boot.build_startup_prompt(
        "switchboard",
        f"cursor/{tid}-test",
        tid,
        "BOOT",
    )
    ok('project="switchboard"' in prompt, "prompt binds selected project")
    ok("get_working_agreement" in prompt and "register_agent" in prompt,
       "prompt lists boot handshake steps")
    ok(f'get_task(task_id="{tid}", project="switchboard")' in prompt,
       "prompt includes get_task for assigned task")

    deliverable_prompt = session_boot.build_startup_prompt(
        "switchboard",
        "cursor/mission-test",
        "",
        "",
        deliverable_id="arch-ms-phase-1",
    )
    ok("Deliverable-first boot" in deliverable_prompt,
       "deliverable-scope prompt advertises mission-first boot")

    calls = session_boot.build_first_calls(
        "switchboard",
        f"cursor/{tid}-test",
        "cursor",
        "test-model",
        tid,
        "BOOT",
        agreement,
    )
    tools = [c["tool"] for c in calls]
    ok(tools[:4] == [
        "get_working_agreement",
        "register_agent",
        "list_unacked_messages",
        "list_unblock_requests",
    ], "first_calls open with handshake sequence")
    ok(any(c["tool"] == "get_project_contract"
           and c["args"]["project"] == "switchboard" for c in calls),
       "first_calls include project-bound get_project_contract")
    ok(any(c["tool"] == "get_task" and c["args"]["task_id"] == tid
           for c in calls),
       "first_calls include get_task for assigned task")

    agent_id = session_boot.suggest_agent_id(
        "cursor", "", tid, "BOOT",
        {"title": "Move prepare_agent_session/contract boot logic"},
    )
    ok(agent_id.startswith(f"cursor/{tid}-"),
       "suggest_agent_id slugs task title without FastMCP")

    contract = session_boot.get_project_contract(
        project="switchboard", task_id=tid,
    )
    ok(contract.get("ok") is True
       and contract.get("source_of_truth") == "switchboard_project_contract",
       "get_project_contract query returns switchboard contract")
    ok(contract.get("assigned_task", {}).get("task_id") == tid,
       "contract resolves assigned task without FastMCP")

    boot = session_boot.prepare_agent_session(
        runtime="cursor",
        project="switchboard",
        task_id=tid,
        intent="unit-test",
    )
    ok(boot.get("ok") is True and boot.get("selected_project") == "switchboard",
       "prepare_agent_session application query resolves project")
    ok(isinstance(boot.get("startup_prompt"), str)
       and isinstance(boot.get("first_calls"), list),
       "prepare_agent_session returns prompt + first_calls as structured data")
    ok(boot.get("project_contract", {}).get("source_of_truth")
       == "switchboard_project_contract",
       "prepare_agent_session embeds project_contract")

    mismatch = session_boot.prepare_agent_session(
        runtime="cursor", project="helm", task_id=tid,
    )
    ok(mismatch.get("ok") is False
       and mismatch.get("status") == "project_task_mismatch",
       "application prepare rejects wrong-project task without FastMCP")

    # --- thin MCP wiring still works through mcp_server ---
    import mcp_server  # noqa: E402

    registered = set(mcp_server.mcp._tool_manager._tools)
    ok(set(BOOT_TOOLS).issubset(registered),
       "FastMCP exposes packaged boot tools")
    ok(all(getattr(mcp_server, name) is getattr(boot_tools, name)
           for name in BOOT_TOOLS),
       "mcp_server retains direct-call aliases for boot tools")

    via_mcp = json.loads(mcp_server.prepare_agent_session(
        runtime="cursor", project="switchboard", task_id=tid,
    ))
    ok(via_mcp.get("ok") is True and via_mcp.get("selected_project") == "switchboard",
       "thin MCP prepare_agent_session still resolves")
    via_contract = json.loads(mcp_server.get_project_contract(
        project="switchboard", task_id=tid,
    ))
    ok(via_contract.get("source_of_truth") == "switchboard_project_contract",
       "thin MCP get_project_contract still works")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
