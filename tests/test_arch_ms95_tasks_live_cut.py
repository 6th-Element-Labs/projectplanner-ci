#!/usr/bin/env python3
"""ARCH-MS-95: Post-G6 Path A live-cut closeout (Caddy + dual-strip green)."""
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


close = ROOT / "docs/phase3/tasks_live_cut_close.md"
playbook = ROOT / "docs/phase3/tasks_cut_playbook.md"
verdict_path = ROOT / "docs/phase3/tasks_independence_verdict.json"
runbook = ROOT / "docs/runbooks/tasks-caddy-cutover-rollback.md"
tracker = ROOT / "docs/ARCH-MS-EXECUTION.md"

ok(close.is_file(), "tasks_live_cut_close.md present")
close_text = close.read_text(encoding="utf-8") if close.is_file() else ""
ok("ARCH-MS-95" in close_text, "closeout cites ARCH-MS-95")
ok("ARCH-MS-92" in close_text and "ARCH-MS-94" in close_text,
   "closeout cites cut execution + G6")
ok("supersed" in close_text.lower() or "close" in close_text.lower(),
   "closeout records live-cut scope close/supersede")

playbook_text = playbook.read_text(encoding="utf-8") if playbook.is_file() else ""
ok("ARCH-MS-95" in playbook_text, "playbook cites ARCH-MS-95 close")
ok(runbook.is_file() and "8122" in runbook.read_text(encoding="utf-8"),
   "rollback runbook live")

# AC: Caddy live routes Tasks
caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
live = "\n".join(
    line for line in caddy.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)
ok("handle /api/tasks*" in live or "handle /api/tasks" in live,
   "live Caddy routes /api/tasks*")
ok("8122" in live, "live Caddy points Tasks at :8122")
ok("/txp/v1/claim_next" in live and "/txp/v1/complete_claim" in live,
   "live Caddy routes claim-only TXP")
ok("@tasks_sibling path_regexp tasks_sibling" in live and "handle @tasks_sibling" in live,
   "sibling dispatch/chat/review paths carved to monolith")

# AC: day-one surface not on monolith
ok((ROOT / "deploy/switchboard-tasks.service").is_file(),
   "production Tasks unit present")
ok(
    "PM_TASKS_HTTP_PRIMARY=service"
    in (ROOT / "deploy/projectplanner.service").read_text(encoding="utf-8"),
    "monolith dual-strip env set",
)
app_src = entrypoint_source("app")
ok("sibling_bc_only" in app_src and "PM_TASKS_HTTP_PRIMARY" in app_src,
   "app_impl dual-strips Mode A off monolith")

# AC: gate Path A green
verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
ok(str(verdict.get("verdict") or "").lower() == "go", "independence verdict go")
ok((verdict.get("inputs") or {}).get("G6_operator_go") is True, "G6 true")
ok(verdict.get("process_cut_authorized") is True, "process_cut_authorized")
ok("ARCH-MS-95" in str((verdict.get("evidence") or {}).get("live_cut_close") or ""),
   "verdict evidence cites ARCH-MS-95 live_cut_close")

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

ok(bool(report.get("passed")), "Phase 3 exit gate passed=true")
ok(bool(report.get("checks", {}).get("path_a_tasks_cut")), "Path A Tasks cut green")
ok(bool(report.get("caddy", {}).get("routes_api_tasks")), "exit gate sees Caddy Tasks")
ok(bool(report.get("dual_strip", {}).get("ok")), "exit gate sees dual-strip")
ok(proc.returncode == 0, f"exit gate CLI returncode 0 (got {proc.returncode})")

tracker_text = tracker.read_text(encoding="utf-8") if tracker.is_file() else ""
ok("ARCH-MS-95" in tracker_text, "execution tracker lists ARCH-MS-95")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
