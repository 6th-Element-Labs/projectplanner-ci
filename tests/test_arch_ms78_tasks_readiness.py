#!/usr/bin/env python3
"""ARCH-MS-78: Tasks readiness package is present for Phase 2 exit."""
from __future__ import annotations

import importlib.util

from path_setup import ROOT

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _load_exit_gate():
    path = ROOT / "scripts" / "arch_ms_phase2_exit_gate.py"
    spec = importlib.util.spec_from_file_location("arch_ms_phase2_exit_gate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


canonical = ROOT / "docs" / "ARCH-MS-PHASE2-TASKS-READINESS.md"
gate_path = ROOT / "docs" / "phase2" / "tasks_readiness.md"
ok(canonical.is_file(), "canonical ARCH-MS-PHASE2-TASKS-READINESS.md exists")
ok(gate_path.is_file(), "exit-gate tasks_readiness.md exists")

canon = canonical.read_text(encoding="utf-8") if canonical.is_file() else ""
gate = gate_path.read_text(encoding="utf-8") if gate_path.is_file() else ""

ok("readiness-only" in canon.lower(), "canonical records readiness-only decision")
ok("ship-now" in canon.lower(), "canonical discusses ship-now vs readiness")
ok("/api/tasks" in canon and "claim_task" in canon and "complete_claim" in canon,
   "canonical sketches task CRUD + claim/complete contracts")
ok("Caddy" in canon and ("8122" in canon or "rollback" in canon.lower()),
   "canonical covers extract plan (Caddy / port / rollback)")
ok("ARCH-MS-PHASE2-TASKS-READINESS.md" in gate or "readiness-only" in gate.lower(),
   "gate artifact points at canonical / records decision")

tracker = (ROOT / "docs" / "ARCH-MS-EXECUTION.md").read_text(encoding="utf-8")
ok("ARCH-MS-78" in tracker, "execution tracker lists ARCH-MS-78")

exit_gate = _load_exit_gate()
tasks_check = exit_gate._tasks_cut_or_readiness(ROOT)
ok(tasks_check.get("ok") is True, f"_tasks_cut_or_readiness ok (got {tasks_check!r})")
ok(tasks_check.get("readiness_artifact_present") is True,
   "gate readiness_artifact_present is true")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
