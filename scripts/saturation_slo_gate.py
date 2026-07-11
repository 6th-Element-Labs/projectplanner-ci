#!/usr/bin/env python3
"""PERF-7 — saturation SLO gate under a synthetic HTTP burst.

Exercises the web process request histograms and SLO evaluator the way production
monitors will: a burst of cheap web reads plus simulated webhook ingests, then
asserts webhook ingest p99 < 50 ms, web p99 < 300 ms, and zero dropped webhook
deliveries.

Environment overrides:
  SAT_GATE_WEB_ROUNDS=30
  SAT_GATE_WEBHOOK_ROUNDS=30
  SAT_GATE_WEB_P99_MS=300
  SAT_GATE_WEBHOOK_P99_MS=50
  SATURATION_SLO_REPORT=.artifacts/saturation-slo-report.json
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="saturation-slo-gate-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        raise SystemExit(f"{name} must be an integer")


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        raise SystemExit(f"{name} must be a number")
    if value <= 0:
        raise SystemExit(f"{name} must be > 0")
    return value


WEB_ROUNDS = _env_int("SAT_GATE_WEB_ROUNDS", 30)
WEBHOOK_ROUNDS = _env_int("SAT_GATE_WEBHOOK_ROUNDS", 30)
WEB_P99_MS = _env_float("SAT_GATE_WEB_P99_MS", 300.0)
WEBHOOK_P99_MS = _env_float("SAT_GATE_WEBHOOK_P99_MS", 50.0)
PROJECT = "switchboard"

import store  # noqa: E402
import request_observability  # noqa: E402
import saturation_signals  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app, _req_obs  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"saturation SLO gate requires optional dependency: {exc.name}")
    raise SystemExit(1) from exc


def main() -> int:
    violations: list[str] = []
    try:
        store.init_db(PROJECT)
        client = TestClient(app)

        # Warm imports / schema once.
        client.get("/health")
        _req_obs.snapshot()

        for i in range(WEB_ROUNDS):
            started = time.perf_counter()
            res = client.get("/health/saturation", params={"project": PROJECT})
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            _req_obs.record("web", elapsed_ms, res.status_code)
            if res.status_code != 200:
                violations.append(f"web read {i} returned {res.status_code}")

        for i in range(WEBHOOK_ROUNDS):
            started = time.perf_counter()
            res = client.post(
                "/api/github/webhook",
                params={"project": PROJECT},
                content=b"{}",
                headers={
                    "X-GitHub-Event": "ping",
                    "Content-Type": "application/json",
                },
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            dropped = res.status_code >= 500
            _req_obs.record("webhook_ingest", elapsed_ms, res.status_code,
                            dropped_webhook=dropped)
            if dropped:
                violations.append(f"webhook ingest {i} returned {res.status_code}")

        snap = saturation_signals.compute_saturation_signals(
            PROJECT,
            mcp_obs_provider=lambda: {"sqlite_lock_waits": store.sqlite_lock_wait_count()},
            request_obs_provider=_req_obs.snapshot,
        )
        slo = snap.get("slos") or {}
        checks = slo.get("checks") or {}
        for name in ("web_p99_ms", "webhook_ingest_p99_ms", "dropped_webhook_deliveries"):
            spec = checks.get(name) or {}
            if spec.get("status") == "fail":
                violations.append(f"{name} failed: {spec}")
            if spec.get("status") == "no_samples":
                violations.append(f"{name} has no samples")

        report = {
            "schema": "switchboard.saturation_slo_gate.v1",
            "ok": not violations,
            "scenario": {
                "web_rounds": WEB_ROUNDS,
                "webhook_rounds": WEBHOOK_ROUNDS,
                "web_p99_budget_ms": WEB_P99_MS,
                "webhook_p99_budget_ms": WEBHOOK_P99_MS,
            },
            "saturation": snap,
            "violations": violations,
        }
        text = json.dumps(report, indent=2, sort_keys=True)
        print(text)

        output = (os.environ.get("SATURATION_SLO_REPORT") or "").strip()
        if output:
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
            print(f"saturation SLO report: {path}")

        if violations:
            print("saturation SLO gate: FAIL", file=sys.stderr)
            for violation in violations:
                print(f"  - {violation}", file=sys.stderr)
            return 1
        print("saturation SLO gate: PASS")
        return 0
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
