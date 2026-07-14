#!/usr/bin/env python3
"""Executable proof for ARCH-MS-45 façade ceilings (under ARCH-MS-53 residual policy)."""
from __future__ import annotations

import json
import subprocess
import sys

from path_setup import ROOT

import store  # noqa: E402
from switchboard.storage.repositories import tasks as tasks_repo  # noqa: E402
from db.connection import _registry_conn  # noqa: E402


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
ok(report.get("schema") == "switchboard.arch_ms_phase1_exit.v1",
   "exit evidence has a versioned machine-readable schema")
ok(report.get("current_lines", {}).get("store.py", 9999) < 200,
   "store.py is under the 200-line façade ceiling")
ok(report.get("current_lines", {}).get("app.py", 9999) < 500,
   "app.py is under the 500-line adapter ceiling")
ok(report.get("current_lines", {}).get("mcp_server.py", 9999) < 500,
   "mcp_server.py is under the 500-line adapter ceiling")
ok(checks.get("store_logic_free") is True,
   "store.py has no business-logic function definitions")
ok(checks.get("store_facade_ceiling") is True
   and checks.get("app_adapter_ceiling") is True
   and checks.get("mcp_adapter_ceiling") is True,
   "entry façades/adapters clear ARCH-MS-45 absolute ceilings")
ok(store.merge_gate is not None
   and store.create_task is tasks_repo.create_task
   and store._registry_conn is _registry_conn,
   "store.py remains a compatible lazy composition root")
ok(not (ROOT / "src/switchboard/storage/repositories/shell.py").is_file(),
   "store residual shell module is deleted (ARCH-MS-64)")
ok(checks.get("store_residual_ceiling") is True,
   "deleted shell residual passes ARCH-MS-53 store residual ceiling")
ok(report.get("passed") is True,
   "Phase 1 exit passes after shell delete and residual drain")
ok(report.get("missing_artifacts") == [],
   "every Phase 1 proof artifact exists")

if proc.stderr:
    print(proc.stderr)
if failed and report.get("error"):
    print("  DETAIL " + str(report["error"]))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
