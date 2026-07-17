#!/usr/bin/env python3
"""ARCH-MS-114 executable Tally independence No-Go verdict."""
from __future__ import annotations

import importlib.util
import subprocess
import sys

from path_setup import ROOT


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


spec = importlib.util.spec_from_file_location(
    "arch_ms114_tally_independence",
    ROOT / "scripts" / "arch_ms114_tally_independence.py",
)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

report = gate.evaluate(ROOT, run_probe=True)
ok(report.get("ok") is True, "executable No-Go artifact is internally consistent")
ok(report.get("verdict") == "nogo", "verdict is explicit No-Go")
ok(report.get("process_build_authorized") is False, "No-Go blocks the side-by-side Tally build")
ok(report.get("production_cutover_authorized") is False, "production cutover is not authorized")
ok(report.get("failed_gates") == ["G2_attribution_integrity", "G6_resource_budget"],
   "only attribution integrity and live resource budget block independence")
checks = report.get("checks") or {}
ok(checks.get("router_inventory_complete") is True, "all thirteen Tally routes are inventoried")
ok(checks.get("day_one_surface_exact") is True, "exact six-route day-one read surface is locked")
ok(checks.get("repository_calls_exact") is True, "declared repository calls match the live router AST")
ok(checks.get("writer_transactions_complete") is True, "all seven writer transactions are explicit")
ok(checks.get("project_scope_structural") is True, "all reads and writes bind an explicit project")
ok(checks.get("attribution_gate_matches_source") is True, "unsafe parent attribution is detected from source")
ok(checks.get("attribution_findings_visible") is True, "both attribution blockers are machine-readable")
ok(checks.get("resource_budget_math") is True, "live post-Deliverables capacity math is exact")
ok(report.get("resource_headroom_bytes") == -65949696, "128 MiB projection misses reserve by 65949696 bytes")
probe = report.get("sqlite_probe") or {}
ok(probe.get("ok") is True, "concurrent attribution snapshot probe passes for valid bundles")
ok(probe.get("lock_errors") == 0, "concurrent probe has zero lock errors")
ok(probe.get("snapshot_mismatches") == 0, "readers never observe inconsistent committed attribution")

verdict = gate.load_verdict(ROOT / "docs" / "tally" / "tally_independence_verdict.json")
ok(gate.go_only_task_authorized(verdict, "ARCH-MS-115") is False,
   "machine verdict denies the Go-only ARCH-MS-115 successor")
go_attempt = subprocess.run(
    [sys.executable,
     str(ROOT / "scripts" / "arch_ms114_tally_independence.py"),
     "--no-sqlite-probe", "--require-go-task", "ARCH-MS-115"],
    text=True, capture_output=True, check=False,
)
ok(go_attempt.returncode == 3, "Go-only CLI gate fails closed with exit code 3")

safe_fixture = '''
def report_usage(task_id=None, outcome_id=None):
    if task_id and outcome_id and conflicting_usage_attribution:
        raise ValueError("conflicting_usage_attribution")
def record_outcome(task_id=None):
    if task_id and outcome_parent_not_found:
        raise ValueError("outcome_parent_not_found")
'''
safe_proof = gate.attribution_safety_proof(safe_fixture)
ok(safe_proof.get("safe") is True, "future fail-closed attribution implementation clears the structural gate")

print(f"\nARCH-MS-114 Tally independence: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
