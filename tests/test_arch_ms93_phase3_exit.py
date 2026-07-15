#!/usr/bin/env python3
"""ARCH-MS-93: Phase 3 exit Path B is green on the live tree."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from path_setup import ROOT

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


verdict = ROOT / "docs/phase3/tasks_independence_verdict.json"
nogo = ROOT / "docs/phase3/tasks_nogo_rationale.md"
waived = ROOT / "docs/phase3/tasks_cut_waived.md"
playbook = ROOT / "docs/phase3/tasks_cut_playbook.md"

ok(verdict.is_file(), "docs/phase3/tasks_independence_verdict.json present")
ok(nogo.is_file(), "docs/phase3/tasks_nogo_rationale.md present")
ok(waived.is_file(), "docs/phase3/tasks_cut_waived.md present")
ok(playbook.is_file(), "docs/phase3/tasks_cut_playbook.md present")

data = json.loads(verdict.read_text(encoding="utf-8"))
raw = str(data.get("verdict") or data.get("decision") or "").strip().lower()
ok(raw in {"nogo", "no-go", "no_go", "keep-in-process", "keep_in_process"},
   f"independence verdict is nogo (got {raw!r})")
ok(data.get("inputs", {}).get("G6_operator_go") is False,
   "operator G6 remains false (no process-cut authorization)")

nogo_text = nogo.read_text(encoding="utf-8")
ok("No-Go" in nogo_text or "nogo" in nogo_text.lower(), "No-Go rationale records decision")
ok("ARCH-MS-89" in nogo_text, "No-Go cites ARCH-MS-89 measured evidence")

waive_text = waived.read_text(encoding="utf-8")
ok("Waived" in waive_text or "waived" in waive_text.lower(),
   "waive artifact records Waived decision")
ok("ARCH-MS-90" in waive_text and "ARCH-MS-91" in waive_text and "ARCH-MS-92" in waive_text,
   "waive covers ARCH-MS-90…92")

ok(not (ROOT / "src/switchboard/services/tasks/app.py").is_file(),
   "no live services/tasks app (Path B in-process)")
ok(not (ROOT / "deploy/switchboard-tasks.service").is_file(),
   "no production Tasks systemd unit")

tracker = (ROOT / "docs/ARCH-MS-EXECUTION.md").read_text(encoding="utf-8")
ok("ARCH-MS-93" in tracker, "execution tracker mentions ARCH-MS-93")
ok("ARCH-MS-90" in tracker and ("Waived" in tracker or "waived" in tracker.lower()),
   "execution tracker lists ARCH-MS-90…92 waive under Path B")

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

ok(report.get("schema") == "switchboard.arch_ms_phase3_exit.v1", "exit report schema")
ok(bool(report.get("passed")), "live Phase 3 exit gate passed=true")
ok(bool(report.get("checks", {}).get("path_b_documented_nogo")), "Path B No-Go satisfied")
ok(bool(report.get("checks", {}).get("phase2_exit_green")), "Phase 2 exit still green")
ok(
    bool(report.get("checks", {}).get("no_half_cut_network_facade")),
    "no half-cut network façade",
)
ok(proc.returncode == 0, f"exit gate CLI returncode 0 (got {proc.returncode})")

# Prior ARCH-MS-86 harness still fail-closed on empty fixtures (import smoke).
gate86 = ROOT / "tests/test_arch_ms86_phase3_exit_gate.py"
ok(gate86.is_file(), "ARCH-MS-86 fail-closed harness still present")

if proc.stderr:
    print(proc.stderr)
if failed and report.get("error"):
    print("  DETAIL " + str(report["error"]))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
