#!/usr/bin/env python3
"""HARDEN-50 — concurrent MCP agent-path regression and SLO gate.

Replays the production incident shape in a hermetic SQLite/WAL database:

* six filtered ``search_tasks`` calls;
* three task writes;
* one dashboard-style board snapshot;
* eight concurrent agent workers by default (configurable).

The burst is repeated so the gate can defend p95/p99 latency instead of one
optimistic sample.  This exercises the server-side implementations used by the
MCP tools (``agent._search_tasks``, ``store.add_comment``, and
``agent.board_summary_text``); network/TLS/client bridge time is deliberately
outside this CI measurement.

Environment overrides are intended for deliberate capacity experiments.  CI
uses the deliverable SLOs as defaults:

  LOAD_GATE_AGENTS=8
  LOAD_GATE_ROUNDS=20
  LOAD_GATE_SEARCH_P99_MS=150
  LOAD_GATE_BOARD_P95_MS=400
  LOAD_GATE_WRITE_P99_MS=100
  LOAD_GATE_CALL_MAX_MS=5000
  CONCURRENT_LOAD_REPORT=.artifacts/concurrent-load-report.json
"""
from __future__ import annotations

import concurrent.futures
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        raise SystemExit(f"{name} must be an integer")
    if value < minimum:
        raise SystemExit(f"{name} must be >= {minimum}")
    return value


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        raise SystemExit(f"{name} must be a number")
    if value <= 0:
        raise SystemExit(f"{name} must be > 0")
    return value


def percentile(values: list[float], rank: float) -> float:
    """Nearest-rank percentile; tail samples never get interpolated away."""
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    index = max(0, math.ceil((rank / 100.0) * len(ordered)) - 1)
    return ordered[index]


AGENTS = _env_int("LOAD_GATE_AGENTS", 8, minimum=6)
ROUNDS = _env_int("LOAD_GATE_ROUNDS", 20)
SEARCH_P99_MS = _env_float("LOAD_GATE_SEARCH_P99_MS", 150.0)
BOARD_P95_MS = _env_float("LOAD_GATE_BOARD_P95_MS", 400.0)
WRITE_P99_MS = _env_float("LOAD_GATE_WRITE_P99_MS", 100.0)
CALL_MAX_MS = _env_float("LOAD_GATE_CALL_MAX_MS", 5_000.0)
SEARCHES_PER_ROUND = 6
WRITES_PER_ROUND = 3
TASKS_PER_LANE = 3
PROJECT = "switchboard"

