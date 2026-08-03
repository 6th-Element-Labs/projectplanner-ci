#!/usr/bin/env python3
"""UI-86: responsive Deliverables relationship sheet and task workspace."""
from __future__ import annotations

import os
import shutil
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


tmp = Path(tempfile.mkdtemp(prefix="ui86-browser-"))
screenshots = Path(os.environ["UI86_SCREENSHOT_DIR"]) if os.environ.get("UI86_SCREENSHOT_DIR") else None
if screenshots:
    screenshots.mkdir(parents=True, exist_ok=True)
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
task = store.create_task({
    "workstream_id": "UI",
    "title": "Clean task detail workspace",
    "description": "Keep the outcome visible while detailed implementation notes remain available.",
    "entry_criteria": "The task is linked to an approved deliverable.",
    "exit_criteria": "Desktop and mobile show one modal layer.",
    "deliverable": "A focused task workspace.",
    "phase": "Build",
    "status": "Not Started",
    "risk_level": "High",
}, actor="ui86-test", project="maxwell")
store.create_deliverable({
    "id": "ui86-modal-cleanup",
    "title": "Task modal cleanup",
    "status": "approved",
    "end_state": "Task details are calm and usable everywhere.",
}, actor="ui86-test", project="maxwell")
store.link_task_to_deliverable(
    "ui86-modal-cleanup", "maxwell", task["task_id"],
    data={"role": "implementation", "blocks_deliverable": True},
    actor="ui86-test", project="maxwell",
)

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
        if server.poll() is not None:
            output = server.stdout.read() if server.stdout else ""
            raise RuntimeError(f"app exited before ready: {output[-2000:]}")
        time.sleep(0.2)
    raise TimeoutError("app did not become ready")


def open_relationship(page) -> None:
    page.goto(
        base + "/?project=maxwell&deliverable=ui86-modal-cleanup#tab-mission",
        wait_until="domcontentloaded",
    )
    page.locator('[data-mission-view="map"]').click()
    page.locator(f'.mission-dag-node[data-linked-task="{task["task_id"]}"]').wait_for()
    page.locator(f'.mission-dag-node[data-linked-task="{task["task_id"]}"]').click()
    page.locator("#dl-node-modal.show").wait_for(state="visible")
    page.wait_for_timeout(250)


try:
    wait_ready()
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        errors: list[str] = []

        desktop = browser.new_page(viewport={"width": 1440, "height": 960})
        desktop.on("pageerror", lambda error: errors.append(str(error)))
        desktop.add_init_script("""
          window.mermaid = {
            initialize: () => {},
            render: async () => ({svg: '<svg data-test-mermaid="1"></svg>'})
          };
        """)
        open_relationship(desktop)
        if screenshots:
            desktop.screenshot(path=str(screenshots / "relationship-desktop.png"))
        assert desktop.locator("#dl-node-title").get_by_text(task["task_id"]).is_visible()
        assert desktop.locator("#dl-node-title").get_by_text(task["title"]).is_visible()
        assert desktop.get_by_text("These affect the task, not its deliverable link.").is_visible()
        assert desktop.get_by_role("button", name="Save relationship").is_visible()
        assert desktop.get_by_role("button", name="Remove link").is_visible()

        desktop.locator("#dl-node-open").click()
        desktop.locator("#dl-node-modal").wait_for(state="hidden")
        desktop.locator("#task-modal.show").wait_for(state="visible")
        desktop.wait_for_timeout(250)
        if screenshots:
            desktop.screenshot(path=str(screenshots / "task-desktop.png"))
        assert desktop.locator(".modal-backdrop").count() == 1
        assert desktop.locator("#task-modal-title").get_by_text(task["title"]).is_visible()
        assert desktop.locator(".tk-task-tabs .nav-link").all_inner_texts() == [
            "Overview", "Edit", "Agent", "Activity",
        ]
        assert desktop.get_by_role("heading", name="Outcome").is_visible()
        assert desktop.get_by_role("heading", name="Acceptance").is_visible()
        assert desktop.get_by_role("heading", name="Properties").is_visible()
        assert desktop.get_by_text("More context — economics, project authority, and repository roles").is_visible()
        assert not desktop.evaluate(
            "document.querySelector('#task-modal .modal-content').scrollWidth > "
            "document.querySelector('#task-modal .modal-content').clientWidth"
        )
        desktop.locator("#task-modal-edit").click()
        assert "active" in desktop.locator('.tk-task-tabs a[href="#m-edit"]').get_attribute("class")
        desktop.locator("#edit-description").fill("Updated without losing the task header hierarchy.")
        desktop.locator("#edit-save").click()
        desktop.locator("#edit-flash").get_by_text("Saved", exact=True).wait_for()
        assert desktop.locator("#task-modal-title .tk-task-title-kicker").is_visible()
        assert desktop.locator("#task-modal-title .tk-task-title-text").get_by_text(task["title"]).is_visible()
        desktop.locator("#task-modal-discuss").click()
        assert "active" in desktop.locator('.tk-task-tabs a[href="#m-activity"]').get_attribute("class")
        desktop.close()

        mobile = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
        mobile.on("pageerror", lambda error: errors.append(str(error)))
        mobile.add_init_script("""
          window.mermaid = {
            initialize: () => {},
            render: async () => ({svg: '<svg data-test-mermaid="1"></svg>'})
          };
        """)
        open_relationship(mobile)
        if screenshots:
            mobile.screenshot(path=str(screenshots / "relationship-mobile.png"))
        relation_box = mobile.locator("#dl-node-modal .modal-content").bounding_box()
        assert relation_box and relation_box["x"] >= 0
        assert relation_box["x"] + relation_box["width"] <= 390
        assert relation_box["y"] + relation_box["height"] >= 842
        mobile.locator("#dl-node-open").click()
        mobile.locator("#dl-node-modal").wait_for(state="hidden")
        mobile.locator("#task-modal.show").wait_for(state="visible")
        mobile.wait_for_timeout(250)
        if screenshots:
            mobile.screenshot(path=str(screenshots / "task-mobile.png"))
        task_box = mobile.locator("#task-modal .modal-content").bounding_box()
        assert task_box and round(task_box["width"]) == 390
        assert round(task_box["height"]) == 844
        assert mobile.locator(".modal-backdrop").count() == 1
        assert not mobile.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        assert mobile.locator("#task-modal-edit").is_visible()
        assert mobile.locator("#task-modal-discuss").is_visible()
        assert mobile.locator(".tk-task-tabs .nav-link").all_inner_texts() == [
            "Overview", "Edit", "Agent", "Activity",
        ]
        assert all(mobile.locator(".tk-task-tabs .nav-link").nth(index).is_visible() for index in range(4))

        mobile.set_viewport_size({"width": 320, "height": 740})
        mobile.wait_for_timeout(150)
        narrow_box = mobile.locator("#task-modal .modal-content").bounding_box()
        assert narrow_box and round(narrow_box["width"]) == 320
        assert not mobile.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        assert all(mobile.locator(".tk-task-tabs .nav-link").nth(index).is_visible() for index in range(4))
        assert not errors, errors
        mobile.close()
        browser.close()
    print("PASS UI-86 Deliverables relationship sheet and full task workspace on desktop/mobile")
finally:
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
    shutil.rmtree(tmp, ignore_errors=True)
