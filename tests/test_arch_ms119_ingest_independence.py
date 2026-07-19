#!/usr/bin/env python3
"""ARCH-MS-119 executable Ingest independence No-Go verdict."""
from __future__ import annotations

import importlib.util
import subprocess
import sys

from path_setup import ROOT


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


spec = importlib.util.spec_from_file_location(
    "arch_ms119_ingest_independence", ROOT / "scripts" / "arch_ms119_ingest_independence.py")
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

report = gate.evaluate(ROOT, run_probe=True)
ok(report.get("ok") is True, "executable No-Go artifact is internally consistent")
ok(report.get("verdict") == "nogo", "verdict is explicit No-Go")
ok(report.get("process_build_authorized") is False, "No-Go blocks standalone Ingest build")
ok(report.get("production_cutover_authorized") is False, "production cutover is not authorized")
ok(report.get("failed_gates") == ["G2_idempotency_and_retry", "G4_auth_boundary", "G6_resource_budget"],
   "failure semantics, standalone auth, and live capacity block independence")
checks = report.get("checks") or {}
ok(checks.get("router_inventory_complete") is True, "all eight Ingest/inbox routes are inventoried")
ok(checks.get("day_one_surface_exact") is True, "exact two-route day-one surface is locked")
ok(checks.get("repository_calls_exact") is True, "declared calls match the live router AST")
ok(checks.get("writer_inventory_complete") is True, "all seven mutation flows are inventoried")
ok(checks.get("project_scope_structural") is True, "all routes bind an explicit project")
ok(checks.get("project_storage_isolated") is True, "project storage isolation is explicit")
ok(checks.get("failure_gate_matches_source") is True, "unsafe failure semantics are source-derived")
ok(checks.get("failure_findings_visible") is True, "all failure blockers are machine-readable")
ok(checks.get("resource_budget_math") is True, "post-Tally monolith capacity math is exact")
ok(report.get("resource_headroom_bytes") == -65949696, "projection misses reserve by 65949696 bytes")
probe = report.get("sqlite_probe") or {}
ok(probe.get("ok") is True, "routed WAL concurrency and isolation probe passes")
ok(probe.get("project_counts") == {"alpha": 80, "beta": 0}, "writes never cross project databases")
ok(probe.get("lock_errors") == 0, "concurrent probe has zero lock errors")

verdict = gate.load_verdict(ROOT / "docs" / "ingest" / "ingest_independence_verdict.json")
ok(gate.go_only_task_authorized(verdict, "ARCH-MS-120") is False,
   "machine verdict denies Go-only ARCH-MS-120")
go_attempt = subprocess.run(
    [sys.executable, str(ROOT / "scripts" / "arch_ms119_ingest_independence.py"),
     "--no-sqlite-probe", "--require-go-task", "ARCH-MS-120"],
    text=True, capture_output=True, check=False)
ok(go_attempt.returncode == 3, "Go-only CLI gate fails closed with exit code 3")

print(f"\nARCH-MS-119 Ingest independence: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
