#!/usr/bin/env python3
"""UI-27: Chromium journey for deliverable/task Autopilot controls."""
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


tmp = Path(tempfile.mkdtemp(prefix="ui27-browser-"))
env = dict(os.environ)
env.update({
    "PM_DB_PATH": str(tmp / "maxwell.db"),
    "PM_HELM_DB_PATH": str(tmp / "helm.db"),
    "PM_SWITCHBOARD_DB_PATH": str(tmp / "switchboard.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": str(tmp / "registry.db"),
    "PM_DYNAMIC_PROJECTS_DIR": str(tmp / "projects"),
    "PM_AUTH_MODE": "dev-open",
    "PYTHONPATH": (
        f"{ROOT / 'tests' / 'browser' / 'ui27_pythonpath'}:"
        f"{ROOT}:{ROOT / 'src'}"
    ),
})
os.environ.update({key: value for key, value in env.items() if key.startswith("PM_")})
(tmp / "projects").mkdir(parents=True)

import store  # noqa: E402
from execution_policy_fixture import install_ready_execution_policy  # noqa: E402

store.init_project_registry()
store.init_db("maxwell")
store.set_project_repo_topology(
    project="maxwell",
    canonical_repo="6th-Element-Labs/projectplanner",
    canonical_default_branch="master",
)
install_ready_execution_policy("maxwell")
store.create_task({"workstream_id": "AUTO", "title": "First ready task"},
                  actor="test", project="maxwell")
store.create_task({"workstream_id": "AUTO", "title": "Blocked follow-on",
                   "depends_on": ["AUTO-1"]}, actor="test", project="maxwell")
store.create_deliverable({
    "id": "ui27-browser", "title": "Task-first Autopilot",
    "status": "approved", "end_state": "The whole deliverable drains.",
}, actor="test", project="maxwell")
for task_id in ("AUTO-1", "AUTO-2"):
    store.link_task_to_deliverable(
        "ui27-browser", "maxwell", task_id,
        data={"role": "contributes", "blocks_deliverable": True},
        actor="test", project="maxwell")

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


try:
    wait_ready()
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        console_errors: list[str] = []
        page.on("console", lambda message: console_errors.append(message.text)
                if message.type == "error" else None)
        # Keep the journey hermetic: the app sees a complete-enough local
        # Mermaid API and never requests the CDN bundle.
        page.add_init_script("""
          window.mermaid = {
            initialize: () => {},
            render: async () => ({svg: '<svg data-test-mermaid="1"></svg>'})
          };
        """)
        page.goto(base + "/?project=maxwell&deliverable=ui27-browser#tab-mission")
        page.wait_for_selector('[data-autopilot-action="start"][data-autopilot-scope="deliverable"]')

        page.locator('[data-autopilot-action="start"][data-autopilot-scope="deliverable"]').click()
        page.wait_for_selector('[data-autopilot-action="pause"][data-autopilot-scope="deliverable"]')
        scopes = page.evaluate("""async () => (await (await fetch(
          'api/deliverables/ui27-browser/autopilot')).json()).scopes""")
        assert len(scopes) == 1 and scopes[0]["scope_type"] == "deliverable", scopes

        page.locator('[data-autopilot-action="pause"][data-autopilot-scope="deliverable"]').click()
        page.wait_for_selector('[data-autopilot-action="resume"][data-autopilot-scope="deliverable"]')
        page.locator('[data-autopilot-action="resume"][data-autopilot-scope="deliverable"]').click()
        page.wait_for_selector('[data-autopilot-action="stop"][data-autopilot-scope="deliverable"]')
        page.locator('[data-autopilot-action="stop"][data-autopilot-scope="deliverable"]').click()
        page.wait_for_selector('[data-autopilot-action="start"][data-autopilot-scope="deliverable"]')

        # Start two individual tasks. AUTO-2 is dependency-blocked but must stay
        # durably armed rather than failing or silently disappearing.
        for task_id in ("AUTO-1", "AUTO-2"):
            page.locator(f'.mission-dag-node[data-linked-task="{task_id}"]').click()
            page.wait_for_selector("#dl-node-modal.show")
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and f"/tasks/{task_id}/autopilot" in response.url
            ) as response_info:
                page.locator('#dl-node-autopilot [data-autopilot-action="start"]').click()
            response = response_info.value
            assert response.ok and response.json().get("task_id") == task_id, (
                response.status, response.text())
            # Reload the cockpit between task starts. This proves the scope is
            # durable across navigation and avoids coupling the contract test to
            # Bootstrap's non-contractual fade timing.
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector(f'.mission-dag-node[data-linked-task="{task_id}"]')
        scopes = page.evaluate("""async () => (await (await fetch(
          'api/deliverables/ui27-browser/autopilot')).json()).scopes""")
        assert {row["task_id"] for row in scopes} == {"AUTO-1", "AUTO-2"}, scopes
        assert all(row["scope_type"] == "task" and row["status"] == "active"
                   for row in scopes), scopes
        assert not console_errors, console_errors
        browser.close()
    print("PASS UI-27 deliverable Start/Pause/Resume/Stop uses durable scope state")
    print("PASS UI-27 two task clicks arm independent ready and blocked tasks")
finally:
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
    shutil.rmtree(tmp, ignore_errors=True)
