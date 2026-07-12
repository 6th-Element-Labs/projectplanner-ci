"""Prometheus /metrics bridge (pull-based) over the existing saturation snapshot.

There is no Prometheus scrape surface today — only the custom JSON /observability
and /api/saturation endpoints. This module adds one using ``prometheus-client`` for
the exposition format ONLY. The exact-percentile sample stores in
``mcp_observability`` / ``request_observability`` remain the single source of truth
(their nearest-rank p50/p99 feed the SLO gates and are pinned by tests). On each
scrape we project the current snapshot into a transient ``CollectorRegistry`` and
render it — so there is no parallel metric state and no per-request overhead, and a
prometheus ``Histogram`` (bucket-approximate) never replaces the exact percentiles.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest


def _num(value: Any) -> Optional[float]:
    """Coerce a snapshot value to float, or None when it is absent/non-numeric."""
    if value is None or isinstance(value, bool):
        return float(value) if isinstance(value, bool) else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def render(saturation: Dict[str, Any]) -> Tuple[bytes, str]:
    """Project a ``compute_saturation_signals`` snapshot into Prometheus text.

    Returns (body, content_type). Defensive throughout: a missing or malformed
    section simply omits its series rather than raising, so /metrics degrades to a
    partial scrape instead of a 500.
    """
    reg = CollectorRegistry()

    def gauge(name: str, doc: str, labels: tuple = ()) -> Gauge:
        return Gauge(name, doc, list(labels), registry=reg)

    gauge("switchboard_up", "1 when the metrics bridge rendered a snapshot").set(1)

    status = str(saturation.get("status") or "unknown")
    gauge("switchboard_saturation_status", "Top saturation severity (1 for the active label)",
          ("status",)).labels(status).set(1)
    ac = _num(saturation.get("alert_count"))
    if ac is not None:
        gauge("switchboard_alert_count", "Active saturation alerts").set(ac)

    # --- HTTP request latency / counts by route class -----------------------
    routes = (saturation.get("request_observability") or {}).get("routes") or {}
    if routes:
        g_calls = gauge("switchboard_request_calls", "HTTP requests by route class", ("route",))
        g_fail = gauge("switchboard_request_failures", "HTTP 5xx responses by route class", ("route",))
        g_samp = gauge("switchboard_request_retained_samples",
                       "Latency samples retained per route class", ("route",))
        g_lat = gauge("switchboard_request_latency_ms",
                      "Route latency (exact nearest-rank)", ("route", "quantile"))
        g_max = gauge("switchboard_request_latency_max_ms", "Route max latency (ms)", ("route",))
        for route, m in routes.items():
            if not isinstance(m, dict):
                continue
            for metric, value in (("calls", g_calls), ("failures", g_fail),
                                  ("retained_samples", g_samp)):
                n = _num(m.get(metric))
                if n is not None:
                    value.labels(route).set(n)
            for q, key in (("0.5", "p50_ms"), ("0.99", "p99_ms")):
                n = _num(m.get(key))
                if n is not None:
                    g_lat.labels(route, q).set(n)
            mx = _num(m.get("max_ms"))
            if mx is not None:
                g_max.labels(route).set(mx)

    for key, name, doc in (
        ("dropped_webhook_deliveries", "switchboard_dropped_webhook_deliveries",
         "Webhook deliveries dropped (lifetime)"),
        ("dropped_webhook_deliveries_window", "switchboard_dropped_webhook_deliveries_window",
         "Webhook deliveries dropped in the trailing window"),
    ):
        n = _num((saturation.get("request_observability") or {}).get(key))
        if n is not None:
            gauge(name, doc).set(n)

    # --- SQLite lock waits (from the mcp_observability section) --------------
    mcp = saturation.get("mcp_observability") or {}
    for key, name, doc in (
        ("sqlite_lock_waits", "switchboard_sqlite_lock_waits", "SQLite lock waits (lifetime)"),
        ("sqlite_lock_waits_window", "switchboard_sqlite_lock_waits_window",
         "SQLite lock waits in the trailing window"),
    ):
        n = _num(mcp.get(key))
        if n is not None:
            gauge(name, doc).set(n)

    # Per-tool MCP latency is only populated in the MCP process's snapshot; emit it
    # when present so the same bridge works if handed a full MCP observability dict.
    tools = mcp.get("tools") or {}
    if tools:
        g_tcalls = gauge("switchboard_mcp_tool_calls", "MCP tool calls", ("tool",))
        g_tfail = gauge("switchboard_mcp_tool_failures", "MCP tool failures", ("tool",))
        g_tlat = gauge("switchboard_mcp_tool_latency_ms", "MCP tool latency", ("tool", "quantile"))
        for tool, m in tools.items():
            if not isinstance(m, dict):
                continue
            for metric, g in (("calls", g_tcalls), ("failures", g_tfail)):
                n = _num(m.get(metric))
                if n is not None:
                    g.labels(tool).set(n)
            for q, key in (("0.5", "p50_ms"), ("0.99", "p99_ms")):
                n = _num(m.get(key))
                if n is not None:
                    g_tlat.labels(tool, q).set(n)

    # --- Concurrency limiter ------------------------------------------------
    conc = saturation.get("concurrency_limiter") or {}
    for key, name, doc in (
        ("inflight", "switchboard_concurrency_inflight", "Expensive ops in flight"),
        ("limit", "switchboard_concurrency_limit", "Configured expensive-op concurrency limit"),
        ("saturated", "switchboard_concurrency_saturated", "1 when the limiter is saturated"),
        ("shed_total", "switchboard_concurrency_shed_total", "Requests shed (lifetime)"),
        ("shed_window", "switchboard_concurrency_shed_window", "Requests shed in the trailing window"),
    ):
        n = _num(conc.get(key))
        if n is not None:
            gauge(name, doc).set(n)

    # --- Webhook inbox depth + SLO/load-shed rollups ------------------------
    pending = _num((saturation.get("webhook_inbox_depth") or {}).get("pending"))
    if pending is not None:
        gauge("switchboard_webhook_inbox_pending", "Pending durable webhook-inbox rows").set(pending)

    slos_ok = (saturation.get("slos") or {}).get("ok")
    if isinstance(slos_ok, bool):
        gauge("switchboard_slos_ok", "1 when all SLO checks pass").set(1 if slos_ok else 0)
    shed = (saturation.get("load_shed") or {}).get("should_shed")
    if isinstance(shed, bool):
        gauge("switchboard_load_shed", "1 when load shedding is recommended").set(1 if shed else 0)

    return generate_latest(reg), CONTENT_TYPE_LATEST
