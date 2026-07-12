#!/usr/bin/env python3
"""Executable proof for the ADR-0009 Phase 0 exit gate."""
from __future__ import annotations

import json
import subprocess
import sys

from path_setup import ROOT

import store  # noqa: E402
from switchboard.storage.repositories import access  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


proc = subprocess.run(
    [sys.executable, str(ROOT / "scripts/arch_ms_phase0_exit_gate.py")],
    cwd=ROOT, text=True, capture_output=True,
)
try:
    report = json.loads(proc.stdout)
except json.JSONDecodeError:
    report = {"passed": False, "error": proc.stdout or proc.stderr}

ok(proc.returncode == 0 and report.get("passed") is True,
   "Phase 0 exit audit passes every fail-closed check")
ok(report.get("schema") == "switchboard.arch_ms_phase0_exit.v1",
   "exit evidence has a versioned machine-readable schema")
ok(report.get("store_reduction_lines", 0) >= report.get("store_reduction_target", 500),
   "store.py is at least 500 lines smaller than the immutable Phase 0 baseline")
ok(all(delta <= 0 for delta in report.get("line_deltas", {}).values()),
   "store.py, app.py, and mcp_server.py show no net growth from baseline")
ok(report.get("checks", {}).get("verbatim_access_move") is True,
   "project/access functions remain extracted; intentional repository evolution is declared")
ok(report.get("evolved_access_functions") == ["projects", "set_project_access"],
   "the project discovery fix is an explicit post-extraction evolution")
ok(store.has_project is access.has_project
   and store.project_access is access.project_access
   and store.grant_project_role is access.grant_project_role,
   "store.py remains a compatible facade over the extracted access repository")
ok(report.get("checks", {}).get("application_layer_proven") is True,
   "REST and MCP task adapters share create/get/update application handlers")
ok(report.get("checks", {}).get("ci_discovery_active") is True,
   "the CI gate still discovers both supported Python test filename patterns")
ok(report.get("missing_artifacts") == [],
   "every scaffold, security, migration, hygiene, and extraction proof artifact exists")

if proc.stderr:
    print(proc.stderr)
if failed and report.get("error"):
    print("  DETAIL " + str(report["error"]))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
