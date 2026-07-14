#!/usr/bin/env python3
"""ARCH-MS-53: residual ceilings forbid rename-as-done in the Phase 1 exit gate."""
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
    path = ROOT / "scripts/arch_ms_phase1_exit_gate.py"
    spec = importlib.util.spec_from_file_location("arch_ms_phase1_exit_gate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write(path: Path, lines: int, body: str = "x = 1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep files syntactically valid for AST checks on store.py.
    content = "\n".join([body] * max(lines, 1)) + "\n"
    path.write_text(content, encoding="utf-8")


def _seed_required_artifacts(root: Path) -> None:
    gate = _load_gate()
    for rel in gate.REQUIRED_ARTIFACTS:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# artifact\n", encoding="utf-8")


def _thin_facades(root: Path) -> None:
    # store.py: only __getattr__/__dir__ allowed; no SQL markers.
    store = (
        "def __getattr__(name):\n"
        "    raise AttributeError(name)\n"
        "\n"
        "def __dir__():\n"
        "    return []\n"
    )
    (root / "store.py").write_text(store, encoding="utf-8")
    (root / "app.py").write_text("app = None\n", encoding="utf-8")
    (root / "mcp_server.py").write_text("mcp = None\n", encoding="utf-8")


gate = _load_gate()

# --- Policy constants ---
ok(gate.STORE_FACADE_MAX == 200, "store façade ceiling remains 200")
ok(gate.ADAPTER_MAX == 500, "app/mcp adapter ceiling remains 500")
ok(gate.STORE_RESIDUAL_MAX == 200, "store residual ceiling is 200 (forbid fat shell dump)")
ok(gate.APP_RESIDUAL_MAX == 500, "app residual ceiling is 500")
ok(gate.MCP_RESIDUAL_MAX == 500, "mcp residual ceiling is 500")
ok(
    "src/switchboard/storage/repositories/shell.py" not in gate.REQUIRED_ARTIFACTS
    and "app_impl.py" not in gate.REQUIRED_ARTIFACTS
    and "mcp_server_impl.py" not in gate.REQUIRED_ARTIFACTS,
    "residuals are not required artifacts (rename-only must not be endorsed)",
)

# --- Scenario: PR #440 rename-as-done (thin entry + fat residuals) ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_required_artifacts(root)
    _thin_facades(root)
    _write(root / "src/switchboard/storage/repositories/shell.py", 7488)
    _write(root / "app_impl.py", 3064)
    _write(root / "mcp_server_impl.py", 2888)
    report = gate.build_report(root)
    ok(report["checks"]["store_facade_ceiling"] is True, "rename-as-done still has thin store façade")
    ok(report["checks"]["app_adapter_ceiling"] is True, "rename-as-done still has thin app adapter")
    ok(report["checks"]["mcp_adapter_ceiling"] is True, "rename-as-done still has thin mcp adapter")
    ok(report["rename_as_done"] is True, "rename-as-done pattern is detected")
    ok(report["checks"]["rename_as_done_forbidden"] is False, "rename-as-done check fails closed")
    ok(report["checks"]["store_residual_ceiling"] is False, "fat shell.py residual fails ceiling")
    ok(report["checks"]["app_residual_ceiling"] is False, "fat app_impl.py residual fails ceiling")
    ok(report["checks"]["mcp_residual_ceiling"] is False, "fat mcp_server_impl.py residual fails ceiling")
    ok(report["passed"] is False, "PR #440-style façade dump alone does not pass Phase 1 exit")

# --- Scenario: drained residuals deleted; thin façades ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_required_artifacts(root)
    _thin_facades(root)
    report = gate.build_report(root)
    ok(report["residuals"]["store"]["deleted"] is True, "absent store residual counts as drained")
    ok(report["residuals"]["app"]["deleted"] is True, "absent app residual counts as drained")
    ok(report["residuals"]["mcp"]["deleted"] is True, "absent mcp residual counts as drained")
    ok(report["checks"]["store_residual_ceiling"] is True, "deleted store residual passes ceiling")
    ok(report["checks"]["app_residual_ceiling"] is True, "deleted app residual passes ceiling")
    ok(report["checks"]["mcp_residual_ceiling"] is True, "deleted mcp residual passes ceiling")
    ok(report["rename_as_done"] is False, "drained tree is not rename-as-done")
    ok(report["checks"]["rename_as_done_forbidden"] is True, "drained tree satisfies rename forbid check")
    ok(report["passed"] is True, "thin façades with residuals deleted pass Phase 1 exit")

# --- Scenario: residuals present but under shrinking ceilings ---
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    _seed_required_artifacts(root)
    _thin_facades(root)
    _write(root / "src/switchboard/storage/repositories/shell.py", 50)
    _write(root / "app_impl.py", 100)
    _write(root / "mcp_server_impl.py", 100)
    report = gate.build_report(root)
    ok(report["checks"]["store_residual_ceiling"] is True, "small shell residual under ceiling passes")
    ok(report["checks"]["app_residual_ceiling"] is True, "small app_impl residual under ceiling passes")
    ok(report["checks"]["mcp_residual_ceiling"] is True, "small mcp residual under ceiling passes")
    ok(report["passed"] is True, "thin façades + shrunk residuals pass Phase 1 exit")

# --- Script is executable and emits versioned JSON on the live tree ---
proc = subprocess.run(
    [sys.executable, str(ROOT / "scripts/arch_ms_phase1_exit_gate.py")],
    cwd=ROOT, text=True, capture_output=True,
)
try:
    live = json.loads(proc.stdout)
except json.JSONDecodeError:
    live = {"passed": False, "error": proc.stdout or proc.stderr}

ok(live.get("schema") == "switchboard.arch_ms_phase1_exit.v1",
   "live exit evidence has versioned schema switchboard.arch_ms_phase1_exit.v1")
ok("store_residual_ceiling" in live.get("checks", {}),
   "live report includes store residual ceiling check")
ok("rename_as_done_forbidden" in live.get("checks", {}),
   "live report includes rename-as-done forbid check")
# Current master still has fat entry monoliths — exit must not greenwash that.
ok(live.get("passed") is False,
   "current tree does not falsely pass Phase 1 exit before residual drain")

if proc.stderr:
    print(proc.stderr)
if failed and live.get("error"):
    print("  DETAIL " + str(live["error"]))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
