#!/usr/bin/env python3
"""ARCH-19 — the cross-process load gate is an ADR-0007 SLO ratchet, not a loose ceiling.

``scripts/cross_process_load_gate.py`` measures the topology the in-process
gate deliberately excludes: separate OS processes (app + MCP server + timer
job) writing one shared SQLite directory, arbitrated by WAL + busy_timeout +
the store retry loop instead of the per-process single-writer queue.  Its
ceilings live in the committed baseline ``perf/cross_process_load_slo.json``.
This test proves the ratchet holds the same properties HARDEN-64 proved for
the in-process gate:

  1. every committed ``ratchet_ms`` stays strictly below its hard ``slo_ms``
     (the ratchet can never be relaxed to the SLO it defends);
  2. the gate enforces the committed ratchet, and env vars can TIGHTEN but
     never LOOSEN it (no shell/CI export can silently regress the gate);
  3. a measured statistic at/above its committed ratchet FAILS the gate —
     even when it is still under the hard SLO (the silent-regression case);
  4. a baseline that relaxes or loses the ratchet is rejected at config load.

Kept hermetic and bounded: two short multi-process gate runs (2 workers x 2
rounds) plus two runs that short-circuit at config load.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GATE = ROOT / "scripts" / "cross_process_load_gate.py"
BASELINE = ROOT / "perf" / "cross_process_load_slo.json"
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


def run_gate(env_extra: dict[str, str], rounds: int = 2):
    """Run the gate as a subprocess; return (returncode, stdout+stderr, report_or_None)."""
    env = dict(os.environ)
    env["PM_AUTH_MODE"] = "dev-open"
    env["XPROC_LOAD_GATE_PROCS"] = "2"
    env["XPROC_LOAD_GATE_ROUNDS"] = str(rounds)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        report_path = handle.name
    env["CROSS_PROCESS_LOAD_REPORT"] = report_path
    env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
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

# --- 2. Gate enforces the committed ratchet; env can tighten, never loosen -----
# One real 2-process run that also *attempts* to loosen the write ceiling to
# 999999ms: the gate must ignore the loosening, enforce the committed ratchet,
# report the cross-process scenario, and still pass with zero lock errors.
code, out, report = run_gate({"XPROC_LOAD_GATE_WRITE_P99_MS": "999999"})
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
    ok(report["scenario"]["processes"] == 2,
       "gate ran genuinely separate worker processes")
    ok(code == 0 and report["ok"] is True
       and report["database_lock_error_count"] == 0,
       "gate PASSes with zero client-visible lock errors under normal latency")

# --- 3. A sub-SLO regression FAILS the gate (the silent-regression case) -------
# Tighten the write ceiling below real latency to simulate a regression that is
# still under the hard SLO. The gate must fail non-zero and name the violation.
code, out, _ = run_gate({"XPROC_LOAD_GATE_WRITE_P99_MS": "0.01"})
ok(code == 1 and "writes p99" in out,
   "a write p99 above the (tightened) ratchet fails the gate with a named violation")

# --- 4. A baseline that relaxes the ratchet to/above the SLO is rejected -------
with tempfile.TemporaryDirectory() as tmp:
    bad = Path(tmp) / "bad_slo.json"
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    payload["metrics"]["writes"]["ratchet_ms"] = payload["metrics"]["writes"]["slo_ms"]
    bad.write_text(json.dumps(payload), encoding="utf-8")
    code, out, _ = run_gate({"CROSS_PROCESS_LOAD_SLO_BASELINE": str(bad)}, rounds=1)
    ok(code != 0 and "below the hard SLO" in out,
       "gate refuses a baseline whose ratchet is relaxed up to the hard SLO")

    missing = Path(tmp) / "does_not_exist.json"
    code, out, _ = run_gate({"CROSS_PROCESS_LOAD_SLO_BASELINE": str(missing)}, rounds=1)
    ok(code != 0 and "baseline missing" in out,
       "gate refuses to run with a missing ratchet baseline")

print(f"\ncross-process load ratchet: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
