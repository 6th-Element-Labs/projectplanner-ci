#!/usr/bin/env python3
"""ARCH-MS-79: optional Tasks cut is waived; readiness exit path retained."""
from __future__ import annotations

import importlib.util
import json

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

# Phase 2 waived a *live* Tasks cut. Phase 3 Path A (ARCH-MS-92) may ship
# deploy/switchboard-tasks.service only when process_cut is authorized.
unit = ROOT / "deploy" / "switchboard-tasks.service"
verdict_path = ROOT / "docs" / "phase3" / "tasks_independence_verdict.json"
verdict: dict = {}
if verdict_path.is_file():
    try:
        loaded = json.loads(verdict_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            verdict = loaded
    except json.JSONDecodeError:
        verdict = {}
process_cut_authorized = (
    verdict.get("process_cut_authorized") is True
    or str(verdict.get("verdict", "")).lower() == "go"
)
ok(
    (not unit.is_file()) or process_cut_authorized,
    "production Tasks unit absent under Phase 2 waive, or authorized by Phase 3 Path A",
)

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
