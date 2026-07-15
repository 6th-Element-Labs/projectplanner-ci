#!/usr/bin/env python3
"""ARCH-MS-79: optional Tasks cut is waived; readiness exit path retained."""
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


readiness = ROOT / "docs" / "phase2" / "tasks_readiness.md"
waived = ROOT / "docs" / "phase2" / "tasks_cut_waived.md"
canonical = ROOT / "docs" / "ARCH-MS-PHASE2-TASKS-READINESS.md"

ok(readiness.is_file(), "Phase 2 Tasks readiness gate artifact exists")
ok(waived.is_file(), "ARCH-MS-79 waive artifact exists")
ok(canonical.is_file(), "canonical readiness doc exists")

waive_text = waived.read_text(encoding="utf-8") if waived.is_file() else ""
ok("Waived" in waive_text or "waived" in waive_text.lower(),
   "waive artifact records Waived decision")
ok("ARCH-MS-78" in waive_text and "readiness" in waive_text.lower(),
   "waive cites ARCH-MS-78 readiness exit path")
ok("services/tasks" not in waive_text.lower() or "not a live" in waive_text.lower(),
   "waive does not claim a live Tasks service")

# Must not have shipped a Tasks process package as part of this waive.
ok(not (ROOT / "src" / "switchboard" / "services" / "tasks" / "app.py").is_file(),
   "no services/tasks app shipped (cut waived)")

tracker = (ROOT / "docs" / "ARCH-MS-EXECUTION.md").read_text(encoding="utf-8")
ok("ARCH-MS-79" in tracker and ("Waived" in tracker or "waived" in tracker.lower()),
   "execution tracker lists ARCH-MS-79 as waived")

exit_gate = _load_exit_gate()
tasks_check = exit_gate._tasks_cut_or_readiness(ROOT)
ok(tasks_check.get("ok") is True, f"phase2 tasks_cut_or_readiness still ok ({tasks_check!r})")
ok(tasks_check.get("readiness_artifact_present") is True,
   "exit gate still sees readiness artifact")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
