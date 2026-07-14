#!/usr/bin/env python3
"""ARCH-MS-71: true Phase 1 exit — residual ceilings green (supersedes PR #440).

PR #440 / ARCH-MS-45 only proved thin entry façades. ARCH-MS-53 forbids
rename-as-done; ARCH-MS-64 deleted shell; ARCH-MS-70 shrank adapter residuals.
This proof locks the live tree at ``passed=true`` and ``rename_as_done=false``.
"""
from __future__ import annotations

import json
import subprocess
import sys

from path_setup import ROOT


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


proc = subprocess.run(
    [sys.executable, str(ROOT / "scripts/arch_ms_phase1_exit_gate.py")],
    cwd=ROOT, text=True, capture_output=True,
)
try:
    report = json.loads(proc.stdout)
except json.JSONDecodeError:
    report = {"passed": False, "error": proc.stdout or proc.stderr}

checks = report.get("checks") or {}
residuals = report.get("residuals") or {}

ok(report.get("schema") == "switchboard.arch_ms_phase1_exit.v1",
   "true exit evidence uses switchboard.arch_ms_phase1_exit.v1")
ok(report.get("passed") is True,
   "Phase 1 exit gate passes on the live tree")
ok(report.get("rename_as_done") is False,
   "live tree is not rename-as-done (thin entry + fat residual)")
ok(checks.get("rename_as_done_forbidden") is True,
   "rename-as-done forbid check is green")
ok(checks.get("store_residual_ceiling") is True
   and checks.get("app_residual_ceiling") is True
   and checks.get("mcp_residual_ceiling") is True,
   "store/app/mcp residual ceilings are all green")
ok(checks.get("store_facade_ceiling") is True
   and checks.get("app_adapter_ceiling") is True
   and checks.get("mcp_adapter_ceiling") is True,
   "entry façades remain under absolute ceilings")
ok(checks.get("store_logic_free") is True
   and checks.get("facade_sql_free") is True
   and checks.get("adapter_sql_free") is True,
   "store/app/mcp stay logic-free and SQL-free at the entry surface")
ok(not (ROOT / "src/switchboard/storage/repositories/shell.py").is_file()
   and not (ROOT / "shell.py").is_file(),
   "shell residual is deleted (ARCH-MS-64)")
ok(bool(residuals.get("store", {}).get("deleted")),
   "store residual accounting reports shell deleted")
ok((ROOT / "src/switchboard/api/routers").is_dir()
   and (ROOT / "src/switchboard/mcp/tools").is_dir()
   and (ROOT / "src/switchboard/application/commands").is_dir()
   and (ROOT / "src/switchboard/application/queries").is_dir()
   and (ROOT / "src/switchboard/domain").is_dir()
   and (ROOT / "src/switchboard/storage/repositories").is_dir()
   and (ROOT / "src/switchboard/contracts").is_dir()
   and (ROOT / "static").is_dir(),
   "modular monolith package layout + static/ operator UI present")

# Residuals may remain under ceiling (ARCH-MS-70) or be deleted — both OK.
app_present = residuals.get("app", {}).get("present") or []
mcp_present = residuals.get("mcp", {}).get("present") or []
ok(all(item.get("under_ceiling") for item in app_present + mcp_present),
   "any remaining app_impl/mcp_server_impl residuals sit under shrinking ceilings")

if proc.stderr:
    print(proc.stderr)
if failed and report.get("error"):
    print("  DETAIL " + str(report["error"]))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
