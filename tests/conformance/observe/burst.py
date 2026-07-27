"""Concurrent completion-conformance burst runner.

The serial conformance matrix proves decision correctness.  This module proves
that many independently correct decisions can be exercised concurrently
without double admission, raced coordinator ownership, leaked runner rows, or
a wedged host.

All product-facing operations are injected.  Observe mode is the default and
must report zero admitted/completed boots.  Full-loop mode is deliberately
awkward to enable: callers must provide both a positive capacity budget and an
operator-watching acknowledgement.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional


BURST_SCHEMA = "switchboard.completion_conformance.burst_scoreboard.v1"
OBSERVATION_SCHEMA = "switchboard.completion_conformance.burst_observation.v1"
MODES = frozenset({"observe", "full"})

BurstWorker = Callable[[dict[str, Any]], Mapping[str, Any]]
CanaryProbe = Callable[[], bool]


@dataclass(frozen=True)
class BurstCase:
    """One independently tagged task/PR in a burst."""

    case_id: str
    scenario: Mapping[str, Any]


def _normalise_observation(
    case: BurstCase, raw: Mapping[str, Any], duration_s: float,
) -> dict[str, Any]:
    row = dict(raw)
    row.setdefault("schema", OBSERVATION_SCHEMA)
    row.setdefault("case_id", case.case_id)
    row.setdefault("task_id", case.case_id)
    row.setdefault("scenario_id", case.scenario.get("id"))
    row.setdefault("boots_requested", 0)
    row.setdefault("boots_admitted", 0)
    row.setdefault("boots_completed", 0)
    row.setdefault("runner_ids", [])
    row.setdefault("coordinator_tick_ids", [])
    row.setdefault("live_runner_ids", [])
    row.setdefault("failed_dispatch", False)
    row.setdefault("error", "")
    row["duration_s"] = round(float(duration_s), 6)
    return row


def _run_case(
    case: BurstCase,
    *,
    start: threading.Barrier,
    worker: BurstWorker,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        start.wait()
        raw = worker({
            "case_id": case.case_id,
            "scenario": dict(case.scenario),
        })
        return _normalise_observation(
            case, raw, time.monotonic() - started,
        )
    except Exception as exc:  # Preserve a failed dispatch as a red row.
        return _normalise_observation(
            case,
            {"error": f"{type(exc).__name__}: {exc}", "failed_dispatch": True},
            time.monotonic() - started,
        )


def build_burst_scoreboard(
    observations: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
    mode: str,
    requested_concurrency: int,
    duration_s: float,
    post_burst_canary_booted: bool,
    capacity_budget: Optional[int] = None,
) -> dict[str, Any]:
    """Aggregate observations without hiding any failed invariant."""
    rows = [dict(row) for row in observations]
    duplicate_boot_tasks = sorted({
        str(row["task_id"]) for row in rows
        if int(row.get("boots_admitted") or 0) > 1
        or len(set(row.get("runner_ids") or [])) > 1
    })
    raced_tick_tasks = sorted({
        str(row["task_id"]) for row in rows
        if len(set(row.get("coordinator_tick_ids") or [])) > 1
    })
    orphaned_runner_ids = sorted({
        str(runner_id)
        for row in rows
        for runner_id in (row.get("live_runner_ids") or [])
    })
    failed_dispatch_live_runner_ids = sorted({
        str(runner_id)
        for row in rows if row.get("failed_dispatch")
        for runner_id in (row.get("live_runner_ids") or [])
    })
    errors = [
        {"case_id": row.get("case_id"), "error": row.get("error")}
        for row in rows if row.get("error")
    ]
    scoreboard = {
        "schema": BURST_SCHEMA,
        "run_id": run_id,
        "mode": mode,
        "requested_concurrency": requested_concurrency,
        "case_count": len(rows),
        "boots_requested": sum(int(r.get("boots_requested") or 0) for r in rows),
        "boots_admitted": sum(int(r.get("boots_admitted") or 0) for r in rows),
        "boots_completed": sum(int(r.get("boots_completed") or 0) for r in rows),
        "duplicate_boot_count": len(duplicate_boot_tasks),
        "duplicate_boot_tasks": duplicate_boot_tasks,
        "raced_coordinator_tick_count": len(raced_tick_tasks),
        "raced_coordinator_tick_tasks": raced_tick_tasks,
        "orphaned_runner_count": len(orphaned_runner_ids),
        "orphaned_runner_ids": orphaned_runner_ids,
        "failed_dispatch_live_runner_count": len(
            failed_dispatch_live_runner_ids
        ),
        "failed_dispatch_live_runner_ids": failed_dispatch_live_runner_ids,
        "duration_s": round(float(duration_s), 6),
        "post_burst_canary_booted": bool(post_burst_canary_booted),
        "capacity_budget": capacity_budget,
        "errors": errors,
        "observations": rows,
    }
    scoreboard["passed"] = not burst_findings(scoreboard)
    return scoreboard


def burst_findings(scoreboard: Mapping[str, Any]) -> list[str]:
    """Return stable, operator-readable invariant failures."""
    findings = []
    if scoreboard.get("mode") == "observe" and scoreboard.get("boots_admitted"):
        findings.append("observe_mode_admitted_capacity")
    budget = scoreboard.get("capacity_budget")
    if budget is not None and int(scoreboard.get("boots_admitted") or 0) > int(budget):
        findings.append("capacity_budget_exceeded")
    if scoreboard.get("duplicate_boot_count"):
        findings.append("duplicate_boot")
    if scoreboard.get("raced_coordinator_tick_count"):
        findings.append("raced_coordinator_tick")
    if scoreboard.get("orphaned_runner_count"):
        findings.append("orphaned_runner")
    if scoreboard.get("failed_dispatch_live_runner_count"):
        findings.append("failed_dispatch_left_live_runner")
    if scoreboard.get("errors"):
        findings.append("worker_error")
    if not scoreboard.get("post_burst_canary_booted"):
        findings.append("post_burst_canary_failed")
    return findings


def assert_burst_invariants(scoreboard: Mapping[str, Any]) -> None:
    findings = burst_findings(scoreboard)
    if findings:
        raise AssertionError(
            "completion-conformance burst failed: " + ", ".join(findings)
        )


def run_burst(
    cases: Iterable[BurstCase],
    *,
    run_id: str,
    worker: BurstWorker,
    canary_probe: CanaryProbe,
    mode: str = "observe",
    concurrency: int = 40,
    capacity_budget: Optional[int] = None,
    operator_watching: bool = False,
    assert_invariants: bool = True,
) -> dict[str, Any]:
    """Start ``cases`` together, drain them, then probe a fresh canary boot."""
    cases = list(cases)
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}, got {mode!r}")
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    if mode == "full" and (
        capacity_budget is None or capacity_budget < 1 or not operator_watching
    ):
        raise RuntimeError(
            "full burst requires a positive capacity_budget and "
            "operator_watching=True"
        )
    worker_count = min(concurrency, len(cases)) if cases else 1
    barrier = threading.Barrier(worker_count)
    started = time.monotonic()
    rows = []
    with ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="conformance-burst",
    ) as pool:
        futures = [
            pool.submit(_run_case, case, start=barrier, worker=worker)
            for case in cases
        ]
        for future in as_completed(futures):
            rows.append(future.result())
    rows.sort(key=lambda row: str(row["case_id"]))
    canary_booted = bool(canary_probe())
    scoreboard = build_burst_scoreboard(
        rows,
        run_id=run_id,
        mode=mode,
        requested_concurrency=concurrency,
        duration_s=time.monotonic() - started,
        post_burst_canary_booted=canary_booted,
        capacity_budget=capacity_budget,
    )
    if assert_invariants:
        assert_burst_invariants(scoreboard)
    return scoreboard


__all__ = [
    "BURST_SCHEMA",
    "OBSERVATION_SCHEMA",
    "BurstCase",
    "assert_burst_invariants",
    "build_burst_scoreboard",
    "burst_findings",
    "run_burst",
]