_tmp = tempfile.mkdtemp(prefix="concurrent-load-gate-")
os.environ["PM_DB_PATH"] = os.path.join(_tmp, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_tmp, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_tmp, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_tmp, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _tmp
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402
import store  # noqa: E402


def _timed(kind: str, operation: Callable[[], Any], start: threading.Event) -> dict[str, Any]:
    start.wait()
    began = time.perf_counter()
    try:
        result = operation()
        elapsed_ms = (time.perf_counter() - began) * 1_000.0
        return {"kind": kind, "elapsed_ms": elapsed_ms, "ok": result is not None}
    except Exception as exc:  # The report must preserve the original failing signal.
        elapsed_ms = (time.perf_counter() - began) * 1_000.0
        return {
            "kind": kind,
            "elapsed_ms": elapsed_ms,
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _search(lane: str) -> list[dict[str, Any]]:
    rows = agent._search_tasks({"workstream": lane, "query": "load task"}, project=PROJECT)
    if len(rows) != TASKS_PER_LANE:
        raise AssertionError(
            f"filtered search for {lane} returned {len(rows)} tasks; expected {TASKS_PER_LANE}"
        )
    return rows


def _write(task_id: str, round_number: int) -> dict[str, Any]:
    row = store.add_comment(
        task_id,
        actor="load-gate/agent",
        text=f"concurrent load round {round_number}",
        project=PROJECT,
    )
    if not row:
        raise AssertionError(f"write target disappeared: {task_id}")
    return row


def _board_snapshot() -> str:
    text = agent.board_summary_text(project=PROJECT)
    if not text or "LOAD0-1" not in text:
        raise AssertionError("board snapshot did not contain the seeded tasks")
    return text


def _run_round(round_number: int, write_targets: list[str]) -> list[dict[str, Any]]:
    start = threading.Event()
    operations: list[tuple[str, Callable[[], Any]]] = []
    for index in range(SEARCHES_PER_ROUND):
        lane = f"LOAD{index}"
        operations.append(("search_tasks", lambda lane=lane: _search(lane)))
    for index in range(WRITES_PER_ROUND):
        task_id = write_targets[index]
        operations.append(
            ("write", lambda task_id=task_id: _write(task_id, round_number))
        )
    operations.append(("board_summary", _board_snapshot))

    # The configured agent workers reproduce the production concurrency ceiling.
    # The dashboard snapshot shares the pool, just as it shares the live process.
    with concurrent.futures.ThreadPoolExecutor(max_workers=AGENTS) as executor:
        futures = [executor.submit(_timed, kind, operation, start)
                   for kind, operation in operations]
        start.set()
        return [future.result(timeout=(CALL_MAX_MS / 1_000.0) + 2.0) for future in futures]


def _metric(samples: list[float], rank: float, budget_ms: float) -> dict[str, Any]:
    return {
        "samples": len(samples),
        "p50_ms": round(percentile(samples, 50), 3),
        f"p{int(rank)}_ms": round(percentile(samples, rank), 3),
        "max_ms": round(max(samples), 3),
        "budget_ms": budget_ms,
    }


def main() -> int:
    all_results: list[dict[str, Any]] = []
    try:
        store.init_db(PROJECT)
        # Six small filtered lanes match real agent usage while eight total lanes
        # keep unrelated rows in the database and reserve distinct write targets.
        for lane_index in range(AGENTS):
            for task_index in range(TASKS_PER_LANE):
                store.create_task(
                    {
                        "workstream_id": f"LOAD{lane_index}",
                        "title": f"Load task {lane_index}-{task_index}",
                        "description": "Hermetic concurrent-load gate fixture",
                    },
                    actor="load-gate/setup",
                    project=PROJECT,
                )
        write_lane = f"LOAD{AGENTS - 1}"
        write_targets = [f"{write_lane}-{index}" for index in range(1, WRITES_PER_ROUND + 1)]

        # Discard one cold-start burst.  The SLO describes steady-state agent
        # calls; import/schema/bootstrap time is a separate deployment concern.
        _run_round(-1, write_targets)
        for round_number in range(ROUNDS):
            all_results.extend(_run_round(round_number, write_targets))
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)

    by_kind = {
        kind: [sample for sample in all_results if sample["kind"] == kind]
        for kind in ("search_tasks", "write", "board_summary")
    }
    errors = [sample for sample in all_results if not sample.get("ok")]
    locked_errors = [
        sample for sample in errors
        if "locked" in (sample.get("error") or "").lower()
        or "busy" in (sample.get("error") or "").lower()
    ]
    search_ms = [sample["elapsed_ms"] for sample in by_kind["search_tasks"]]
    write_ms = [sample["elapsed_ms"] for sample in by_kind["write"]]
    board_ms = [sample["elapsed_ms"] for sample in by_kind["board_summary"]]
    every_ms = [sample["elapsed_ms"] for sample in all_results]

    metrics = {
        "search_tasks": _metric(search_ms, 99, SEARCH_P99_MS),
        "writes": _metric(write_ms, 99, WRITE_P99_MS),
        "board_summary": _metric(board_ms, 95, BOARD_P95_MS),
        "all_calls": {"samples": len(every_ms), "max_ms": round(max(every_ms), 3),
                      "budget_ms": CALL_MAX_MS},
    }
    violations: list[str] = []
    if errors:
        violations.append(f"{len(errors)} operation(s) failed")
    if locked_errors:
        violations.append(f"{len(locked_errors)} client-visible database lock error(s)")
    if metrics["search_tasks"]["p99_ms"] >= SEARCH_P99_MS:
        violations.append(
            f"search_tasks p99 {metrics['search_tasks']['p99_ms']}ms >= {SEARCH_P99_MS}ms"
        )
    if metrics["writes"]["p99_ms"] >= WRITE_P99_MS:
        violations.append(f"writes p99 {metrics['writes']['p99_ms']}ms >= {WRITE_P99_MS}ms")
    if metrics["board_summary"]["p95_ms"] >= BOARD_P95_MS:
        violations.append(
            f"board_summary p95 {metrics['board_summary']['p95_ms']}ms >= {BOARD_P95_MS}ms"
        )
    if metrics["all_calls"]["max_ms"] >= CALL_MAX_MS:
        violations.append(f"call max {metrics['all_calls']['max_ms']}ms >= {CALL_MAX_MS}ms")

    report = {
        "schema": "switchboard.concurrent_load_gate.v1",
        "ok": not violations,
        "scenario": {
            "agents": AGENTS,
            "rounds": ROUNDS,
            "searches_per_round": SEARCHES_PER_ROUND,
            "writes_per_round": WRITES_PER_ROUND,
            "board_snapshots_per_round": 1,
            "tasks_per_filtered_lane": TASKS_PER_LANE,
            "storage": "hermetic sqlite WAL",
            "scope": "server-side; excludes network, TLS, and client bridge time",
        },
        "metrics": metrics,
        "error_count": len(errors),
        "database_lock_error_count": len(locked_errors),
        "errors": errors[:20],
        "violations": violations,
    }
    report_text = json.dumps(report, indent=2, sort_keys=True)
    print(report_text)

    output = (os.environ.get("CONCURRENT_LOAD_REPORT") or "").strip()
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report_text + "\n", encoding="utf-8")
        print(f"concurrent-load report: {path}")

    if violations:
        print("concurrent-load SLO gate: FAIL", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1
    print("concurrent-load SLO gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
