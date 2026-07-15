#!/usr/bin/env python3
"""ARCH-MS-84: Auth cut ops proof harness + gate doc measured inputs."""
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


script = ROOT / "scripts" / "arch_ms84_auth_ops_proof.py"
gate = ROOT / "docs" / "AUTH-INDEPENDENCE-GATE.md"
runbook = ROOT / "docs" / "runbooks" / "auth-caddy-cutover-rollback.md"
fragment = ROOT / "deploy" / "skeleton" / "Caddyfile.auth-fragment.example"

ok(script.is_file(), "scripts/arch_ms84_auth_ops_proof.py exists")
ok(runbook.is_file(), "auth caddy cutover/rollback runbook exists")
ok(fragment.is_file(), "Caddyfile.auth-fragment.example exists")
ok(gate.is_file(), "AUTH-INDEPENDENCE-GATE.md exists")

gate_text = gate.read_text(encoding="utf-8") if gate.is_file() else ""
ok("ARCH-MS-84" in gate_text, "independence gate references ARCH-MS-84")
ok("Measured" in gate_text or "measured" in gate_text, "independence gate has measured results")
ok("sqlite" in gate_text.lower() or "contention" in gate_text.lower(),
   "independence gate documents SQLite contention proof")
ok("401" in gate_text and "403" in gate_text, "independence gate documents 401/403 parity")
ok("Conditional Go" in gate_text or "No-Go" in gate_text,
   "independence gate records Go/No-Go recommendation")

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
        "status_parity_401_403",
        "caddy_drill_artifacts",
        "auth_down_empty_token_fail_closed",
    ):
        ok(checks.get(name) is True, f"ops check {name} passed")
    gng = report.get("go_no_go") or {}
    ok(bool(gng.get("recommendation")), "ops proof emits Go/No-Go recommendation")
    ok(gng.get("operator_g6_required") is True, "operator G6 still required")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
