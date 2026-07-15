#!/usr/bin/env python3
"""ARCH-MS-81: Phase 2 exit Path A is green on the live tree."""
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


verdict = ROOT / "docs/phase2/auth_independence_verdict.json"
playbook = ROOT / "docs/phase2/auth_cut_playbook.md"
tasks = ROOT / "docs/phase2/tasks_readiness.md"

ok(verdict.is_file(), "docs/phase2/auth_independence_verdict.json present")
ok(playbook.is_file(), "docs/phase2/auth_cut_playbook.md present")
ok(tasks.is_file(), "docs/phase2/tasks_readiness.md present")

data = json.loads(verdict.read_text(encoding="utf-8"))
raw = str(data.get("verdict") or data.get("decision") or "").strip().lower()
ok(raw in {"go", "yes", "cut"}, f"independence verdict is go (got {raw!r})")

proc = subprocess.run(
    [sys.executable, str(ROOT / "scripts/arch_ms_phase2_exit_gate.py")],
    cwd=ROOT,
    text=True,
    capture_output=True,
)
try:
    report = json.loads(proc.stdout)
except json.JSONDecodeError:
    report = {"passed": False, "error": proc.stdout or proc.stderr}

ok(report.get("schema") == "switchboard.arch_ms_phase2_exit.v1", "exit report schema")
ok(bool(report.get("passed")), "live Phase 2 exit gate passed=true")
ok(bool(report.get("checks", {}).get("path_a_auth_cut")), "Path A Auth cut satisfied")
ok(
    bool(report.get("checks", {}).get("no_half_cut_network_facade")),
    "no half-cut network façade",
)
ok(proc.returncode == 0, f"exit gate CLI returncode 0 (got {proc.returncode})")

if proc.stderr:
    print(proc.stderr)
if failed and report.get("error"):
    print("  DETAIL " + str(report["error"]))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
