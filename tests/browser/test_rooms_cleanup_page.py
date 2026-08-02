#!/usr/bin/env python3
"""Playwright regression for the responsive, functional Rooms page."""
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

tmp = Path(tempfile.mkdtemp(prefix="rooms-cleanup-browser-"))
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
store.init_db("switchboard")
task = store.create_task(
    {"workstream_id": "UI", "title": "Make Rooms simple and useful",
     "description": "Keep the shared task at the center of the conversation."},
    actor="rooms-cleanup-test", project="switchboard",
)
store.register_agent(
    "codex/rooms-fixture", "codex", lane="UI", task_id=task["task_id"],
    ttl_s=600, actor="rooms-cleanup-test", project="switchboard",
)
store.send_agent_message(
    "rooms-reviewer", "codex/rooms-fixture", "Keep the artifact, not chat chrome, at the center.",
    task_id=task["task_id"], requires_ack=True, ack_deadline_minutes=15,
    project="switchboard",
)

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
        page.goto(f"{base}/?project=switchboard#tab-rooms", wait_until="networkidle")
        page.wait_for_selector(".tk-room-list-item")

        ok(page.locator("#toptab-rooms").is_visible(), "Rooms is a real Collaborate destination")
        list_box = page.locator(".tk-rooms-list-pane").bounding_box()
        room_box = page.locator(".tk-room-view").bounding_box()
        ok(bool(list_box and room_box and list_box["x"] < room_box["x"]),
           "desktop uses the approved room-list and shared-work layout")
        ok(page.locator(".tk-room-artifact-title").text_content() == "Make Rooms simple and useful",
           "the room centers the real linked task")
        ok(page.locator(".tk-room-message-copy").filter(has_text="Keep the artifact").is_visible(),
           "existing coordination messages remain visible")
        ok(page.locator("#room-recipient").input_value() == "codex/rooms-fixture",
           "the composer targets a real room agent")
        page.locator("#room-message").fill("Ship the clean Rooms view.")
        page.locator("#room-send").click()
        page.wait_for_selector(".tk-room-message-copy:text-is('Ship the clean Rooms view.')")
        ok(page.locator(".tk-room-message-copy:text-is('Ship the clean Rooms view.')").is_visible(),
           "the composer sends through the existing message API")
        ok(not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"),
           "desktop Rooms has no page-level horizontal overflow")

        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{base}/?project=switchboard&viewport=mobile#tab-rooms", wait_until="networkidle")
        page.wait_for_selector(".tk-room-list-item")
        ok(page.locator(".tk-rooms-list-pane").is_visible() and not page.locator(".tk-room-view").is_visible(),
           "mobile opens on the scan-friendly room list")
        page.locator(".tk-room-list-item").first.click()
        ok(page.locator("#rooms-back").is_visible() and page.locator(".tk-room-artifact-title").is_visible(),
           "mobile opens one focused shared-work view")
        page.locator("#rooms-back").click()
        ok(page.locator(".tk-rooms-list-pane").is_visible(), "mobile Back returns to Rooms")
        ok(not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"),
           "mobile Rooms has no page-level horizontal overflow")
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
