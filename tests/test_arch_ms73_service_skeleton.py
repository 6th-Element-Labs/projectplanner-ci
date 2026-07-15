#!/usr/bin/env python3
"""ARCH-MS-73: service skeleton — FastAPI unit + health + systemd/Caddy pattern.

Proves the reusable process-cut skeleton is importable in CI, serves a cheap
``/health``, exposes a contracts/OpenAPI boundary, and stays unmounted from the
live monolith / production Caddy / enabled systemd units.
"""
from __future__ import annotations

import importlib
from pathlib import Path

from path_setup import ROOT, entrypoint_source

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# --- Import surface ----------------------------------------------------------
SKELETON_PACKAGES = (
    "switchboard.services",
    "switchboard.services._skeleton",
    "switchboard.services._skeleton.contracts",
    "switchboard.services._skeleton.routers",
)
SKELETON_MODULES = (
    "switchboard.services._skeleton.settings",
    "switchboard.services._skeleton.health",
    "switchboard.services._skeleton.app",
    "switchboard.services._skeleton.contracts.v1",
    "switchboard.services._skeleton.contracts.openapi",
    "switchboard.services._skeleton.routers.example",
)

for name in SKELETON_PACKAGES + SKELETON_MODULES:
    try:
        importlib.import_module(name)
        ok(True, f"import {name}")
    except Exception as exc:  # pragma: no cover - failure path prints
        ok(False, f"import {name}: {exc}")

from fastapi.testclient import TestClient

from switchboard.services._skeleton import create_app
from switchboard.services._skeleton.contracts.openapi import build_openapi_document
from switchboard.services._skeleton.settings import SkeletonSettings

settings = SkeletonSettings(
    service_name="arch-ms73-test",
    host="127.0.0.1",
    port=8120,
)
client = TestClient(create_app(settings))

health = client.get("/health")
ok(health.status_code == 200, f"/health status {health.status_code}")
body = health.json()
ok(body.get("status") == "ok", f"/health status field: {body!r}")
ok(body.get("service") == "arch-ms73-test", f"/health service field: {body!r}")

ping = client.get("/api/example/ping")
ok(ping.status_code == 200, f"/api/example/ping status {ping.status_code}")
ok(ping.json().get("ok") is True, f"/api/example/ping body: {ping.json()!r}")

doc = build_openapi_document(service_name="arch-ms73-test")
ok(doc.get("openapi") == "3.1.0", "contracts OpenAPI version is 3.1.0")
ok("/health" in doc.get("paths", {}), "contracts OpenAPI includes /health")
ok(
    "/api/example/ping" in doc.get("paths", {}),
    "contracts OpenAPI includes /api/example/ping",
)

openapi_resp = client.get("/openapi-skeleton.json")
ok(openapi_resp.status_code == 200, "GET /openapi-skeleton.json")
ok(
    openapi_resp.json().get("info", {}).get("x-switchboard-service") == "arch-ms73-test",
    "openapi-skeleton.json carries service name",
)

# --- Stay dormant: not wired into live traffic -------------------------------
app_impl_src = entrypoint_source("app")
ok(
    "switchboard.services._skeleton" not in app_impl_src
    and "services._skeleton" not in app_impl_src,
    "live app_impl does not import the skeleton",
)

caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
ok(
    "8120" not in caddy and "/skeleton" not in caddy,
    "production Caddyfile does not route skeleton traffic",
)

live_units = list((ROOT / "deploy").glob("*.service"))
live_names = [p.name for p in live_units]
ok(
    "switchboard-skeleton.service" not in live_names,
    "no enabled (non-.example) skeleton systemd unit in deploy/",
)

readme = ROOT / "deploy" / "skeleton" / "README.md"
unit = ROOT / "deploy" / "skeleton" / "switchboard-skeleton.service.example"
caddy_frag = ROOT / "deploy" / "skeleton" / "Caddyfile.fragment.example"
ok(readme.is_file(), "deploy/skeleton/README.md exists")
ok(unit.is_file(), "deploy/skeleton/switchboard-skeleton.service.example exists")
ok(caddy_frag.is_file(), "deploy/skeleton/Caddyfile.fragment.example exists")

unit_text = unit.read_text(encoding="utf-8")
ok(
    "switchboard.services._skeleton.app:app" in unit_text,
    "systemd example points at skeleton uvicorn app",
)
ok(
    "DORMANT" in unit_text or "Do NOT enable" in unit_text,
    "systemd example is marked dormant",
)
ok(
    "reverse_proxy" in caddy_frag.read_text(encoding="utf-8"),
    "Caddy fragment documents reverse_proxy",
)

print()
print(f"{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
