#!/usr/bin/env python3
"""ARCH-MS-100: Tasks live cut exit — chart Tasks green / closure-ready."""
from __future__ import annotations

import json
import subprocess
import sys

from path_setup import ROOT, entrypoint_source

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


exit_doc = ROOT / "docs/phase3/tasks_live_cut_exit.md"
close_doc = ROOT / "docs/phase3/tasks_live_cut_close.md"
verdict_path = ROOT / "docs/phase3/tasks_independence_verdict.json"
tracker = ROOT / "docs/ARCH-MS-EXECUTION.md"

ok(exit_doc.is_file(), "tasks_live_cut_exit.md present")
exit_text = exit_doc.read_text(encoding="utf-8") if exit_doc.is_file() else ""
ok("ARCH-MS-100" in exit_text, "exit packet cites ARCH-MS-100")
ok("closure verification" in exit_text.lower() or "ready for" in exit_text.lower(),
   "exit packet declares ready for closure verification")
ok("chart" in exit_text.lower() and "Tasks" in exit_text,
   "exit packet records chart Tasks green")

ok(close_doc.is_file() and "ARCH-MS-100" in close_doc.read_text(encoding="utf-8"),
   "closeout links ARCH-MS-100 exit")

# Caddy proves live Tasks traffic
caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
live = "\n".join(
    line for line in caddy.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)
ok("8122" in live and ("/api/tasks*" in live or "/api/tasks" in live),
   "Caddy live routes Tasks to :8122")
ok("/txp/v1/claim_next" in live, "Caddy live claim TXP to Tasks")
ok(
    "PM_TASKS_HTTP_PRIMARY=service"
    in (ROOT / "deploy/projectplanner.service").read_text(encoding="utf-8"),
    "dual-strip env on production monolith",
)
ok("sibling_bc_only" in entrypoint_source("app"), "monolith dual-strips Mode A")

# Independence go + no half-cut + Auth still green
verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
ok(str(verdict.get("verdict") or "").lower() == "go", "independence verdict go")
ok((verdict.get("inputs") or {}).get("G6_operator_go") is True, "G6 true")
ok(verdict.get("process_cut_authorized") is True, "process_cut_authorized")
ok("ARCH-MS-100" in str((verdict.get("evidence") or {}).get("live_cut_exit") or ""),
   "verdict cites ARCH-MS-100 live_cut_exit")

p3 = subprocess.run(
    [sys.executable, str(ROOT / "scripts/arch_ms_phase3_exit_gate.py")],
    cwd=ROOT, text=True, capture_output=True,
)
try:
    r3 = json.loads(p3.stdout)
except json.JSONDecodeError:
    r3 = {"passed": False}
ok(bool(r3.get("passed")), "Phase 3 exit gate passed")
ok(bool(r3.get("checks", {}).get("path_a_tasks_cut")), "Path A Tasks cut green")
ok(bool(r3.get("checks", {}).get("no_half_cut_network_facade")), "no half-cut")
ok(bool(r3.get("checks", {}).get("no_network_wrap_with_store_imports")),
   "no store-import network wrap")
ok(bool(r3.get("checks", {}).get("phase2_exit_green")), "Auth/Phase2 still green via p3")
ok(p3.returncode == 0, f"phase3 CLI rc 0 (got {p3.returncode})")

p2 = subprocess.run(
    [sys.executable, str(ROOT / "scripts/arch_ms_phase2_exit_gate.py")],
    cwd=ROOT, text=True, capture_output=True,
)
try:
    r2 = json.loads(p2.stdout)
except json.JSONDecodeError:
    r2 = {"passed": False}
ok(bool(r2.get("passed")), "Phase 2 exit gate passed")
ok(bool(r2.get("checks", {}).get("path_a_auth_cut")), "Auth Path A still green")
ok(p2.returncode == 0, f"phase2 CLI rc 0 (got {p2.returncode})")

tracker_text = tracker.read_text(encoding="utf-8") if tracker.is_file() else ""
ok("ARCH-MS-100" in tracker_text, "execution tracker lists ARCH-MS-100")
ok("chart Tasks green" in tracker_text or "live cut proven" in tracker_text.lower(),
   "tracker names Tasks chart / live-cut exit")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
