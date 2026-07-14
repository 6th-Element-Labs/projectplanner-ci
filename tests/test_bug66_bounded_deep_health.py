#!/usr/bin/env python3
"""BUG-66: deep readiness is isolated, bounded, and single-flight under a stall."""

from __future__ import annotations

import os
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from path_setup import ROOT  # noqa: F401 - also adds repo root and src/ to sys.path

from switchboard.api.routers import health as health_router


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {message}")
    else:
        failed += 1
        print(f"  FAIL  {message}")


original_ids = health_router.store.project_ids
original_probe = health_router.store.probe_project_db
original_timeout = os.environ.get("PM_HEALTH_DEEP_TIMEOUT_SECONDS")
release_probe = threading.Event()
probe_started = threading.Event()
probe_calls = 0


def stalled_once(_project: str) -> None:
    global probe_calls
    probe_calls += 1
    if probe_calls == 1:
        probe_started.set()
        release_probe.wait(timeout=5)
    return None


try:
    os.environ["PM_HEALTH_DEEP_TIMEOUT_SECONDS"] = "invalid"
    ok(health_router._readiness_timeout_seconds() == 2.0,
       "invalid timeout configuration falls back to the safe default")
    os.environ["PM_HEALTH_DEEP_TIMEOUT_SECONDS"] = "99"
    ok(health_router._readiness_timeout_seconds() == 4.0,
       "operator configuration cannot exceed the edge-safe maximum")
    os.environ["PM_HEALTH_DEEP_TIMEOUT_SECONDS"] = "0.1"
    health_router.store.project_ids = lambda: ["secret-project"]
    health_router.store.probe_project_db = stalled_once

    app = FastAPI()
    app.include_router(health_router.create_router(
        resolve_project=lambda project: project,
        resolve_principal=lambda *_args, **_kwargs: {},
        saturation_snapshot=lambda _project: {},
        project_init_failures=lambda: {},
    ))

    with TestClient(app) as client:
        started = time.perf_counter()
        first = client.get("/health/deep")
        first_elapsed = time.perf_counter() - started
        ok(probe_started.is_set(), "the isolated readiness worker started the live probe")
        ok(first.status_code == 503
           and first.json().get("reason") == "probe_timeout"
           and first_elapsed < 0.5,
           "a stalled probe fails closed before the edge timeout")
        ok("secret-project" not in first.text and first.headers.get("retry-after") == "1",
           "the bounded failure is actionable without leaking project identity")

        liveness_started = time.perf_counter()
        live = client.get("/health")
        ok(live.status_code == 200 and time.perf_counter() - liveness_started < 0.2,
           "cheap liveness stays responsive while deep readiness is stalled")

        second = client.get("/health/deep")
        ok(second.status_code == 503 and probe_calls == 1,
           "repeated polling reuses one in-flight probe instead of spawning threads")

        release_probe.set()
        deadline = time.monotonic() + 2
        recovered = None
        while time.monotonic() < deadline:
            recovered = client.get("/health/deep")
            if recovered.status_code == 200:
                break
            time.sleep(0.02)
        ok(recovered is not None and recovered.status_code == 200
           and recovered.json().get("ready") is True,
           "readiness recovers without a process restart after the probe clears")
        ok(probe_calls <= 2,
           "recovery does not fan out extra probes around task completion")
finally:
    release_probe.set()
    health_router.store.project_ids = original_ids
    health_router.store.probe_project_db = original_probe
    if original_timeout is None:
        os.environ.pop("PM_HEALTH_DEEP_TIMEOUT_SECONDS", None)
    else:
        os.environ["PM_HEALTH_DEEP_TIMEOUT_SECONDS"] = original_timeout


print(f"\nBUG-66 bounded deep health: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
