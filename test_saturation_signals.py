#!/usr/bin/env python3
"""PERF-7 — hermetic saturation signals, PSI parsing, and SLO evaluation tests."""
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="saturation-signals-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import load_shed  # noqa: E402
import psi_pressure  # noqa: E402
import request_observability  # noqa: E402
import saturation_signals  # noqa: E402
import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


SAMPLE_PSI = """some avg10=12.34 avg60=1.00 avg300=0.10 total=12345
full avg10=0.50 avg60=0.10 avg300=0.00 total=99
"""

parsed = psi_pressure.parse_pressure_text(SAMPLE_PSI)
ok(parsed["some"]["avg10"] == 12.34, "PSI parser reads some avg10")
ok(parsed["full"]["total_us"] == 99, "PSI parser reads full total")

unavailable = psi_pressure.read_psi("cpu", proc_root="/definitely-missing-proc-root")
ok(unavailable["available"] is False, "missing PSI mount returns available=false")

obs = request_observability.RequestObservability(sample_limit=8)
obs.record("web", 12.0, 200)
obs.record("web", 400.0, 200)
obs.record("webhook_ingest", 10.0, 200)
obs.record("webhook_ingest", 60.0, 500, dropped_webhook=True)
snap = obs.snapshot()
ok(snap["routes"]["web"]["p99_ms"] == 400.0, "request observability computes web p99")
ok(snap["routes"]["webhook_ingest"]["calls"] == 2, "webhook ingest calls counted")
ok(snap["dropped_webhook_deliveries"] == 1, "dropped webhook counter increments")

shed = load_shed.should_shed(
    psi={
        "available": True,
        "resources": {
            "cpu": {"stall": {"some": {"avg10": 99.0}, "full": {"avg10": 0.0}}},
            "memory": {"stall": {"some": {"avg10": 0.0}}},
            "io": {"stall": {"some": {"avg10": 0.0}}},
        },
    },
    sqlite_lock_waits=0,
    webhook_inbox_pending=0,
    thresholds={"psi_some_avg10": 25.0, "retry_after_s": 3},
)
ok(shed["should_shed"] is True, "load shed triggers on high cpu PSI")
ok(shed["retry_after_s"] == 3, "load shed carries retry_after_s for Retry-After header")

store.init_db("switchboard")
payload = saturation_signals.compute_saturation_signals(
    "switchboard",
    mcp_obs_provider=lambda: {"sqlite_lock_waits": 2},
    request_obs_provider=obs.snapshot,
)
ok(payload["schema"] == "switchboard.saturation_signals.v1", "saturation snapshot schema")
ok(payload["mcp_observability"]["sqlite_lock_waits"] == 2, "saturation includes lock waits")
ok(payload["slos"]["checks"]["web_p99_ms"]["status"] == "fail", "web p99 SLO fails on 400ms sample")
ok(payload["slos"]["checks"]["webhook_ingest_p99_ms"]["status"] == "fail",
   "webhook ingest p99 SLO fails on 60ms sample under 50ms budget")
ok(any(a["kind"] == "slo" for a in payload["alerts"]), "SLO violations surface as alerts")
ok(payload["load_shed"]["should_shed"] is False, "benign fixture does not recommend shed without PSI pressure")

# --- Pressure badge: alert must key off the trailing WINDOW, not the lifetime counter ---
# Regression: a huge lifetime count with an empty window must NOT alert — the badge returns
# to green once contention subsides, instead of sticking yellow forever after the first lock.
_alerts_stuck = saturation_signals.build_alerts(
    psi={"available": None},
    mcp_obs={"sqlite_lock_waits": 9999, "sqlite_lock_waits_window": 0, "sqlite_lock_wait_window_s": 60},
    inbox_depth={},
    slo={"violations": []},
    load_shed_state={},
)
ok(not any(a["kind"] == "sqlite_lock_wait" for a in _alerts_stuck),
   "lifetime lock-waits with an empty window no longer pin the badge (recovers to green)")

# Real, current contention above threshold still raises a warning with the windowed value.
_alerts_hot = saturation_signals.build_alerts(
    psi={"available": None},
    mcp_obs={"sqlite_lock_waits": 9999, "sqlite_lock_waits_window": 999, "sqlite_lock_wait_window_s": 60},
    inbox_depth={},
    slo={"violations": []},
    load_shed_state={},
)
_lw = [a for a in _alerts_hot if a["kind"] == "sqlite_lock_wait"]
ok(len(_lw) == 1 and _lw[0]["value"] == 999 and _lw[0].get("lifetime") == 9999,
   "elevated windowed lock-waits still warn, reporting the windowed value + lifetime context")

