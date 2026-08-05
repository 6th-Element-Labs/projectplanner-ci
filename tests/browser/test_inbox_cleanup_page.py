#!/usr/bin/env python3
"""Playwright regression for the responsive, functional Inbox cleanup."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from path_setup import ROOT  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="inbox-cleanup-browser-"))
env = dict(os.environ)
env.update({
    "PM_DB_PATH": str(tmp / "maxwell.db"),
    "PM_HELM_DB_PATH": str(tmp / "helm.db"),
    "PM_SWITCHBOARD_DB_PATH": str(tmp / "switchboard.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": str(tmp / "registry.db"),
    "PM_DYNAMIC_PROJECTS_DIR": str(tmp / "projects"),
    "PM_AUTH_MODE": "dev-open",
    "PYTHONPATH": f"{ROOT}:{ROOT / 'src'}",
})
os.environ.update({key: value for key, value in env.items() if key.startswith("PM_")})
(tmp / "projects").mkdir(parents=True)

import store  # noqa: E402
from switchboard.storage.repositories.attention import (  # noqa: E402
    default_attention_repository,
)

store.init_project_registry()
store.init_db("switchboard")
task = store.create_task(
    {"workstream_id": "UI", "title": "Inbox functional cleanup fixture"},
    actor="inbox-cleanup-test", project="switchboard",
)
default_attention_repository.create_request({
    "task_id": task["task_id"],
    "provider": "provider-neutral",
    "provider_request_id": "inbox-functional-v2",
    "schema_version": "provider.question.v1",
    "prompt": "Choose the safe rollout path for the Inbox cleanup.",
    "choices": [
        {"id": "staged", "label": "Staged rollout", "description": "Keep the rollout reversible."},
        {"id": "hold", "label": "Hold", "description": "Wait for more evidence."},
    ],
    "recommended_default": {"id": "staged"},
    "idempotency_key": "inbox-functional-v2",
    "host_id": "host/inbox-cleanup",
    "runner_session_id": "runner/inbox-cleanup",
    "context": {"reason_code": "operator_decision_required", "blast_radius": {"tasks": 1}},
}, actor="inbox-cleanup-test", project="switchboard")

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
base = f"http://127.0.0.1:{port}"
server = subprocess.Popen(
    [sys.executable, "app.py"], cwd=ROOT, env={**env, "PM_PORT": str(port)},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("PASS " if condition else "FAIL ") + message)
    passed += int(condition)
    failed += int(not condition)


def wait_ready(timeout: float = 25) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    raise RuntimeError("server did not become ready")


try:
    wait_ready()
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{base}/?project=switchboard#tab-inbox-hub", wait_until="networkidle")
        page.wait_for_selector(".tk-inbox-alert")

        ok(page.locator(".tk-inbox-tabs .nav-link").count() == 5,
           "desktop keeps the approved Inbox views plus Email recovery")
        list_box = page.locator(".tk-inbox-list-pane").bounding_box()
        detail_box = page.locator(".tk-inbox-detail-pane").bounding_box()
        ok(bool(list_box and detail_box and list_box["x"] < detail_box["x"]),
           "desktop uses the approved list and detail layout")
        ok(page.locator('[data-choice="staged"]').is_visible(),
           "existing provider decisions remain available")
        ok(not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"),
           "desktop Inbox has no page-level horizontal overflow")

        page.locator("#needs-search").fill("nothing-matches-this")
        ok(page.locator("text=No matching alerts").is_visible(), "Needs you search filters live items")
        page.locator("#needs-search").fill("")
        page.locator('.tk-inbox-tabs a[href="#tab-inbox"]').click()
        ok(page.locator("#q-search").is_visible(), "Action Queue keeps its live filters")
        page.locator('.tk-inbox-tabs a[href="#tab-email-inbox"]').click()
        ok(page.locator("#email-inbox-content").is_visible()
           and page.locator("text=Project email history").is_visible(),
           "Email history and quarantine recovery remain reachable")
        page.locator('.tk-inbox-tabs a[href="#tab-decisions"]').click()
        ok(page.locator("#decisions-table").is_visible(), "Decisions remains reachable")
        page.locator('.tk-inbox-tabs a[href="#tab-risks"]').click()
        ok(page.locator("#risks-table").is_visible(), "Risks remains reachable")

        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{base}/?project=switchboard&viewport=mobile#tab-inbox-hub", wait_until="networkidle")
        page.wait_for_selector(".tk-inbox-alert")
        ok(page.locator(".tk-inbox-list-pane").is_visible()
           and not page.locator(".tk-inbox-detail-pane").is_visible(),
           "mobile opens on the scan-friendly alert list")
        page.locator(".tk-inbox-alert").first.click()
        ok(page.locator("#needs-back").is_visible()
           and page.locator('[data-choice="staged"]').is_visible(),
           "mobile alert opens the existing actionable detail")
        page.locator("#needs-back").click()
        ok(page.locator(".tk-inbox-list-pane").is_visible(),
           "mobile Back returns to the Inbox list")
        ok(not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"),
           "mobile Inbox has no page-level horizontal overflow")
        ok(not errors, f"browser reports no page errors: {errors[:3]}")
        browser.close()
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()

print(f"{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
