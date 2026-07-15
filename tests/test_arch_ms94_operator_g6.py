#!/usr/bin/env python3
"""ARCH-MS-94: Operator G6 reopens Path A; Path B No-Go superseded."""
from __future__ import annotations

import json
import subprocess
import sys

from path_setup import ROOT

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


verdict_path = ROOT / "docs/phase3/tasks_independence_verdict.json"
waived = ROOT / "docs/phase3/tasks_cut_waived.md"
nogo = ROOT / "docs/phase3/tasks_nogo_rationale.md"
tracker = ROOT / "docs/ARCH-MS-EXECUTION.md"

ok(verdict_path.is_file(), "independence verdict artifact present")
data = json.loads(verdict_path.read_text(encoding="utf-8"))
ok(data.get("schema") == "switchboard.tasks_independence_verdict.v1", "verdict schema")
ok(str(data.get("verdict") or "").lower() == "go", "verdict is go")
ok(data.get("task_id") == "ARCH-MS-94", "verdict owned by ARCH-MS-94")
ok((data.get("inputs") or {}).get("G6_operator_go") is True, "G6_operator_go true")
ok(data.get("process_cut_authorized") is True, "process_cut_authorized true")
ok(data.get("path_b_nogo_valid") is False, "Path B No-Go no longer valid")

supersedes = data.get("supersedes") or {}
ok(supersedes.get("task_id") == "ARCH-MS-93", "supersedes ARCH-MS-93")
ok(str(supersedes.get("decision") or "").lower() == "nogo",
   "supersedes Path B nogo decision")
ok("ARCH-MS-94" in str((data.get("evidence") or {}).get("operator_g6") or ""),
   "evidence cites ARCH-MS-94 as operator G6")

waive_text = waived.read_text(encoding="utf-8") if waived.is_file() else ""
ok(waived.is_file(), "waive note present")
ok("ARCH-MS-94" in waive_text and "supersed" in waive_text.lower(),
   "waive note records ARCH-MS-94 supersession")
ok("ARCH-MS-93" in waive_text, "waive note cites prior ARCH-MS-93 Path B")

nogo_text = nogo.read_text(encoding="utf-8") if nogo.is_file() else ""
ok(nogo.is_file(), "historical Path B rationale retained")
ok("SUPERSEDED" in nogo_text and "ARCH-MS-94" in nogo_text,
   "nogo rationale marked SUPERSEDED by ARCH-MS-94")

tracker_text = tracker.read_text(encoding="utf-8") if tracker.is_file() else ""
ok("ARCH-MS-94" in tracker_text, "execution tracker lists ARCH-MS-94")
ok("Operator G6" in tracker_text or "operator G6" in tracker_text.lower(),
   "tracker names operator G6 reopen")

# Live Path A must remain green under the reopened Go.
proc = subprocess.run(
    [sys.executable, str(ROOT / "scripts/arch_ms_phase3_exit_gate.py")],
    cwd=ROOT,
    text=True,
    capture_output=True,
)
try:
    report = json.loads(proc.stdout)
except json.JSONDecodeError:
    report = {"passed": False, "error": proc.stdout or proc.stderr}

ok(bool(report.get("passed")), "Phase 3 exit gate still passed=true")
ok(bool(report.get("checks", {}).get("path_a_tasks_cut")), "Path A Tasks cut still green")
ok(report.get("independence", {}).get("process_cut_authorized") is True,
   "exit gate sees process_cut_authorized")
ok(proc.returncode == 0, f"exit gate CLI returncode 0 (got {proc.returncode})")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