# --- Remaining lifetime-counter root causes: dropped deliveries, concurrency shed, PSI red, pending ---
# Dropped webhook deliveries: a lifetime total must NOT fail the SLO; only a recent (windowed) drop.
_slo_stuck = saturation_signals.evaluate_slos(
    request_obs={"dropped_webhook_deliveries": 9999, "dropped_webhook_deliveries_window": 0},
    mcp_obs={}, inbox_depth={})
ok(_slo_stuck["checks"]["dropped_webhook_deliveries"]["status"] == "pass",
   "lifetime dropped deliveries with empty window no longer fail the SLO (badge recovers to green)")
_slo_hot = saturation_signals.evaluate_slos(
    request_obs={"dropped_webhook_deliveries": 9999, "dropped_webhook_deliveries_window": 3},
    mcp_obs={}, inbox_depth={})
ok(_slo_hot["checks"]["dropped_webhook_deliveries"]["status"] == "fail",
   "a recent dropped delivery still fails the SLO")

# Concurrency shed: lifetime total must NOT alert; only recent (windowed) rejections do.
_cc_stuck = saturation_signals._concurrency_alerts({"enabled": True, "shed_total": 9999, "shed_window": 0})
ok(not any(a["kind"] == "concurrency_shed" for a in _cc_stuck),
   "lifetime concurrency rejections with empty window no longer pin the badge yellow")
_cc_hot = saturation_signals._concurrency_alerts({"enabled": True, "shed_total": 9999, "shed_window": 4})
_cs = [a for a in _cc_hot if a["kind"] == "concurrency_shed"]
ok(len(_cs) == 1 and _cs[0]["value"] == 4 and _cs[0].get("lifetime") == 9999,
   "recent concurrency rejections alert with the windowed value + lifetime context")

# PSI red keys off the SUSTAINED avg60, not a 10s spike.
_psi_spike = saturation_signals.build_alerts(
    psi={"available": True, "resources": {"cpu": {"stall": {"full": {"avg10": 99.0, "avg60": 0.0}}}}},
    mcp_obs={}, inbox_depth={}, slo={"violations": []}, load_shed_state={})
ok(not any(a["kind"] == "psi_full" for a in _psi_spike),
   "a 10s PSI-full spike with a calm avg60 no longer paints the badge red")
_psi_sustained = saturation_signals.build_alerts(
    psi={"available": True, "resources": {"cpu": {"stall": {"full": {"avg10": 99.0, "avg60": 40.0}}}}},
    mcp_obs={}, inbox_depth={}, slo={"violations": []}, load_shed_state={})
ok(any(a["kind"] == "psi_full" and a["severity"] == "critical" for a in _psi_sustained),
   "sustained PSI-full (avg60 over threshold) still raises red")

# Webhook inbox pending is no longer a twitchy `> 0` alert (the SLO catches real backlog).
_pending = saturation_signals.build_alerts(
    psi={"available": None}, mcp_obs={},
    inbox_depth={"pending": 5, "dead": 0}, slo={"violations": []}, load_shed_state={})
ok(not any(a["kind"] == "webhook_inbox_pending" for a in _pending),
   "a few pending inbox items no longer trip the badge (SLO catches real backlog)")

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  FastAPI endpoint smoke requires optional dependency: {exc.name}")
else:
    client = TestClient(app)
    health = client.get("/health/saturation", params={"project": "switchboard"})
    ok(health.status_code == 200, "/health/saturation returns 200")
    body = health.json()
    ok("alert_count" in body and "sqlite_lock_waits" in body,
       "/health/saturation exposes alert_count and sqlite_lock_waits")

    full = client.get("/api/saturation", params={"project": "switchboard"})
    ok(full.status_code == 200, "/api/saturation returns 200")
    full_body = full.json()
    ok(full_body.get("schema") == "switchboard.saturation_signals.v1",
       "/api/saturation returns full saturation payload")

    rest = client.get("/ixp/v1/saturation_signals", params={"project": "switchboard"})
    ok(rest.status_code == 200, "/ixp/v1/saturation_signals returns 200")

    try:
        import mcp_server  # noqa: E402
    except ModuleNotFoundError as exc:
        print(f"  SKIP  MCP saturation tool smoke requires optional dependency: {exc.name}")
    else:
        mcp_payload = json.loads(mcp_server.get_saturation_signals(project="switchboard"))
        ok(mcp_payload.get("schema") == "switchboard.saturation_signals.v1",
           "MCP get_saturation_signals returns saturation payload")

shutil.rmtree(_TMP, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
