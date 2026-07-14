#!/usr/bin/env python3
"""Focused proof for the ARCH-MS-69 operator REST router extraction."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT


TMP = tempfile.mkdtemp(prefix="arch-ms69-operator-rest-routers-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def expanded_routes(routes):
    """Flatten FastAPI 0.139's lazy _IncludedRouter entries for inspection."""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from expanded_routes(included.routes)
        else:
            yield route


def endpoints_for_path_prefix(prefix: str):
    return [
        route for route in expanded_routes(app.routes)
        if getattr(route, "path", "").startswith(prefix)
    ]


def endpoints_for_exact_paths(paths: set[str]):
    return [
        route for route in expanded_routes(app.routes)
        if getattr(route, "path", "") in paths
    ]


INBOX_PREFIX = "/api/inbox"
INTAKE_PREFIX = "/api/intake"
DIGEST_PATHS = {"/api/digest", "/api/digests"}
NOTIFY_PATHS = {"/api/notify/status", "/api/notify/test"}
EXPORT_PATHS = {"/api/export.xlsx", "/api/export.xml"}
GITHUB_WEBHOOK_PATHS = {
    "/api/github/webhook",
    "/api/github/webhook/drain",
    "/api/github/webhook/inbox",
}


try:
    inbox_endpoints = endpoints_for_path_prefix(INBOX_PREFIX)
    ok(inbox_endpoints and all(
        route.endpoint.__module__ == "switchboard.api.routers.intake_inbox"
        for route in inbox_endpoints
    ), "every /api/inbox endpoint is owned by switchboard.api.routers.intake_inbox")

    intake_endpoints = endpoints_for_path_prefix(INTAKE_PREFIX)
    ok(intake_endpoints and all(
        route.endpoint.__module__ == "switchboard.api.routers.intake_inbox"
        for route in intake_endpoints
    ), "every /api/intake endpoint is owned by switchboard.api.routers.intake_inbox")

    digest_endpoints = endpoints_for_exact_paths(DIGEST_PATHS)
    ok(len(digest_endpoints) == len(DIGEST_PATHS) and all(
        route.endpoint.__module__ == "switchboard.api.routers.digest_notify"
        for route in digest_endpoints
    ), "digest endpoints are owned by switchboard.api.routers.digest_notify")

    notify_endpoints = endpoints_for_exact_paths(NOTIFY_PATHS)
    ok(len(notify_endpoints) == len(NOTIFY_PATHS) and all(
        route.endpoint.__module__ == "switchboard.api.routers.digest_notify"
        for route in notify_endpoints
    ), "notify endpoints are owned by switchboard.api.routers.digest_notify")

    export_endpoints = endpoints_for_exact_paths(EXPORT_PATHS)
    ok(len(export_endpoints) == len(EXPORT_PATHS) and all(
        route.endpoint.__module__ == "switchboard.api.routers.ops_export"
        for route in export_endpoints
    ), "export endpoints are owned by switchboard.api.routers.ops_export")

    webhook_endpoints = endpoints_for_exact_paths(GITHUB_WEBHOOK_PATHS)
    ok(len(webhook_endpoints) == len(GITHUB_WEBHOOK_PATHS) and all(
        route.endpoint.__module__ == "switchboard.api.routers.github_webhook"
        for route in webhook_endpoints
    ), "github webhook endpoints are owned by switchboard.api.routers.github_webhook")

    app_impl_source = (ROOT / "app_impl.py").read_text(encoding="utf-8")
    duplicate_needles = (
        '@app.get("/api/inbox")',
        '@app.post("/api/inbox/',
        '@app.post("/api/intake")',
        '@app.post("/api/intake/upload")',
        '@app.post("/api/digest")',
        '@app.get("/api/digests")',
        '@app.get("/api/notify/status")',
        '@app.post("/api/notify/test")',
        '@app.get("/api/export.xlsx")',
        '@app.get("/api/export.xml")',
        '@app.post("/api/github/webhook")',
    )
    ok(all(needle not in app_impl_source for needle in duplicate_needles),
       "app_impl.py contains no duplicate operator REST route decorators")

    ok("_create_intake_inbox_router" in app_impl_source
       and "_create_digest_notify_router" in app_impl_source
       and "_create_ops_export_router" in app_impl_source
       and "_create_github_webhook_router" in app_impl_source
       and "app.include_router(_create_intake_inbox_router" in app_impl_source
       and "app.include_router(_create_digest_notify_router" in app_impl_source
       and "app.include_router(_create_ops_export_router" in app_impl_source
       and "app.include_router(_create_github_webhook_router" in app_impl_source,
       "composition root mounts the extracted operator REST routers")

    client = TestClient(app)
    inbox = client.get("/api/inbox", params={"project": "switchboard"})
    ok(inbox.status_code == 200 and "items" in inbox.json(),
       "extracted /api/inbox returns inbox items")

    digests = client.get("/api/digests")
    ok(digests.status_code == 200 and "digests" in digests.json(),
       "extracted /api/digests lists digests")

    notify = client.get("/api/notify/status")
    ok(notify.status_code == 200,
       "extracted /api/notify/status returns channel status")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
