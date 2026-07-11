"""Load-shed decision hook for PERF-5 (PERF-7 wires PSI thresholds here).

Callers (global concurrency limiter, middleware) ask ``should_shed()`` *before*
accepting expensive work.  When shedding is recommended the response is a
structured 429/503 with ``Retry-After`` — graceful backpressure beats accepting
work the box cannot finish.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


DEFAULT_THRESHOLDS = {
    "psi_some_avg10": _float_env("PM_PSI_SOME_AVG10_SHED", 25.0),
    "psi_full_avg10": _float_env("PM_PSI_FULL_AVG10_SHED", 5.0),
    "psi_io_some_avg10": _float_env("PM_PSI_IO_SOME_AVG10_SHED", 30.0),
    "sqlite_lock_waits": _int_env("PM_SQLITE_LOCK_WAIT_SHED", 10),
    "webhook_inbox_pending": _int_env("PM_WEBHOOK_INBOX_PENDING_SHED", 50),
    "retry_after_s": _int_env("PM_LOAD_SHED_RETRY_AFTER_S", 5),
}


def _psi_pressure(psi: dict, resource: str, kind: str = "some") -> Optional[float]:
    resources = (psi or {}).get("resources") or {}
    entry = resources.get(resource) or {}
    stall = entry.get("stall") or {}
    bucket = stall.get(kind) or {}
    value = bucket.get("avg10")
    return float(value) if value is not None else None


def should_shed(
    *,
    psi: Optional[dict] = None,
    sqlite_lock_waits: int = 0,
    webhook_inbox_pending: int = 0,
    thresholds: Optional[Dict[str, Any]] = None,
) -> dict:
    """Return whether the box should shed new work and why."""
    limits = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        limits.update(thresholds)

    reasons: List[str] = []
    signals: Dict[str, Any] = {
        "sqlite_lock_waits": int(sqlite_lock_waits or 0),
        "webhook_inbox_pending": int(webhook_inbox_pending or 0),
    }

    psi_snapshot = psi or {}
    if psi_snapshot.get("available"):
        cpu_some = _psi_pressure(psi_snapshot, "cpu", "some")
        cpu_full = _psi_pressure(psi_snapshot, "cpu", "full")
        mem_some = _psi_pressure(psi_snapshot, "memory", "some")
        io_some = _psi_pressure(psi_snapshot, "io", "some")
        signals["psi"] = {
            "cpu_some_avg10": cpu_some,
            "cpu_full_avg10": cpu_full,
            "memory_some_avg10": mem_some,
            "io_some_avg10": io_some,
        }
        if cpu_some is not None and cpu_some >= limits["psi_some_avg10"]:
            reasons.append(
                f"cpu PSI some avg10 {cpu_some}% >= {limits['psi_some_avg10']}%"
            )
        if cpu_full is not None and cpu_full >= limits["psi_full_avg10"]:
            reasons.append(
                f"cpu PSI full avg10 {cpu_full}% >= {limits['psi_full_avg10']}%"
            )
        if mem_some is not None and mem_some >= limits["psi_some_avg10"]:
            reasons.append(
                f"memory PSI some avg10 {mem_some}% >= {limits['psi_some_avg10']}%"
            )
        if io_some is not None and io_some >= limits["psi_io_some_avg10"]:
            reasons.append(
                f"io PSI some avg10 {io_some}% >= {limits['psi_io_some_avg10']}%"
            )
    else:
        signals["psi"] = {"available": False}

    if signals["sqlite_lock_waits"] >= limits["sqlite_lock_waits"]:
        reasons.append(
            f"sqlite lock waits {signals['sqlite_lock_waits']} "
            f">= {limits['sqlite_lock_waits']}"
        )
    if signals["webhook_inbox_pending"] >= limits["webhook_inbox_pending"]:
        reasons.append(
            f"webhook inbox pending {signals['webhook_inbox_pending']} "
            f">= {limits['webhook_inbox_pending']}"
        )

    return {
        "schema": "switchboard.load_shed.v1",
        "should_shed": bool(reasons),
        "reasons": reasons,
        "retry_after_s": int(limits["retry_after_s"]),
        "thresholds": limits,
        "signals": signals,
    }
