#!/usr/bin/env python3
"""ARCH-MS-61: merge_gate lives under application/commands (not shell)."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms61-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

passed = failed = 0
SHELL_BEFORE = 1288


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    try:
        importlib.import_module("switchboard.application.commands.merge_gate")
        ok(True, "switchboard.application.commands.merge_gate imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"merge_gate import failed: {exc!r}")

    cmd_path = ROOT / "src/switchboard/application/commands/merge_gate.py"
    ok(cmd_path.is_file(), "merge_gate.py exists under application/commands")

    from switchboard.application.commands import merge_gate as cmd_mod  # noqa: E402
    import store  # noqa: E402

    ok(store.merge_gate is cmd_mod.merge_gate,
       "store facade delegates merge_gate")
    ok(store._merge_gate_bool is cmd_mod._merge_gate_bool,
       "store facade delegates _merge_gate_bool")
    ok(store.merge_gate.__module__
       == "switchboard.application.commands.merge_gate",
       "merge_gate lives under application.commands.merge_gate")
    ok("never marks a task Done" in (store.merge_gate.__doc__ or "")
       or "never marks" in (store.merge_gate.__doc__ or ""),
       "docstring keeps non-executor / never-marks-Done contract")

    ok(not (ROOT / "src/switchboard/storage/repositories/shell.py").is_file(),
       "shell residual deleted (ARCH-MS-64)")
    cmd_src = cmd_path.read_text()
    rest_src = (ROOT / "src/switchboard/api/routers/external_effects.py").read_text()
    mcp_src = (ROOT / "src/switchboard/mcp/tools/external_effects.py").read_text()

    for name in (
        "_merge_gate_finding",
        "_merge_gate_pr_number",
        "_merge_gate_context_rows",
        "_merge_gate_status_contexts",
        "_merge_gate_context_passed",
        "_merge_gate_required_contexts",
        "_merge_gate_pr_evidence",
        "_merge_gate_pr_ref",
        "_merge_gate_bool",
        "merge_gate",
    ):
        ok(f"def {name}(" in cmd_src,
           f"application command defines {name}")

    ok("merge_gate_command.execute_mapping_result" in rest_src,
       "REST adapter calls application command")
    ok("store.merge_gate(" not in rest_src,
       "REST adapter is not a fat store call for merge_gate")
    ok("merge_gate_command.execute_mapping_result" in mcp_src,
       "MCP adapter calls application command")
    ok("store.merge_gate(" not in mcp_src,
       "MCP adapter is not a fat store call for merge_gate")
    ok("def execute_mapping_result(" in cmd_src,
       "execute_mapping_result remains the adapter entry")

    import sys
    import types

    mcp_module = types.ModuleType("mcp")
    mcp_server_module = types.ModuleType("mcp.server")
    mcp_fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    mcp_fastmcp_module.Context = object
    sys.modules.setdefault("mcp", mcp_module)
    sys.modules.setdefault("mcp.server", mcp_server_module)
    sys.modules.setdefault("mcp.server.fastmcp", mcp_fastmcp_module)
    from switchboard.mcp.tools import external_effects as mcp_effects  # noqa: E402

    captured = {}
    original_services = mcp_effects._SERVICES
    original_execute = mcp_effects.merge_gate_command.execute_mapping_result
    try:
        mcp_effects._SERVICES = mcp_effects.ExternalEffectsToolServices(
            dumps=lambda value: value,
            require_write=lambda *_args, **_kwargs: {"id": "reviewer", "actor": "reviewer"},
        )
        mcp_effects.merge_gate_command.execute_mapping_result = (
            lambda payload, **_kwargs: captured.update(payload) or {"ok": True}
        )
        executed = {
            "schema": "switchboard.executed_test_run.v1",
            "head_sha": "a" * 40,
            "commands": ["python3 test_merge_gate.py"],
            "completed_at": 1234.0,
            "exit_code": 0,
            "output_hash": "sha256:" + "b" * 64,
        }
        playwright = {
            **executed,
            "test_kind": "ui_playwright",
            "browser": "chromium",
            "headless": True,
            "executed_count": 1,
            "skipped": False,
            "console_error_count": 0,
            "failed_request_count": 0,
        }
        result = mcp_effects.merge_gate(
            "BUG-95", None, project="switchboard",
            evidence_json=__import__("json").dumps({
                "executed_test_run": executed,
                "ui_playwright_evidence": playwright,
            }),
        )
        ok(result.get("ok") is True
           and captured.get("evidence", {}).get("executed_test_run") == executed
           and captured.get("evidence", {}).get("ui_playwright_evidence") == playwright,
           "MCP merge_gate passes exact-head test and Playwright evidence to command")
        invalid = mcp_effects.merge_gate(
            "BUG-95", None, project="switchboard", evidence_json="[]")
        ok(invalid.get("error") == "evidence_json must be a JSON object string",
           "MCP merge_gate rejects non-object evidence")
    finally:
        mcp_effects._SERVICES = original_services
        mcp_effects.merge_gate_command.execute_mapping_result = original_execute

    finding = cmd_mod._merge_gate_finding(
        "draft_pr", "Draft PRs cannot pass the merge gate.", "failed_gate")
    ok(finding.get("code") == "draft_pr"
       and finding.get("blocking") is True
       and finding.get("failure_class") == "failed_gate",
       "finding fixture keeps code/blocking/failure_class shape")
    ok(cmd_mod._merge_gate_pr_number(
        "https://github.com/6th-Element-Labs/projectplanner/pull/475") == 475,
       "PR URL parse helper preserved")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
