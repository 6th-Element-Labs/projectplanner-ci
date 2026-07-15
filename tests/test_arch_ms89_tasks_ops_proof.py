#!/usr/bin/env python3
"""ARCH-MS-89: Tasks cut ops proof harness + Go/No-Go verdict artifact."""
from __future__ import annotations

import json
import subprocess
import sys

from path_setup import ROOT

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


script = ROOT / "scripts" / "arch_ms89_tasks_ops_proof.py"
gate = ROOT / "docs" / "TASKS-INDEPENDENCE-GATE.md"
verdict_path = ROOT / "docs" / "phase3" / "tasks_independence_verdict.json"
runbook = ROOT / "docs" / "runbooks" / "tasks-caddy-cutover-rollback.md"
fragment = ROOT / "deploy" / "skeleton" / "Caddyfile.tasks-fragment.example"
unit = ROOT / "deploy" / "tasks" / "switchboard-tasks.service.example"

ok(script.is_file(), "scripts/arch_ms89_tasks_ops_proof.py exists")
ok(runbook.is_file(), "tasks caddy cutover/rollback runbook exists")
ok(fragment.is_file(), "Caddyfile.tasks-fragment.example exists")
ok(unit.is_file(), "switchboard-tasks.service.example exists")
ok(gate.is_file(), "TASKS-INDEPENDENCE-GATE.md exists")
ok(verdict_path.is_file(), "docs/phase3/tasks_independence_verdict.json exists")

gate_text = gate.read_text(encoding="utf-8") if gate.is_file() else ""
ok("ARCH-MS-89" in gate_text, "independence gate references ARCH-MS-89")
ok("Measured" in gate_text or "measured" in gate_text, "independence gate has measured results")
ok("contention" in gate_text.lower() or "sqlite" in gate_text.lower(),
   "independence gate documents SQLite contention proof")
ok("401" in gate_text, "independence gate documents 401 parity")
ok("Conditional Go" in gate_text or "No-Go" in gate_text,
   "independence gate records Go/No-Go recommendation")

verdict = {}
if verdict_path.is_file():
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
ok(verdict.get("schema") == "switchboard.tasks_independence_verdict.v1",
   "verdict schema is tasks_independence_verdict.v1")
ok(verdict.get("verdict") in {"go", "nogo"},
   f"verdict is go|nogo (got {verdict.get('verdict')!r})")
ok(
    verdict.get("task_id") in {"ARCH-MS-89", "ARCH-MS-93"},
    f"verdict task_id is ARCH-MS-89 or Path B close ARCH-MS-93 (got {verdict.get('task_id')!r})",
)
if verdict.get("verdict") == "nogo":
    ok(bool(verdict.get("notes") or (verdict.get("evidence") or {}).get("rationale")
            or (verdict.get("evidence") or {}).get("nogo_rationale")),
       "No-Go verdict includes rationale")
    ok((verdict.get("inputs") or {}).get("G6_operator_go") is False,
       "No-Go does not claim operator G6")
else:
    ok(verdict.get("operator_g6_required") is True,
       "Go verdict still requires operator G6 before process cut")
    ok((verdict.get("inputs") or {}).get("G5_ops_proof") is True,
       "Go verdict records G5_ops_proof=true")
    ok((verdict.get("inputs") or {}).get("G6_operator_go") is False,
       "Go verdict does not claim operator G6 yet")

# Exit gate must not authorize process cut without operator G6 / Path A artifacts
from scripts import arch_ms_phase3_exit_gate as phase3_gate  # noqa: E402
live_exit = phase3_gate.build_report(ROOT, phase2_passed=True)
ok(live_exit.get("independence", {}).get("verdict") in {"go", "nogo"},
   "live exit gate sees independence verdict go|nogo")
ok(live_exit.get("independence", {}).get("process_cut_authorized") is False,
   "live tree does not authorize Tasks process cut")
ok(live_exit.get("checks", {}).get("no_half_cut_network_facade") is True,
   "live tree has no half-cut façade")

proc = subprocess.run(
    [sys.executable, str(script), "--json"],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
    check=False,
)
ok(proc.returncode == 0, f"ops proof exit 0 (code={proc.returncode})")
if proc.returncode != 0:
    print((proc.stdout or "")[-2000:])
    print((proc.stderr or "")[-2000:])

report = {}
try:
    report = json.loads(proc.stdout)
except json.JSONDecodeError:
    ok(False, f"ops proof JSON parse failed: {(proc.stdout or '')[:400]}")
else:
    ok(report.get("ok") is True, "ops proof report ok=true")
    checks = report.get("checks") or {}
    for name in (
        "sqlite_contention",
        "second_uvicorn_budget",
        "status_parity_day_one",
        "caddy_drill_artifacts",
        "ports_and_gate_docs_present",
    ):
        ok(checks.get(name) is True, f"ops check {name} passed")
    gng = report.get("go_no_go") or {}
    ok(bool(gng.get("recommendation")), "ops proof emits Go/No-Go recommendation")
    ok(gng.get("verdict") in {"go", "nogo"}, "ops proof emits verdict go|nogo")
    ok(gng.get("operator_g6_required") is True, "ops proof still requires operator G6")
    # ARCH-MS-93 may supersede Conditional Go with Path B No-Go for Phase 3 exit.
    if verdict.get("task_id") == "ARCH-MS-93" and verdict.get("verdict") == "nogo":
        ok(True, "ARCH-MS-93 Path B No-Go supersedes Conditional Go for Phase 3 exit")
        ok(bool((verdict.get("supersedes") or {}).get("decision") or
                (verdict.get("evidence") or {}).get("prior_conditional_go")),
           "Path B No-Go records prior Conditional Go provenance")
    elif gng.get("verdict") == "go":
        ok(verdict.get("verdict") == "go",
           "committed verdict matches harness go")
    else:
        ok(verdict.get("verdict") == "nogo",
           "committed verdict matches harness nogo")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
