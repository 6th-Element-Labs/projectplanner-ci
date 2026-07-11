"""Saturation signals + SLO evaluation + alerts (PERF-7).

Aggregates PSI pressure, MCP sqlite lock-wait (HARDEN-49), webhook inbox depth
(PERF-1 when present), and HTTP latency histograms into one operator-facing
snapshot with explicit SLO pass/fail and load-shed guidance for PERF-5.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional

import concurrency_limiter
import load_shed
import psi_pressure

DEFAULT_SLOS = {
    "webhook_ingest_p99_ms": float(os.environ.get("PM_SLO_WEBHOOK_INGEST_P99_MS", "50")),
    "web_p99_ms": float(os.environ.get("PM_SLO_WEB_P99_MS", "300")),
    "dropped_webhook_deliveries_max": int(
        os.environ.get("PM_SLO_WEBHOOK_DROPPED_MAX", "0")
    ),
    "sqlite_lock_waits_max": int(os.environ.get("PM_SLO_SQLITE_LOCK_WAITS_MAX", "0")),
    "webhook_inbox_pending_max": int(
        os.environ.get("PM_SLO_WEBHOOK_INBOX_PENDING_MAX", "25")
    ),
    "webhook_inbox_dead_max": int(os.environ.get("PM_SLO_WEBHOOK_INBOX_DEAD_MAX", "0")),
}


def _webhook_inbox_depth(project: str) -> dict:
    """Depth signal — uses PERF-1 module when installed, else probes the table."""
    try:
        import webhook_inbox  # PERF-1 leaf module; optional until merged

        return webhook_inbox.inbox_depth(project)
    except ImportError:
        pass
    try:
        import store

        with store._conn(project) as c:
            row = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_inbox'"
            ).fetchone()
            if not row:
                return {
                    "schema": "switchboard.webhook_inbox_depth.v1",
                    "project": project,
                    "available": False,
                    "pending": 0,
                    "dead": 0,
                    "total": 0,
                    "oldest_pending_age_s": 0.0,
                }
            counts: Dict[str, int] = {}
            for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM webhook_inbox GROUP BY status"
            ).fetchall():
                counts[r["status"]] = int(r["n"])
            oldest = c.execute(
                "SELECT MIN(received_at) AS t FROM webhook_inbox WHERE status='pending'"
            ).fetchone()
        oldest_at = float(oldest["t"]) if oldest and oldest["t"] is not None else None
        return {
            "schema": "switchboard.webhook_inbox_depth.v1",
            "project": project,
            "available": True,
            "pending": counts.get("pending", 0),
            "dead": counts.get("dead", 0),
            "applied": counts.get("applied", 0),
            "ignored": counts.get("ignored", 0),
            "total": sum(counts.values()),
            "by_status": counts,
            "oldest_pending_at": oldest_at,
            "oldest_pending_age_s": (time.time() - oldest_at) if oldest_at else 0.0,
        }
    except Exception as exc:  # noqa: BLE001 — surface the failing signal
        return {
            "schema": "switchboard.webhook_inbox_depth.v1",
            "project": project,
            "available": False,
            "error": type(exc).__name__,
            "message": str(exc),
            "pending": 0,
            "dead": 0,
        }


def _route_metric(request_obs: dict, route_class: str) -> dict:
    return (request_obs.get("routes") or {}).get(route_class) or {}


def evaluate_slos(
    *,
    request_obs: dict,
    mcp_obs: dict,
    inbox_depth: dict,
    slo_budgets: Optional[dict] = None,
) -> dict:
    budgets = dict(DEFAULT_SLOS)
    if slo_budgets:
        budgets.update(slo_budgets)

    webhook = _route_metric(request_obs, "webhook_ingest")
    web = _route_metric(request_obs, "web")
    lock_waits = int((mcp_obs or {}).get("sqlite_lock_waits") or 0)
    dropped = int((request_obs or {}).get("dropped_webhook_deliveries") or 0)
    pending = int((inbox_depth or {}).get("pending") or 0)
    dead = int((inbox_depth or {}).get("dead") or 0)

    checks = {
        "webhook_ingest_p99_ms": {
            "value_ms": webhook.get("p99_ms"),
            "budget_ms": budgets["webhook_ingest_p99_ms"],
            "samples": webhook.get("retained_samples", 0),
        },
        "web_p99_ms": {
            "value_ms": web.get("p99_ms"),
            "budget_ms": budgets["web_p99_ms"],
            "samples": web.get("retained_samples", 0),
        },
        "dropped_webhook_deliveries": {
            "value": dropped,
            "budget_max": budgets["dropped_webhook_deliveries_max"],
        },
        "sqlite_lock_waits": {
            "value": lock_waits,
            "budget_max": budgets["sqlite_lock_waits_max"],
        },
        "webhook_inbox_pending": {
            "value": pending,
            "budget_max": budgets["webhook_inbox_pending_max"],
        },
        "webhook_inbox_dead": {
            "value": dead,
            "budget_max": budgets["webhook_inbox_dead_max"],
        },
    }

    violations: List[str] = []
    for name, spec in checks.items():
        if name.endswith("_p99_ms"):
            value = spec.get("value_ms")
            budget = spec.get("budget_ms")
            if value is None or spec.get("samples", 0) == 0:
                spec["ok"] = None
                spec["status"] = "no_samples"
                continue
            spec["ok"] = value < budget
            spec["status"] = "pass" if spec["ok"] else "fail"
            if not spec["ok"]:
                violations.append(f"{name} {value}ms >= {budget}ms")
        else:
            value = int(spec.get("value") or 0)
            budget = int(spec.get("budget_max") or 0)
            spec["ok"] = value <= budget
            spec["status"] = "pass" if spec["ok"] else "fail"
            if not spec["ok"]:
                violations.append(f"{name} {value} > {budget}")

    return {
        "schema": "switchboard.saturation_slos.v1",
        "ok": not violations,
        "budgets": budgets,
        "checks": checks,
        "violations": violations,
    }


def build_alerts(
    *,
    psi: dict,
    mcp_obs: dict,
    inbox_depth: dict,
    slo: dict,
    load_shed_state: dict,
) -> List[dict]:
    alerts: List[dict] = []
    now = round(time.time(), 3)

    for violation in slo.get("violations") or []:
        alerts.append({
            "at": now,
            "severity": "critical" if "dropped" in violation or "dead" in violation else "warning",
            "kind": "slo",
            "message": violation,
        })

    lock_waits = int((mcp_obs or {}).get("sqlite_lock_waits") or 0)
    if lock_waits > 0:
        alerts.append({
            "at": now,
            "severity": "warning",
            "kind": "sqlite_lock_wait",
            "message": f"sqlite lock waits: {lock_waits}",
            "value": lock_waits,
        })

    pending = int((inbox_depth or {}).get("pending") or 0)
    dead = int((inbox_depth or {}).get("dead") or 0)
    if pending > 0:
        alerts.append({
            "at": now,
            "severity": "warning" if pending < DEFAULT_SLOS["webhook_inbox_pending_max"] else "critical",
            "kind": "webhook_inbox_pending",
            "message": f"webhook inbox pending: {pending}",
            "value": pending,
            "oldest_pending_age_s": (inbox_depth or {}).get("oldest_pending_age_s"),
        })
    if dead > 0:
        alerts.append({
            "at": now,
            "severity": "critical",
            "kind": "webhook_inbox_dead",
            "message": f"webhook inbox dead rows: {dead}",
            "value": dead,
        })

    if psi.get("available"):
        for resource, entry in (psi.get("resources") or {}).items():
            stall = (entry or {}).get("stall") or {}
            some = (stall.get("some") or {}).get("avg10")
            full = (stall.get("full") or {}).get("avg10")
            if some is not None and some >= load_shed.DEFAULT_THRESHOLDS["psi_some_avg10"]:
                alerts.append({
                    "at": now,
                    "severity": "warning",
                    "kind": "psi_some",
                    "message": f"{resource} PSI some avg10 {some}%",
                    "resource": resource,
                    "value": some,
                })
            if full is not None and full >= load_shed.DEFAULT_THRESHOLDS["psi_full_avg10"]:
                alerts.append({
                    "at": now,
                    "severity": "critical",
                    "kind": "psi_full",
                    "message": f"{resource} PSI full avg10 {full}%",
                    "resource": resource,
                    "value": full,
                })
    elif psi.get("available") is False:
        alerts.append({
            "at": now,
            "severity": "info",
            "kind": "psi_unavailable",
            "message": "PSI not available on this host; load-shed uses lock-wait and inbox depth only",
        })

    if load_shed_state.get("should_shed"):
        alerts.append({
            "at": now,
            "severity": "critical",
            "kind": "load_shed",
            "message": "; ".join(load_shed_state.get("reasons") or ["load shed recommended"]),
            "retry_after_s": load_shed_state.get("retry_after_s"),
        })

    return alerts


def _concurrency_alerts(concurrency: dict) -> List[dict]:
    alerts: List[dict] = []
    if not (concurrency or {}).get("enabled"):
        return alerts
    now = round(time.time(), 3)
    if concurrency.get("saturated"):
        alerts.append({
            "at": now,
            "severity": "warning",
            "kind": "concurrency_saturated",
            "message": (
                f"global expensive-op slots full "
                f"({concurrency.get('inflight')}/{concurrency.get('limit')})"
            ),
            "retry_after_s": concurrency.get("retry_after_s"),
        })
    shed_total = int((concurrency or {}).get("shed_total") or 0)
    if shed_total > 0:
        alerts.append({
            "at": now,
            "severity": "warning",
            "kind": "concurrency_shed",
            "message": f"concurrency limit rejections: {shed_total}",
            "value": shed_total,
        })
    return alerts


def compute_saturation_signals(
    project: str = "switchboard",
    *,
    mcp_obs_provider: Optional[Callable[[], dict]] = None,
    request_obs_provider: Optional[Callable[[], dict]] = None,
    slo_budgets: Optional[dict] = None,
) -> dict:
    psi = psi_pressure.read_all_psi()
    mcp_obs = (mcp_obs_provider or (lambda: {}))()
    request_obs = (request_obs_provider or (lambda: {}))()
    store_lock_waits = 0
    store_lock_waits_window = 0
    lock_wait_window_s = 60.0
    try:
        import store as _store

        lock_wait_window_s = float(os.environ.get("PM_SQLITE_LOCK_WAIT_WINDOW_S", "60"))
        store_lock_waits = int(getattr(_store, "sqlite_lock_wait_count", lambda: 0)())
        store_lock_waits_window = int(
            getattr(_store, "sqlite_lock_waits_in_window", lambda _w=60.0: 0)(lock_wait_window_s))
    except Exception:
        store_lock_waits = 0
        store_lock_waits_window = 0
    combined_lock_waits = max(
        store_lock_waits,
        int((mcp_obs or {}).get("sqlite_lock_waits") or 0),
    )
    mcp_obs = dict(mcp_obs or {})
    mcp_obs["sqlite_lock_waits"] = combined_lock_waits
    mcp_obs["sqlite_lock_waits_window"] = store_lock_waits_window
    mcp_obs["sqlite_lock_wait_window_s"] = lock_wait_window_s
    inbox_depth = _webhook_inbox_depth(project)
    slo = evaluate_slos(
        request_obs=request_obs,
        mcp_obs=mcp_obs,
        inbox_depth=inbox_depth,
        slo_budgets=slo_budgets,
    )
    shed = load_shed.should_shed(
        psi=psi,
        sqlite_lock_waits=store_lock_waits_window,
        webhook_inbox_pending=int((inbox_depth or {}).get("pending") or 0),
    )
    concurrency = concurrency_limiter.snapshot()
    alerts = build_alerts(
        psi=psi,
        mcp_obs=mcp_obs,
        inbox_depth=inbox_depth,
        slo=slo,
        load_shed_state=shed,
    )
    alerts.extend(_concurrency_alerts(concurrency))
    severity_rank = {"critical": 3, "warning": 2, "info": 1}
    top_severity = "healthy"
    if alerts:
        top = max(alerts, key=lambda item: severity_rank.get(item.get("severity"), 0))
        top_severity = top.get("severity") or "warning"
        if top_severity == "info" and len(alerts) == 1 and alerts[0].get("kind") == "psi_unavailable":
            top_severity = "healthy"

    return {
        "schema": "switchboard.saturation_signals.v1",
        "as_of": round(time.time(), 3),
        "project": project,
        "status": top_severity,
        "psi": psi,
        "mcp_observability": {
            "sqlite_lock_waits": int((mcp_obs or {}).get("sqlite_lock_waits") or 0),
            "sqlite_lock_waits_window": int((mcp_obs or {}).get("sqlite_lock_waits_window") or 0),
            "sqlite_lock_wait_window_s": (mcp_obs or {}).get("sqlite_lock_wait_window_s"),
            "slow_call_threshold_ms": (mcp_obs or {}).get("slow_call_threshold_ms"),
            "tools": (mcp_obs or {}).get("tools") or {},
        },
        "request_observability": request_obs,
        "webhook_inbox_depth": inbox_depth,
        "slos": slo,
        "load_shed": shed,
        "concurrency_limiter": concurrency,
        "alerts": alerts,
        "alert_count": len(alerts),
    }
