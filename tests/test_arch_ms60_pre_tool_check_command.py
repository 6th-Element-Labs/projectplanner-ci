#!/usr/bin/env python3
"""ARCH-MS-60: pre_tool_check lives under application/commands (not shell)."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms60-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

passed = failed = 0
SHELL_BEFORE = 1952


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    try:
        importlib.import_module("switchboard.application.commands.pre_tool_check")
        ok(True, "switchboard.application.commands.pre_tool_check imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"pre_tool_check import failed: {exc!r}")

    cmd_path = ROOT / "src/switchboard/application/commands/pre_tool_check.py"
    ok(cmd_path.is_file(), "pre_tool_check.py exists under application/commands")

    from switchboard.application.commands import pre_tool_check as cmd_mod  # noqa: E402
    import store  # noqa: E402

    ok(store.pre_tool_check is cmd_mod.pre_tool_check,
       "store facade delegates pre_tool_check")
    ok(store._pre_tool_classify is cmd_mod._pre_tool_classify,
       "store facade delegates _pre_tool_classify")
    ok(store.pre_tool_check.__module__
       == "switchboard.application.commands.pre_tool_check",
       "pre_tool_check lives under application.commands.pre_tool_check")

    ok(not (ROOT / "src/switchboard/storage/repositories/shell.py").is_file(),
       "shell residual deleted (ARCH-MS-64)")
    cmd_src = cmd_path.read_text()
    rest_src = (ROOT / "src/switchboard/api/routers/ixp_work_sessions.py").read_text()
    mcp_src = (ROOT / "src/switchboard/mcp/tools/work_sessions.py").read_text()

    for name in (
        "_pre_tool_input",
        "_pre_tool_classify",
        "_pre_tool_target_path",
        "_pre_tool_relpath",
        "_pre_tool_decision",
        "_pre_tool_requested_profile",
        "_record_pre_tool_activity",
        "pre_tool_check",
    ):
        ok(f"def {name}(" in cmd_src,
           f"application command defines {name}")

    ok("pre_tool_check_command.execute_mapping_result" in rest_src,
       "REST adapter calls application command")
    ok("store.pre_tool_check(" not in rest_src,
       "REST adapter is not a fat store call for pre_tool_check")
    ok("pre_tool_check_command.execute_mapping_result" in mcp_src,
       "MCP adapter calls application command")
    ok("store.pre_tool_check(" not in mcp_src,
       "MCP adapter is not a fat store call for pre_tool_check")

    # Golden decision shape preserved (classification + schema).
    classification = cmd_mod._pre_tool_classify("Write", {"file_path": "a.py"})
    ok(classification.get("action") == "file_write"
       and classification.get("requires_work_session") is True,
       "classify Write as file_write requiring Work Session")
    decision = cmd_mod._pre_tool_decision("allow", "ok", ok=True)
    ok(decision.get("schema") == "switchboard.pre_tool_check.v1"
       and decision.get("decision") == "allow",
       "decision fixture keeps schema + decision fields")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
