#!/usr/bin/env python3
"""ARCH-MS-84: architecture ratchets executable proof."""
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


baseline = ROOT / "perf" / "arch_ms84_ratchet_baseline.json"
script = ROOT / "scripts" / "arch_ms84_architecture_ratchets.py"
ok(baseline.is_file(), "perf/arch_ms84_ratchet_baseline.json exists")
ok(script.is_file(), "scripts/arch_ms84_architecture_ratchets.py exists")

data = json.loads(baseline.read_text(encoding="utf-8")) if baseline.is_file() else {}
ok(data.get("schema") == "switchboard.arch_ms84_architecture_ratchet.v1",
   "baseline schema is arch_ms84 ratchet v1")
ok("auth_forbidden_imports" in data.get("scopes", {}), "baseline scopes auth forbidden imports")
ok("store_import_files_src" in data.get("scopes", {}), "baseline scopes store import ceiling")
ok("wildcard_import_sites_src" in data.get("scopes", {}), "baseline scopes wildcard ceiling")
ok("untyped_body_dict_routers" in data.get("scopes", {}), "baseline scopes untyped body ceiling")

proc = subprocess.run(
    [sys.executable, str(script), "--json", "--ruff-changed", "--base", "HEAD"],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
    check=False,
)
ok(proc.returncode == 0, f"architecture ratchets exit 0 (code={proc.returncode})")
report = {}
try:
    report = json.loads(proc.stdout)
except json.JSONDecodeError:
    ok(False, f"ratchet JSON parse failed: {(proc.stdout or proc.stderr)[:400]}")
else:
    ok(report.get("ok") is True, "ratchet report ok=true")
    checks = report.get("checks") or {}
    for name in (
        "auth_forbidden_imports",
        "store_import_files_src",
        "wildcard_import_sites_src",
        "untyped_body_dict_routers",
        "ruff_changed",
    ):
        ok(checks.get(name) is True, f"check {name} passed")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
