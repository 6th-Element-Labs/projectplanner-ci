#!/usr/bin/env python3
"""ARCH-MS-74: Phase 2 exit gate harness proof (ADR-0011 Decision 5).

Proves ``scripts/arch_ms_phase2_exit_gate.py`` is importable, emits a versioned
schema, implements Path A ∨ Path B with fail-closed half-cut detection, and
stays CI-safe while the live tree may still report ``passed=false`` until
2B0/2B/2C evidence lands (board AC).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from path_setup import ROOT


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _load_gate():
    path = ROOT / "scripts/arch_ms_phase2_exit_gate.py"
    spec = importlib.util.spec_from_file_location("arch_ms_phase2_exit_gate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_rails(root: Path) -> None:
    gate = _load_gate()
    _write(root / gate.CHARTER_ADR, "# ADR-0011\n")
    _write(root / gate.SKELETON_APP, "app = None\n")
    _write(root / gate.PHASE1_GATE, "print('fixture')\n")
    _write(root / "deploy" / "Caddyfile", "plan.example.com {\n    handle {\n        reverse_proxy 127.0.0.1:8110\n    }\n}\n")
    _write(root / "app.py", "app = None\n")
    _write(root / gate.TASKS_READINESS, "# Tasks readiness for service #2\n")


gate = _load_gate()

ok(gate.SCHEMA == "switchboard.arch_ms_phase2_exit.v1", "schema constant is versioned")
ok(
    (ROOT / "scripts/arch_ms_phase2_exit_gate.py").is_file(),
    "scripts/arch_ms_phase2_exit_gate.py exists",
)

# --- Live tree: well-formed report; may still be red until 2B/2C ---
proc = subprocess.run(
    [sys.executable, str(ROOT / "scripts/arch_ms_phase2_exit_gate.py")],
    cwd=ROOT,
    text=True,
    capture_output=True,
)
try:
    live = json.loads(proc.stdout)
except json.JSONDecodeError:
    live = {"passed": False, "error": proc.stdout or proc.stderr}

ok(live.get("schema") == gate.SCHEMA, "live report has versioned schema")
ok(isinstance(live.get("checks"), dict), "live report includes checks object")
ok(
    "exit_path_satisfied" in (live.get("checks") or {}),
    "live checks include exit_path_satisfied",
)
ok(
    "phase1_exit_green" in (live.get("checks") or {}),
    "live checks include phase1_exit_green",
)
ok(
    "no_dual_auth_markers" in (live.get("checks") or {}),
    "live checks include no_dual_auth_markers",
)
ok(
    "tasks_cut_or_readiness" in (live.get("checks") or {}),
    "live checks include tasks_cut_or_readiness",
)
ok(
    bool(live.get("checks", {}).get("phase1_exit_green")),
    "Phase 1 exit is still green on the live tree",
)
ok(
    bool(live.get("checks", {}).get("no_dual_auth_markers")),
    "live tree has no dual-auth markers in scanned code",
)
ok(
    bool(live.get("checks", {}).get("architecture_rails_present")),
    "charter ADR + skeleton rails are present",
)
# Board AC: initially may fail until 2B/2C — assert the harness stays fail-closed
# rather than forcing a green exit prematurely.
ok(
    live.get("passed") is False
    or live.get("checks", {}).get("exit_path_satisfied") is True,
    "live exit is either still red (expected mid-phase) or fully satisfied",
)
ok(
    proc.returncode in (0, 1),
    f"CLI exit code is 0/1 (got {proc.returncode})",
)
ok(
    (proc.returncode == 0) == bool(live.get("passed")),
    "CLI exit code matches report.passed",
)

# --- Fixture: neither path → fail ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_rails(root)
    report = gate.build_report(root, phase1_passed=True)
    ok(report["passed"] is False, "neither Path A nor Path B fails closed")
    ok(report["checks"]["exit_path_satisfied"] is False, "exit_path_satisfied false")
    ok(report["paths"]["path_a_auth_cut"] is False, "Path A false without Go evidence")
    ok(report["paths"]["path_b_documented_nogo"] is False, "Path B false without No-Go")

# --- Fixture: half-cut (live Auth unit without Go) → fail ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_rails(root)
    _write(root / gate.AUTH_SERVICE_PACKAGE, "app = None\n")
    _write(root / gate.AUTH_SERVICE_UNIT, "[Service]\nExecStart=/bin/true\n")
    report = gate.build_report(root, phase1_passed=True)
    ok(report["half_cut_detected"] is True, "Auth unit without Go is a half-cut")
    ok(
        report["checks"]["no_half_cut_network_facade"] is False,
        "half-cut fails no_half_cut_network_facade",
    )
    ok(report["passed"] is False, "half-cut cannot pass Phase 2 exit")

# --- Fixture: Path A complete → pass ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_rails(root)
    _write(
        root / gate.INDEPENDENCE_VERDICT,
        json.dumps({"verdict": "go", "recorded_by": "fixture"}) + "\n",
    )
    _write(root / gate.AUTH_SERVICE_PACKAGE, "app = None\n")
    _write(root / gate.AUTH_SERVICE_UNIT, "[Service]\nExecStart=/bin/true\n")
    _write(
        root / "deploy" / "Caddyfile",
        "plan.example.com {\n"
        "    handle /api/auth* {\n"
        "        reverse_proxy 127.0.0.1:8121\n"
        "    }\n"
        "}\n",
    )
    _write(root / gate.AUTH_CUT_PLAYBOOK, "# cutover + rollback\n")
    report = gate.build_report(root, phase1_passed=True)
    ok(report["paths"]["path_a_auth_cut"] is True, "Path A satisfied with full evidence")
    ok(report["passed"] is True, "Path A alone can pass Phase 2 exit")
    ok(report["checks"]["exit_path_satisfied"] is True, "exit_path_satisfied via Path A")

# --- Fixture: Path B complete → pass ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_rails(root)
    _write(
        root / gate.INDEPENDENCE_VERDICT,
        json.dumps({"verdict": "nogo", "recorded_by": "fixture"}) + "\n",
    )
    _write(
        root / gate.NOGO_RATIONALE,
        "# No-Go\n\nMeasured evidence: ARCH-MS-84 ops harness.\n",
    )
    report = gate.build_report(root, phase1_passed=True)
    ok(report["paths"]["path_b_documented_nogo"] is True, "Path B satisfied with No-Go")
    ok(report["passed"] is True, "Path B alone can pass Phase 2 exit")
    ok(
        report["path_b_checks"]["auth_remains_in_process"] is True,
        "Path B requires Auth still in-process",
    )

# --- Fixture: dual-auth marker fails closed ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_rails(root)
    _write(
        root / gate.INDEPENDENCE_VERDICT,
        json.dumps({"verdict": "nogo"}) + "\n",
    )
    _write(root / gate.NOGO_RATIONALE, "# No-Go\n")
    _write(root / "app_impl.py", "if os.environ.get('PM_GLOBAL_AUTH'):\n    pass\n")
    report = gate.build_report(root, phase1_passed=True)
    ok(report["checks"]["no_dual_auth_markers"] is False, "PM_GLOBAL_AUTH marker fails")
    ok(report["passed"] is False, "dual-auth markers block both exit paths")

# --- Fixture: Phase 1 red blocks exit even with Path B docs ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_rails(root)
    _write(
        root / gate.INDEPENDENCE_VERDICT,
        json.dumps({"verdict": "nogo"}) + "\n",
    )
    _write(root / gate.NOGO_RATIONALE, "# No-Go\n")
    report = gate.build_report(root, phase1_passed=False)
    ok(report["checks"]["phase1_exit_green"] is False, "injected Phase 1 red is visible")
    ok(report["passed"] is False, "Phase 1 red blocks Phase 2 exit")

if proc.stderr:
    print(proc.stderr)
if failed and live.get("error"):
    print("  DETAIL " + str(live["error"]))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
