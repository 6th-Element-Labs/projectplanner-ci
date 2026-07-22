#!/usr/bin/env python3
"""UI-61: ⌘K / Search jumps to the task modal (and deliverable page)."""
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

tmp = Path(tempfile.mkdtemp(prefix="ui61-cmdk-"))
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
os.environ.update({k: v for k, v in env.items() if k.startswith("PM_")})
(tmp / "projects").mkdir(parents=True)

import store  # noqa: E402

store.init_project_registry()
store.init_db("maxwell")
TASK = store.create_task(
    {"workstream_id": "UI", "title": "Cmdk jump target"},
    actor="ui61-test", project="maxwell",
)
TASK_ID = TASK["task_id"]
DL = store.create_deliverable(
    {
        "title": "Cmdk Deliverable Target",
        "end_state": "reachable from palette",
        "status": "in_progress",
    },
    actor="ui61-test",
    project="maxwell",
)
assert isinstance(DL, dict) and not DL.get("error"), DL
DL_ID = DL.get("id") or DL.get("deliverable_id")
assert DL_ID, DL

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("PASS " if condition else "FAIL ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
base = f"http://127.0.0.1:{port}"
server = subprocess.Popen(
    [sys.executable, "app.py"], cwd=ROOT, env={**env, "PM_PORT": str(port)},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


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
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{base}/?project=maxwell", wait_until="networkidle")
        page.wait_for_function("() => typeof TeepPlan !== 'undefined' && Array.isArray(TeepPlan.tasks) && TeepPlan.tasks.length > 0")

        page.keyboard.press("Meta+k")
        page.wait_for_selector("#cmdk-backdrop.show")
        page.fill("#cmdk-input", TASK_ID)
        page.wait_for_selector(".cmdk-item", timeout=5000)
        labels = page.locator(".cmdk-item").all_inner_texts()
        ok(any(TASK_ID in (t or "") for t in labels), f"palette lists task {TASK_ID}")
        page.keyboard.press("Enter")
        page.wait_for_selector("#task-modal.show", timeout=5000)
        title = page.locator("#task-modal-title").inner_text()
        ok(TASK_ID in title, "selecting a task opens the task modal")

        # Fresh load for the deliverable jump (Bootstrap modal hide is flaky under headless).
        page.goto(f"{base}/?project=maxwell", wait_until="networkidle")
        page.wait_for_function(
            "() => typeof TeepPlan !== 'undefined' && Array.isArray(TeepPlan.deliverables)"
        )
        page.evaluate("() => window.TAIKUN_openCmdk && window.TAIKUN_openCmdk('Cmdk Deliverable')")
        page.wait_for_selector("#cmdk-backdrop.show")
        page.wait_for_function(
            """() => Array.from(document.querySelectorAll('.cmdk-item'))
                .some(el => (el.textContent || '').includes('Cmdk Deliverable Target'))"""
        )
        page.evaluate(
            """() => {
              const items = Array.from(document.querySelectorAll('.cmdk-item'));
              const hit = items.find(el => (el.textContent || '').includes('Cmdk Deliverable Target'));
              if (hit) hit.click();
            }"""
        )
        page.wait_for_function(
            "() => !!document.querySelector('#toptab-mission.active')"
            f" || (new URL(location.href).searchParams.get('deliverable') || '') === {DL_ID!r}"
        )
        ok(
            page.evaluate(
                "() => !!document.querySelector('#toptab-mission.active')"
                f" || (new URL(location.href).searchParams.get('deliverable') || '') === {DL_ID!r}"
            ),
            "selecting a deliverable opens the Deliverable tab",
        )
        browser.close()
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
