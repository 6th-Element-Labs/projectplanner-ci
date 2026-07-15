#!/usr/bin/env python3
"""ARCH-MS-86: Phase 3 exit gate harness proof (ADR-0012 Decision 5).

Proves ``scripts/arch_ms_phase3_exit_gate.py`` is importable, emits a versioned
schema, implements Path A ∨ Path B with fail-closed half-cut / network-wrap
detection, and stays CI-safe while the live tree may still report
``passed=false`` until 3B0/3B evidence lands (board AC).
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
    path = ROOT / "scripts/arch_ms_phase3_exit_gate.py"
    spec = importlib.util.spec_from_file_location("arch_ms_phase3_exit_gate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_rails(root: Path) -> None:
    gate = _load_gate()
    _write(root / gate.CHARTER_ADR, "# ADR-0012\n")
    _write(root / gate.PHASE2_GATE, "print('fixture')\n")
    _write(
        root / "deploy" / "Caddyfile",
        "plan.example.com {\n"
        "    handle {\n"
        "        reverse_proxy 127.0.0.1:8110\n"
        "    }\n"
        "}\n",
    )
    _write(root / "deploy" / "projectplanner.service", "[Service]\nExecStart=/bin/true\n")


gate = _load_gate()

ok(gate.SCHEMA == "switchboard.arch_ms_phase3_exit.v1", "schema constant is versioned")
ok(
    (ROOT / "scripts/arch_ms_phase3_exit_gate.py").is_file(),
    "scripts/arch_ms_phase3_exit_gate.py exists",
)

# --- Live tree: well-formed report; may still be red until 3B0/3B ---
proc = subprocess.run(
    [sys.executable, str(ROOT / "scripts/arch_ms_phase3_exit_gate.py")],
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
    "phase2_exit_green" in (live.get("checks") or {}),
    "live checks include phase2_exit_green",
)
ok(
    "adr_0012_present" in (live.get("checks") or {}),
    "live checks include adr_0012_present",
)
ok(
    "no_network_wrap_with_store_imports" in (live.get("checks") or {}),
    "live checks include no_network_wrap_with_store_imports",
)
ok(
    bool(live.get("checks", {}).get("phase2_exit_green")),
    "Phase 2 exit is still green on the live tree",
)
ok(
    bool(live.get("checks", {}).get("adr_0012_present")),
    "ADR-0012 charter is present on the live tree",
)
ok(
    bool(live.get("checks", {}).get("architecture_rails_present")),
    "charter ADR + Phase 2 gate rails are present",
)
# Board AC: initially may fail until 3B0/3B — assert the harness stays fail-closed
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
ok(
    live.get("passed") is False,
    "live tree is still red until 3B0/3B evidence (board AC for harness)",
)

# --- Fixture: neither path → fail ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_rails(root)
    report = gate.build_report(root, phase2_passed=True)
    ok(report["passed"] is False, "neither Path A nor Path B fails closed")
    ok(report["checks"]["exit_path_satisfied"] is False, "exit_path_satisfied false")
    ok(report["paths"]["path_a_tasks_cut"] is False, "Path A false without Go evidence")
    ok(report["paths"]["path_b_documented_nogo"] is False, "Path B false without No-Go")

# --- Fixture: half-cut (live Tasks unit without Go) → fail ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_rails(root)
    _write(root / gate.TASKS_SERVICE_PACKAGE, "app = None\n")
    _write(root / gate.TASKS_SERVICE_UNIT, "[Service]\nExecStart=/bin/true\n")
    report = gate.build_report(root, phase2_passed=True)
    ok(report["half_cut_detected"] is True, "Tasks unit without Go is a half-cut")
    ok(
        report["checks"]["no_half_cut_network_facade"] is False,
        "half-cut fails no_half_cut_network_facade",
    )
    ok(report["passed"] is False, "half-cut cannot pass Phase 3 exit")

# --- Fixture: network-wrap while store imports remain → fail ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_rails(root)
    _write(
        root / gate.INDEPENDENCE_VERDICT,
        json.dumps({"verdict": "go", "recorded_by": "fixture"}) + "\n",
    )
    _write(
        root / gate.TASKS_SERVICE_PACKAGE,
        "from store import create_task\n\napp = None\n",
    )
    _write(root / gate.TASKS_SERVICE_UNIT, "[Service]\nExecStart=/bin/true\n")
    _write(
        root / "deploy" / "Caddyfile",
        "plan.example.com {\n"
        "    handle /api/tasks* {\n"
        "        reverse_proxy 127.0.0.1:8122\n"
        "    }\n"
        "    handle /txp/v1/claim* {\n"
        "        reverse_proxy 127.0.0.1:8122\n"
        "    }\n"
        "}\n",
    )
    _write(
        root / "deploy" / "projectplanner.service",
        "[Service]\nEnvironment=PM_TASKS_HTTP_PRIMARY=service\n",
    )
    _write(root / gate.TASKS_CUT_PLAYBOOK, "# cutover + rollback\n")
    report = gate.build_report(root, phase2_passed=True)
    ok(
        report["network_wrap_with_store_imports"] is True,
        "store imports + network cut is network-wrap",
    )
    ok(
        report["checks"]["no_network_wrap_with_store_imports"] is False,
        "network-wrap fails shared check",
    )
    ok(report["passed"] is False, "network-wrap with store imports fails closed")

# --- Fixture: Path A complete → pass ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_rails(root)
    _write(
        root / gate.INDEPENDENCE_VERDICT,
        json.dumps({"verdict": "go", "recorded_by": "fixture"}) + "\n",
    )
    _write(root / gate.TASKS_SERVICE_PACKAGE, "app = None\n")
    _write(root / gate.TASKS_SERVICE_UNIT, "[Service]\nExecStart=/bin/true\n")
    _write(
        root / "deploy" / "Caddyfile",
        "plan.example.com {\n"
        "    handle /api/tasks* {\n"
        "        reverse_proxy 127.0.0.1:8122\n"
        "    }\n"
        "    handle /txp/v1/claim* {\n"
        "        reverse_proxy 127.0.0.1:8122\n"
        "    }\n"
        "}\n",
    )
    _write(
        root / "deploy" / "projectplanner.service",
        "[Service]\nEnvironment=PM_TASKS_HTTP_PRIMARY=service\n",
    )
    _write(root / gate.TASKS_CUT_PLAYBOOK, "# cutover + rollback\n")
    report = gate.build_report(root, phase2_passed=True)
    ok(report["paths"]["path_a_tasks_cut"] is True, "Path A satisfied with full evidence")
    ok(report["passed"] is True, "Path A alone can pass Phase 3 exit")
    ok(report["checks"]["exit_path_satisfied"] is True, "exit_path_satisfied via Path A")
    ok(
        report["path_a_checks"]["dual_strip_present"] is True,
        "Path A requires dual-strip env evidence",
    )
    ok(
        report["path_a_checks"]["caddy_routes_claim_txp"] is True,
        "Path A requires claim-only TXP Caddy route",
    )

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
        "# No-Go\n\nMeasured evidence: ARCH-MS-89 ops harness.\n",
    )
    report = gate.build_report(root, phase2_passed=True)
    ok(report["paths"]["path_b_documented_nogo"] is True, "Path B satisfied with No-Go")
    ok(report["passed"] is True, "Path B alone can pass Phase 3 exit")
    ok(
        report["path_b_checks"]["tasks_remains_in_process"] is True,
        "Path B requires Tasks still in-process",
    )

# --- Fixture: Conditional Go (G6 pending) must not authorize live cut ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_rails(root)
    _write(
        root / gate.INDEPENDENCE_VERDICT,
        json.dumps({
            "verdict": "go",
            "decision": "conditional_go",
            "operator_g6_required": True,
            "inputs": {"G6_operator_go": False},
            "recorded_by": "fixture",
        }) + "\n",
    )
    _write(root / gate.TASKS_SERVICE_PACKAGE, "app = None\n")
    _write(root / gate.TASKS_SERVICE_UNIT, "[Service]\nExecStart=/bin/true\n")
    report = gate.build_report(root, phase2_passed=True)
    ok(report["half_cut_detected"] is True,
       "Conditional Go + live unit is still a half-cut")
    ok(report["independence"].get("process_cut_authorized") is False,
       "Conditional Go does not authorize process cut")
    ok(report["path_a_checks"].get("independence_verdict_go") is False,
       "Path A independence_verdict_go requires authorized Go")
    ok(report["passed"] is False, "Conditional Go cannot pass Path A without G6")

# --- Fixture: Phase 2 red blocks exit even with Path B docs ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_rails(root)
    _write(
        root / gate.INDEPENDENCE_VERDICT,
        json.dumps({"verdict": "nogo"}) + "\n",
    )
    _write(root / gate.NOGO_RATIONALE, "# No-Go\n")
    report = gate.build_report(root, phase2_passed=False)
    ok(report["checks"]["phase2_exit_green"] is False, "injected Phase 2 red is visible")
    ok(report["passed"] is False, "Phase 2 red blocks Phase 3 exit")

# --- Fixture: missing ADR-0012 blocks exit ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_rails(root)
    (root / gate.CHARTER_ADR).unlink()
    _write(
        root / gate.INDEPENDENCE_VERDICT,
        json.dumps({"verdict": "nogo"}) + "\n",
    )
    _write(root / gate.NOGO_RATIONALE, "# No-Go\n")
    report = gate.build_report(root, phase2_passed=True)
    ok(report["checks"]["adr_0012_present"] is False, "missing ADR-0012 is visible")
    ok(report["passed"] is False, "missing ADR-0012 blocks Phase 3 exit")

if proc.stderr:
    print(proc.stderr)
if failed and live.get("error"):
    print("  DETAIL " + str(live["error"]))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
