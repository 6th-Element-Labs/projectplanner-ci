#!/usr/bin/env python3
"""Playwright regression for the responsive Plan cleanup."""
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

tmp = Path(tempfile.mkdtemp(prefix="plan-cleanup-browser-"))
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

store.init_project_registry()
store.init_db("maxwell")
for title, phase, status in (
    ("Plan cleanup ready task", "Build", "Not Started"),
    ("Plan cleanup active task", "P0 Next", "In Progress"),
):
    store.create_task(
        {"workstream_id": "UI", "title": title, "phase": phase, "status": status},
        actor="plan-cleanup-test", project="maxwell",
    )

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("PASS " if condition else "FAIL ") + message)
    passed += int(condition)
    failed += int(not condition)


with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
base = f"http://127.0.0.1:{port}"
server = subprocess.Popen(
    [sys.executable, "app.py"], cwd=ROOT, env={**env, "PM_PORT": str(port)},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)


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
        page.goto(f"{base}/?project=maxwell#tab-plan-hub", wait_until="networkidle")
        page.wait_for_selector(".tk-plan-epic-row", timeout=10000)

        ok(page.locator(".tk-plan-tabs .nav-link").count() == 4,
           "desktop keeps the four approved Plan views")
        ok(page.locator(".tk-plan-epic-row").count() >= 1,
           "live Plan data renders as epic summary rows")
        ok(not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"),
           "desktop Plan has no page-level horizontal overflow")

        page.locator('.tk-plan-tabs a[href="#tab-board"]').click()
        ok(page.locator("#tab-board").is_visible(), "Board remains interactive")
        page.locator('.tk-plan-tabs a[href="#tab-gantt"]').click()
        ok(page.locator("#tab-gantt").is_visible(), "Timeline remains interactive")
        page.locator('.tk-plan-tabs a[href="#tab-plan"]').click()
        ok(page.locator("#tab-plan").is_visible(), "Milestones remain interactive")

        page.locator('.tk-plan-tabs a[href="#tab-epics"]').click()
        page.locator(".tk-plan-epic-row").first.click()
        task_link = page.locator("#epics-content .collapse.show a[data-task]").first
        task_link.wait_for(state="visible", timeout=5000)
        task_link.click()
        page.wait_for_selector("#task-modal.show", timeout=5000)
        ok(page.locator("#task-modal").is_visible(),
           "Plan tasks still open the existing task-details modal")
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{base}/?project=maxwell&viewport=mobile#tab-plan-hub", wait_until="networkidle")
        page.wait_for_selector(".tk-plan-epic-row", timeout=10000)
        page.locator('.tk-plan-tabs a[href="#tab-board"]').click()
        widths = page.locator("#tab-board .tk-board-col").evaluate_all(
            "els => els.map(el => Math.round(el.getBoundingClientRect().width))"
        )
        ok(bool(widths) and all(width <= 390 for width in widths),
           "mobile Board columns stack within the viewport")
        page.locator('.tk-plan-tabs a[href="#tab-plan"]').click()
        ok(not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"),
           "mobile Plan and Milestones have no page-level horizontal overflow")
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
