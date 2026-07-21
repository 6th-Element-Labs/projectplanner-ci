#!/usr/bin/env python3
"""ARCH-19 — cross-process SQLite writer-contention regression and SLO gate.

The in-process gate (``scripts/concurrent_load_gate.py``) measures the
thread-concurrency path inside ONE process, where the single-writer queue
(PERF-2) serializes every mutation before SQLite ever sees it.  Production is
not one process: the web app, the MCP server, the coordinator autopilot, and
the systemd timer jobs each import ``store`` and write the same SQLite files
directly.  Across OS processes the write queue cannot serialize anything —
cross-process writers are arbitrated only by WAL + ``busy_timeout`` + the
store's lock-retry loop.  That topology is exactly the pressure ARCH-19 names
as the Postgres trigger, and until this gate it was unmeasured.

This gate replays it hermetically: N worker processes (default 3 — app + MCP
server + one timer job is the steady-state concurrent-writer set) run the
same agent-path mix against one shared database directory:

* three filtered ``search_tasks`` calls per round;
* three task writes per round (each worker owns a distinct write lane);
* one dashboard-style board snapshot per round;
* a small per-worker thread pool so each process is internally concurrent,
  as the live uvicorn/MCP processes are.

Workers rendezvous on a start signal so their bursts overlap, then free-run —
matching production, where processes are not round-synchronized.  The
single-writer queue stays at its production default (enabled) in every
worker; what this gate adds over the in-process gate is specifically the
WAL-level contention BETWEEN the queues.

The enforced ceilings live in the committed ratchet baseline
``perf/cross_process_load_slo.json`` (same ADR-0007 ratchet philosophy as
HARDEN-64: prove it once, then never silently regress).  ``ratchet_ms`` is
the enforced ceiling; ``slo_ms`` is the hard invariant the ratchet may never
be relaxed to (the loader asserts ``ratchet_ms < slo_ms``).  A measured
statistic at/above its ratchet — or ANY client-visible database-lock error
that escapes the store's retry loop — fails the gate.  A sustained breach
here is ARCH-19 evidence that the shared-SQLite topology is out of headroom.

Env overrides are tighten-only, exactly like the in-process gate:

  XPROC_LOAD_GATE_PROCS=3          worker OS processes (min 2)
  XPROC_LOAD_GATE_THREADS=3        threads inside each worker
  XPROC_LOAD_GATE_ROUNDS=20        measured rounds per worker
  XPROC_LOAD_GATE_SEARCH_P99_MS=<= committed ratchet, tighten-only>
  XPROC_LOAD_GATE_BOARD_P95_MS=<= committed ratchet, tighten-only>
  XPROC_LOAD_GATE_WRITE_P99_MS=<= committed ratchet, tighten-only>
  XPROC_LOAD_GATE_CALL_MAX_MS=<= committed ratchet, tighten-only>
  CROSS_PROCESS_LOAD_SLO_BASELINE=perf/cross_process_load_slo.json (override path)
  CROSS_PROCESS_LOAD_REPORT=.artifacts/cross-process-load-report.json
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO_ROOT / "perf" / "cross_process_load_slo.json"

SEARCHES_PER_ROUND = 3
WRITES_PER_ROUND = 3
TASKS_PER_LANE = 3
PROJECT = "switchboard"
READY_TIMEOUT_S = 60.0


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


def load_ratchet(path: Path | None = None) -> dict[str, dict[str, float]]:
    """Read the committed cross-process SLO ratchet baseline.

    Returns ``{metric: {"ratchet_ms": float, "slo_ms": float}}``.  Any file
    that sets ``ratchet_ms >= slo_ms`` is a config error — that is precisely
    the silent loosening this baseline exists to forbid.
    """
    baseline = Path(os.environ.get("CROSS_PROCESS_LOAD_SLO_BASELINE", "") or (path or DEFAULT_BASELINE))
    try:
        raw = json.loads(baseline.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"cross-process load SLO ratchet baseline missing: {baseline}")
    except (ValueError, OSError) as exc:
        raise SystemExit(f"cross-process load SLO ratchet baseline unreadable ({baseline}): {exc}")
    metrics = raw.get("metrics")
    if not isinstance(metrics, dict):
        raise SystemExit(f"{baseline}: 'metrics' object is required")
    ceilings: dict[str, dict[str, float]] = {}
    for name in ("writes", "search_tasks", "board_summary", "all_calls"):
        entry = metrics.get(name)
        if not isinstance(entry, dict):
            raise SystemExit(f"{baseline}: metric '{name}' is missing")
        try:
            ratchet = float(entry["ratchet_ms"])
            slo = float(entry["slo_ms"])
        except (KeyError, TypeError, ValueError):
            raise SystemExit(f"{baseline}: metric '{name}' needs numeric ratchet_ms and slo_ms")
        if ratchet <= 0 or slo <= 0:
            raise SystemExit(f"{baseline}: metric '{name}' ceilings must be > 0")
        if ratchet >= slo:
            raise SystemExit(
                f"{baseline}: metric '{name}' ratchet_ms {ratchet} must stay below the "
                f"hard SLO {slo} — the ratchet turns one way and never loosens to the SLO"
            )
        ceilings[name] = {"ratchet_ms": ratchet, "slo_ms": slo}
    return ceilings


def _tighten_only(env_name: str, ratchet_ms: float) -> float:
    """Apply an env override as ``min(ratchet, env)`` — it can tighten, never loosen."""
    override = _env_float(env_name, ratchet_ms)
    return min(ratchet_ms, override)


# ---------------------------------------------------------------------------
# Worker process — one simulated production writer (app / MCP / timer job).
# ---------------------------------------------------------------------------

def _worker_main(args: argparse.Namespace) -> int:
    rounds = _env_int("XPROC_LOAD_GATE_ROUNDS", 20)
    threads = _env_int("XPROC_LOAD_GATE_THREADS", 3)
    procs = _env_int("XPROC_LOAD_GATE_PROCS", 3, minimum=2)
    call_timeout_s = args.call_max_ms / 1_000.0 + 2.0

    sys.path.insert(0, str(REPO_ROOT))
    import agent  # noqa: E402
    import store  # noqa: E402

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
            actor=f"xproc-load-gate/worker-{args.index}",
            text=f"cross-process load round {round_number}",
            project=PROJECT,
            hydrate_task=False,
        )
        if not row:
            raise AssertionError(f"write target disappeared: {task_id}")
        return row

    def _board_snapshot() -> str:
        text = agent.board_summary_text(project=PROJECT)
        if not text or "LOAD0-1" not in text:
            raise AssertionError("board snapshot did not contain the seeded tasks")
        return text

    def _timed(kind: str, operation: Callable[[], Any]) -> dict[str, Any]:
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

    # Each worker owns write lane LOAD{index} and searches the other lanes,
    # so every database write races the OTHER processes, not its own rows.
    write_targets = [f"LOAD{args.index}-{i}" for i in range(1, WRITES_PER_ROUND + 1)]
    search_lanes = [f"LOAD{(args.index + offset) % procs}" for offset in range(1, SEARCHES_PER_ROUND + 1)]

    def _run_round(round_number: int) -> list[dict[str, Any]]:
        operations: list[tuple[str, Callable[[], Any]]] = []
        for lane in search_lanes:
            operations.append(("search_tasks", lambda lane=lane: _search(lane)))
        for task_id in write_targets:
            operations.append(("write", lambda task_id=task_id: _write(task_id, round_number)))
        operations.append(("board_summary", _board_snapshot))
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
            futures = [executor.submit(_timed, kind, op) for kind, op in operations]
            return [future.result(timeout=call_timeout_s) for future in futures]

    # Rendezvous: signal readiness (imports + schema warm), then wait for the
    # parent's go signal so all processes hit the database at the same time.
    Path(args.ready).touch()
    deadline = time.monotonic() + READY_TIMEOUT_S
    go = Path(args.go)
    while not go.exists():
        if time.monotonic() > deadline:
            raise SystemExit(f"worker {args.index}: go signal never arrived")
        time.sleep(0.01)

    # Discard one cold-start burst; the SLO describes steady-state calls.
    _run_round(-1)
    samples: list[dict[str, Any]] = []
    for round_number in range(rounds):
        samples.extend(_run_round(round_number))

    Path(args.report).write_text(json.dumps(samples), encoding="utf-8")
    return 0


# ---------------------------------------------------------------------------
# Parent — seed the hermetic shared database, spawn workers, enforce ratchet.
# ---------------------------------------------------------------------------

def _metric(samples: list[float], rank: float, budget_ms: float) -> dict[str, Any]:
    return {
        "samples": len(samples),
        "p50_ms": round(percentile(samples, 50), 3),
        f"p{int(rank)}_ms": round(percentile(samples, rank), 3),
        "max_ms": round(max(samples), 3),
        "budget_ms": budget_ms,
    }


def _parent_main() -> int:
    procs = _env_int("XPROC_LOAD_GATE_PROCS", 3, minimum=2)
    threads = _env_int("XPROC_LOAD_GATE_THREADS", 3)
    rounds = _env_int("XPROC_LOAD_GATE_ROUNDS", 20)
    ratchet = load_ratchet()
    search_p99_ms = _tighten_only("XPROC_LOAD_GATE_SEARCH_P99_MS", ratchet["search_tasks"]["ratchet_ms"])
    board_p95_ms = _tighten_only("XPROC_LOAD_GATE_BOARD_P95_MS", ratchet["board_summary"]["ratchet_ms"])
    write_p99_ms = _tighten_only("XPROC_LOAD_GATE_WRITE_P99_MS", ratchet["writes"]["ratchet_ms"])
    call_max_ms = _tighten_only("XPROC_LOAD_GATE_CALL_MAX_MS", ratchet["all_calls"]["ratchet_ms"])

    tmp = tempfile.mkdtemp(prefix="xproc-load-gate-")
    env = dict(os.environ)
    env["PM_DB_PATH"] = os.path.join(tmp, "maxwell.db")
    env["PM_HELM_DB_PATH"] = os.path.join(tmp, "helm.db")
    env["PM_SWITCHBOARD_DB_PATH"] = os.path.join(tmp, "switchboard.db")
    env["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(tmp, "registry.db")
    env["PM_DYNAMIC_PROJECTS_DIR"] = tmp
    env["PM_AUTH_MODE"] = "dev-open"
    # The single-writer queue stays at its production default (enabled): each
    # worker serializes its OWN writes, and this gate measures the WAL-level
    # contention BETWEEN the per-process queues — the deployed topology.

    workers: list[subprocess.Popen] = []
    worker_stderr: list[Any] = []
    all_results: list[dict[str, Any]] = []
    worker_failures: list[str] = []
    try:
        # Seed in a subprocess so the parent never holds a connection to the
        # database the workers contend on.
        seed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--seed", "--procs", str(procs)],
            env=env, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
        )
        if seed.returncode != 0:
            raise SystemExit(f"seed process failed:\n{seed.stdout}{seed.stderr}")

        go_path = os.path.join(tmp, "go")
        report_paths = [os.path.join(tmp, f"worker-{i}.report.json") for i in range(procs)]
        ready_paths = [os.path.join(tmp, f"worker-{i}.ready") for i in range(procs)]
        for index in range(procs):
            handle = open(os.path.join(tmp, f"worker-{index}.stderr"), "w+")
            worker_stderr.append(handle)
            workers.append(subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()),
                 "--worker", "--index", str(index),
                 "--report", report_paths[index],
                 "--ready", ready_paths[index],
                 "--go", go_path,
                 "--call-max-ms", str(call_max_ms)],
                env=env, cwd=str(REPO_ROOT),
                stdout=subprocess.DEVNULL, stderr=handle,
            ))

        deadline = time.monotonic() + READY_TIMEOUT_S
        while not all(os.path.exists(p) for p in ready_paths):
            if time.monotonic() > deadline:
                raise SystemExit("workers never reported ready")
            if any(w.poll() not in (None, 0) for w in workers):
                raise SystemExit("a worker died before the go signal")
            time.sleep(0.02)
        Path(go_path).touch()

        join_timeout = READY_TIMEOUT_S + (rounds + 1) * (call_max_ms / 1_000.0 + 2.0)
        for index, worker in enumerate(workers):
            try:
                code = worker.wait(timeout=join_timeout)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker_failures.append(f"worker {index} timed out after {join_timeout:.0f}s")
                continue
            if code != 0:
                worker_stderr[index].seek(0)
                tail = worker_stderr[index].read()[-500:]
                worker_failures.append(f"worker {index} exited {code}: {tail.strip()}")
        for index, path in enumerate(report_paths):
            try:
                all_results.extend(json.loads(Path(path).read_text(encoding="utf-8")))
            except (OSError, ValueError):
                if not any(f"worker {index}" in failure for failure in worker_failures):
                    worker_failures.append(f"worker {index} produced no report")
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
        for handle in worker_stderr:
            handle.close()
        shutil.rmtree(tmp, ignore_errors=True)

    if not all_results and worker_failures:
        raise SystemExit("cross-process load gate produced no samples: " + "; ".join(worker_failures))

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
        "search_tasks": _metric(search_ms, 99, search_p99_ms),
        "writes": _metric(write_ms, 99, write_p99_ms),
        "board_summary": _metric(board_ms, 95, board_p95_ms),
        "all_calls": {"samples": len(every_ms), "max_ms": round(max(every_ms), 3),
                      "budget_ms": call_max_ms},
    }
    violations: list[str] = list(worker_failures)
    if errors:
        violations.append(f"{len(errors)} operation(s) failed")
    if locked_errors:
        violations.append(f"{len(locked_errors)} client-visible database lock error(s)")
    if metrics["search_tasks"]["p99_ms"] >= search_p99_ms:
        violations.append(
            f"search_tasks p99 {metrics['search_tasks']['p99_ms']}ms >= {search_p99_ms}ms"
        )
    if metrics["writes"]["p99_ms"] >= write_p99_ms:
        violations.append(f"writes p99 {metrics['writes']['p99_ms']}ms >= {write_p99_ms}ms")
    if metrics["board_summary"]["p95_ms"] >= board_p95_ms:
        violations.append(
            f"board_summary p95 {metrics['board_summary']['p95_ms']}ms >= {board_p95_ms}ms"
        )
    if metrics["all_calls"]["max_ms"] >= call_max_ms:
        violations.append(f"call max {metrics['all_calls']['max_ms']}ms >= {call_max_ms}ms")

    report = {
        "schema": "switchboard.cross_process_load_gate.v1",
        "ok": not violations,
        "scenario": {
            "processes": procs,
            "threads_per_process": threads,
            "rounds_per_process": rounds,
            "searches_per_round": SEARCHES_PER_ROUND,
            "writes_per_round": WRITES_PER_ROUND,
            "board_snapshots_per_round": 1,
            "tasks_per_filtered_lane": TASKS_PER_LANE,
            "storage": "hermetic sqlite WAL shared by separate OS processes",
            "scope": ("server-side; cross-process WAL writer contention; "
                      "excludes network, TLS, and client bridge time"),
        },
        "metrics": metrics,
        "slo_ratchet": {
            "baseline": str(DEFAULT_BASELINE.relative_to(REPO_ROOT)),
            "enforced_ms": {
                "writes_p99": write_p99_ms,
                "search_tasks_p99": search_p99_ms,
                "board_summary_p95": board_p95_ms,
                "all_calls_max": call_max_ms,
            },
            "hard_slo_ms": {
                "writes_p99": ratchet["writes"]["slo_ms"],
                "search_tasks_p99": ratchet["search_tasks"]["slo_ms"],
                "board_summary_p95": ratchet["board_summary"]["slo_ms"],
                "all_calls_max": ratchet["all_calls"]["slo_ms"],
            },
        },
        "error_count": len(errors),
        "database_lock_error_count": len(locked_errors),
        "errors": errors[:20],
        "violations": violations,
    }
    report_text = json.dumps(report, indent=2, sort_keys=True)
    print(report_text)

    output = (os.environ.get("CROSS_PROCESS_LOAD_REPORT") or "").strip()
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report_text + "\n", encoding="utf-8")
        print(f"cross-process load report: {path}")

    if violations:
        print("cross-process load SLO gate: FAIL", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1
    print("cross-process load SLO gate: PASS")
    return 0


def _seed_main(procs: int) -> int:
    sys.path.insert(0, str(REPO_ROOT))
    import store  # noqa: E402

    store.init_db(PROJECT)
    for lane_index in range(procs):
        for task_index in range(TASKS_PER_LANE):
            store.create_task(
                {
                    "workstream_id": f"LOAD{lane_index}",
                    "title": f"Load task {lane_index}-{task_index}",
                    "description": "Hermetic cross-process load gate fixture",
                },
                actor="xproc-load-gate/setup",
                project=PROJECT,
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--seed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--procs", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--index", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--report", default="", help=argparse.SUPPRESS)
    parser.add_argument("--ready", default="", help=argparse.SUPPRESS)
    parser.add_argument("--go", default="", help=argparse.SUPPRESS)
    parser.add_argument("--call-max-ms", type=float, default=2500.0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.seed:
        return _seed_main(args.procs)
    if args.worker:
        return _worker_main(args)
    return _parent_main()


if __name__ == "__main__":
    raise SystemExit(main())
