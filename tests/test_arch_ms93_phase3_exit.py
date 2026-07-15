#!/usr/bin/env python3
"""ARCH-MS-93: Phase 3 exit Path A is green on the live tree (after ARCH-MS-92)."""
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
playbook = ROOT / "docs/phase3/tasks_cut_playbook.md"
waived = ROOT / "docs/phase3/tasks_cut_waived.md"
nogo = ROOT / "docs/phase3/tasks_nogo_rationale.md"

ok(verdict.is_file(), "docs/phase3/tasks_independence_verdict.json present")
ok(playbook.is_file(), "docs/phase3/tasks_cut_playbook.md present")
ok(waived.is_file(), "docs/phase3/tasks_cut_waived.md present (superseded note)")
ok(nogo.is_file(), "docs/phase3/tasks_nogo_rationale.md retained for audit")

data = json.loads(verdict.read_text(encoding="utf-8"))
raw = str(data.get("verdict") or data.get("decision") or "").strip().lower()
ok(raw in {"go", "yes", "cut"}, f"independence verdict is go (got {raw!r})")
ok(data.get("inputs", {}).get("G6_operator_go") is True, "operator G6 is true")
ok(data.get("process_cut_authorized") is True, "process_cut_authorized true")

ok((ROOT / "src/switchboard/services/tasks/app.py").is_file(),
   "Tasks service package present")
ok((ROOT / "deploy/switchboard-tasks.service").is_file(),
   "production Tasks systemd unit present")

caddy = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
live = "\n".join(
    line for line in caddy.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)
ok("8122" in live and "/api/tasks" in live, "live Caddy routes Tasks Mode A")
ok("PM_TASKS_HTTP_PRIMARY=service" in
   (ROOT / "deploy/projectplanner.service").read_text(encoding="utf-8"),
   "monolith dual-strip env live")

tracker = (ROOT / "docs/ARCH-MS-EXECUTION.md").read_text(encoding="utf-8")
ok("ARCH-MS-93" in tracker, "execution tracker mentions ARCH-MS-93")
ok("ARCH-MS-92" in tracker, "execution tracker mentions ARCH-MS-92")

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
ok(bool(report.get("checks", {}).get("path_a_tasks_cut")), "Path A Tasks cut satisfied")
ok(bool(report.get("checks", {}).get("phase2_exit_green")), "Phase 2 exit still green")
ok(
    bool(report.get("checks", {}).get("no_half_cut_network_facade")),
    "no half-cut network façade",
)
ok(proc.returncode == 0, f"exit gate CLI returncode 0 (got {proc.returncode})")

gate86 = ROOT / "tests/test_arch_ms86_phase3_exit_gate.py"
ok(gate86.is_file(), "ARCH-MS-86 fail-closed harness still present")

if proc.stderr:
    print(proc.stderr)
if failed and report.get("error"):
    print("  DETAIL " + str(report["error"]))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
