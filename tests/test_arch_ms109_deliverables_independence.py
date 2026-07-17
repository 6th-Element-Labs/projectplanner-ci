#!/usr/bin/env python3
"""ARCH-MS-109 executable Deliverables independence verdict."""
from __future__ import annotations

import importlib.util

from path_setup import ROOT


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


spec = importlib.util.spec_from_file_location(
    "arch_ms109_deliverables_independence",
    ROOT / "scripts" / "arch_ms109_deliverables_independence.py",
)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

report = gate.evaluate(ROOT, run_probe=True)
ok(report.get("ok") is True, "executable independence artifact is internally consistent")
ok(report.get("verdict") == "go", "verdict is explicit Go")
ok(report.get("process_build_authorized") is True, "Go authorizes ARCH-MS-110 side-by-side build")
ok(report.get("production_cutover_authorized") is False, "production cutover is not pre-authorized")
ok(report.get("failed_gates") == [], "all six independence gates pass")
checks = report.get("checks") or {}
ok(checks.get("router_inventory_complete") is True, "all 24 router routes are inventoried")
ok(checks.get("day_one_surface_exact") is True, "exact eight-route day-one surface is locked")
ok(checks.get("repository_calls_exact") is True, "repository/application calls match live router AST")
ok(checks.get("writer_transactions_complete") is True,
   "all sixteen writers have explicit monolith transaction/orchestration boundaries")
ok(checks.get("closure_transaction_atomic") is True, "closure report and audit stamp remain atomic")
ok(checks.get("auth_project_scope_bound") is True, "standalone build requires project-scoped read Auth")
ok(checks.get("revision_drift_contract") is True, "revision and drift contract is explicit")
ok(checks.get("resource_budget_math") is True, "live post-Coord capacity preserves 512 MiB reserve")
probe = report.get("sqlite_probe") or {}
ok(probe.get("ok") is True, "concurrent closure snapshot probe passes")
ok(probe.get("lock_errors") == 0, "concurrent probe has zero lock errors")
ok(probe.get("snapshot_mismatches") == 0, "readers never observe split closure state")

verdict = gate.load_verdict(ROOT / "docs" / "deliverables" / "deliverables_independence_verdict.json")
ok(len(verdict.get("writer_inventory") or []) == 16, "all sixteen writes explicitly stay monolith-owned")
ok("ARCH-MS-110" in (verdict.get("go_only_tasks") or []), "machine verdict gates the Go-only successor")
incomplete_writers = [dict(row) for row in verdict.get("writer_inventory") or []]
incomplete_writers[0].pop("transaction")
ok(gate.writer_transaction_inventory_complete(incomplete_writers) is False,
   "negative writer mutation without a transaction boundary is rejected")

# Review remediation ARCH-MS-109-RV-1: this fixture retains every old marker
# string but deliberately moves the audit insert to a second connection.  The
# structural proof must reject it, preventing the repository-wide false pass.
split_transaction_fixture = '''
def _record_deliverable_closure_impl(deliverable_id, report, actor, project):
    with _store_facade()._conn(project) as c:
        c.execute("UPDATE deliverables SET metadata_json=?, updated_at=? WHERE id=?")
    with _store_facade()._conn(project) as c:
        c.execute("INSERT INTO activity", ("deliverable.closure_verified",))

def record_deliverable_closure(deliverable_id, report, actor, project):
    return _store_facade()._write_through(
        project, lambda: _store_facade()._record_deliverable_closure_impl(
            deliverable_id, report, actor, project))
'''
split_proof = gate.closure_transaction_proof(split_transaction_fixture)
ok(split_proof.get("ok") is False,
   "negative split-transaction mutation is rejected despite retaining old markers")

print(f"\nARCH-MS-109 Deliverables independence: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
