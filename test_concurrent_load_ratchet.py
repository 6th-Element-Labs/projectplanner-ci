#!/usr/bin/env python3
"""HARDEN-64 — the concurrent-load gate is an ADR-0007 SLO ratchet, not a loose ceiling.

Bar-1 (HARDEN-50) built ``scripts/concurrent_load_gate.py`` and wired it into
``scripts/switchboard_ci.sh``.  Its ceilings were hard-coded, env-overridable
defaults sitting 5-40x above measured latency — a change could reintroduce
client-visible locks or regress write p99 most of the way to 100 ms and still
stay green.  Bar-2's regression guard closes that: the enforced ceilings now
live in the committed baseline ``perf/concurrent_load_slo.json`` and the gate
reads them.  This test proves the ratchet holds its three properties:

  1. every committed ``ratchet_ms`` stays strictly below its hard ``slo_ms``
     (the ratchet can never be relaxed to the SLO it defends);
  2. the gate enforces the committed ratchet, and env vars can TIGHTEN but never
     LOOSEN it (no shell/CI export can silently regress the gate);
  3. a measured statistic at/above its committed ratchet FAILS the gate — even
     when it is still under the hard SLO (the silent-regression case).

Kept hermetic and bounded: two short gate subprocess runs plus two that
short-circuit at config load.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GATE = ROOT / "scripts" / "concurrent_load_gate.py"
BASELINE = ROOT / "perf" / "concurrent_load_slo.json"
METRICS = ("writes", "search_tasks", "board_summary", "all_calls")
passed = failed = 0


def ok(condition: bool, message: str) -> bool:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if condition:
        passed += 1
    else:
        failed += 1
    return bool(condition)


def run_gate(env_extra: dict[str, str], rounds: int = 3):
    """Run the gate as a subprocess; return (returncode, stdout+stderr, report_or_None)."""
    env = dict(os.environ)
    env["PM_AUTH_MODE"] = "dev-open"
    env["LOAD_GATE_ROUNDS"] = str(rounds)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        report_path = handle.name
    env["CONCURRENT_LOAD_REPORT"] = report_path
    env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    report = None
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = None
    finally:
        Path(report_path).unlink(missing_ok=True)
    return proc.returncode, (proc.stdout + proc.stderr), report


# --- 1. The committed baseline is a well-formed ratchet below the hard SLO -----
baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
metrics = baseline.get("metrics", {})
ok(isinstance(metrics, dict) and all(name in metrics for name in METRICS),
   f"baseline {BASELINE.name} defines all four metrics")
for name in METRICS:
    entry = metrics.get(name, {})
    ratchet = entry.get("ratchet_ms")
    slo = entry.get("slo_ms")
    ok(isinstance(ratchet, (int, float)) and isinstance(slo, (int, float))
       and 0 < ratchet < slo,
       f"{name}: committed ratchet {ratchet}ms stays below hard SLO {slo}ms")
# The task's named invariant: a write p99 >= 100 ms must fail. Ratchet <= SLO
# means the gate trips at or before 100 ms, so the SLO is enforced a fortiori.
ok(metrics["writes"]["ratchet_ms"] <= 100.0,
   "writes ratchet enforces the hard 'p99 >= 100ms fails' SLO a fortiori")

# --- 2. Gate enforces the committed ratchet; env can tighten, never loosen -----
# One run that also *attempts* to loosen the write ceiling to 999999ms: the gate
# must ignore the loosening and enforce the committed ratchet, and still pass.
code, out, report = run_gate({"LOAD_GATE_WRITE_P99_MS": "999999"})
ok(report is not None, "loosen-attempt run produced a report")
if report is not None:
    committed_write = metrics["writes"]["ratchet_ms"]
    enforced = report.get("slo_ratchet", {}).get("enforced_ms", {})
    ok(report["metrics"]["writes"]["budget_ms"] == committed_write
       and enforced.get("writes_p99") == committed_write,
       f"env cannot loosen: write ceiling held at committed {committed_write}ms, not 999999ms")
    ok(enforced.get("search_tasks_p99") == metrics["search_tasks"]["ratchet_ms"]
       and enforced.get("board_summary_p95") == metrics["board_summary"]["ratchet_ms"]
       and enforced.get("all_calls_max") == metrics["all_calls"]["ratchet_ms"],
       "gate reads every enforced ceiling from the committed baseline")
    ok(code == 0 and report["ok"] is True,
       "gate PASSes against the committed ratchet under normal latency")

# --- 3. A sub-SLO regression FAILS the gate (the silent-regression case) -------
# Tighten the write ceiling below real latency to simulate a regression that is
# still under the hard SLO. The gate must fail non-zero and name the violation.
code, out, _ = run_gate({"LOAD_GATE_WRITE_P99_MS": "0.01"})
ok(code == 1 and "writes p99" in out,
   "a write p99 above the (tightened) ratchet fails the gate with a named violation")

# --- 4. A baseline that relaxes the ratchet to/above the SLO is rejected -------
with tempfile.TemporaryDirectory() as tmp:
    bad = Path(tmp) / "bad_slo.json"
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    payload["metrics"]["writes"]["ratchet_ms"] = payload["metrics"]["writes"]["slo_ms"]
    bad.write_text(json.dumps(payload), encoding="utf-8")
    code, out, _ = run_gate({"CONCURRENT_LOAD_SLO_BASELINE": str(bad)}, rounds=1)
    ok(code != 0 and "below the hard SLO" in out,
       "gate refuses a baseline whose ratchet is relaxed up to the hard SLO")

    missing = Path(tmp) / "does_not_exist.json"
    code, out, _ = run_gate({"CONCURRENT_LOAD_SLO_BASELINE": str(missing)}, rounds=1)
    ok(code != 0 and "baseline missing" in out,
       "gate refuses to run with a missing ratchet baseline")

print(f"\nconcurrent-load ratchet: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
