#!/usr/bin/env python3
"""UI-78: the approved top toolbar, with side/mobile navigation preserved."""
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


tmp = Path(tempfile.mkdtemp(prefix="ui78-toolbar-"))
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
from execution_readiness_fixture import configure_ready_project  # noqa: E402

store.init_project_registry()
store.init_db("maxwell")
configure_ready_project("maxwell", actor="ui78-test")
task = store.create_task(
    {"workstream_id": "UI", "title": "Toolbar Autopilot target"},
    actor="ui78-test", project="maxwell",
)
store.create_deliverable({
    "id": "ui78-toolbar", "title": "Approved toolbar",
    "status": "approved", "end_state": "Top toolbar starts the existing Autopilot path.",
}, actor="ui78-test", project="maxwell")
store.link_task_to_deliverable(
    "ui78-toolbar", "maxwell", task["task_id"],
    data={"role": "contributes", "blocks_deliverable": True},
    actor="ui78-test", project="maxwell",
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


try:
    wait_ready()
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        console_errors: list[str] = []
        failed_requests: list[str] = []
        page.on("console", lambda message: console_errors.append(message.text)
                if message.type == "error" else None)
        page.on("requestfailed", lambda request: failed_requests.append(request.url))
        page.add_init_script("""
          window.mermaid = {
            initialize: () => {},
            render: async () => ({svg: '<svg data-test-mermaid="1"></svg>'})
          };
        """)
        page.goto(f"{base}/?project=maxwell&deliverable=ui78-toolbar", wait_until="networkidle")
        page.wait_for_function(
            "() => typeof TeepPlan !== 'undefined' && TeepPlan.selectedDeliverableId === 'ui78-toolbar'"
        )

        expected = ["f-search", "btn-ack-inbox", "btn-new-task", "btn-autopilot", "user-menu"]
        boxes = [page.locator(f"#{element_id}").bounding_box() for element_id in expected]
        assert all(boxes), boxes
        assert all(boxes[index]["x"] < boxes[index + 1]["x"] for index in range(len(boxes) - 1)), boxes
        toolbar_height = page.locator(".tk-toolbar").evaluate(
            "el => Math.round(el.getBoundingClientRect().height)"
        )
        assert toolbar_height <= 50, toolbar_height
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert page.locator("#btn-autopilot").inner_text().strip() == "Autopilot"

        page.locator("#toptab-plan").click()
        page.wait_for_function("() => document.querySelector('#toolbar-context').textContent.includes('Plan')")
        context = " ".join(page.locator("#toolbar-context").inner_text().split())
        assert "Project Maxwell" in context and "·" in context and "Plan" in context, context

        with page.expect_response(
            lambda response: response.request.method == "POST"
            and "/api/deliverables/ui78-toolbar/autopilot" in response.url
        ) as response_info:
            page.locator("#btn-autopilot").click()
        assert response_info.value.ok, response_info.value.status
        scopes = page.evaluate("""async () => (await (await fetch(
          'api/deliverables/ui78-toolbar/autopilot')).json()).scopes""")
        assert len(scopes) == 1 and scopes[0]["scope_type"] == "deliverable", scopes

        page.set_viewport_size({"width": 1024, "height": 768})
        tablet_overflow = page.evaluate("""() => ({
          scrollWidth: document.documentElement.scrollWidth,
          innerWidth: window.innerWidth,
          offenders: Array.from(document.querySelectorAll('body *')).map(el => {
            const r = el.getBoundingClientRect();
            return {tag: el.tagName, id: el.id, cls: String(el.className), left: r.left, right: r.right};
          }).filter(x => x.left < -1 || x.right > window.innerWidth + 1).slice(0, 12)
        })""")
        assert tablet_overflow["scrollWidth"] <= tablet_overflow["innerWidth"], tablet_overflow
        assert page.locator("#btn-autopilot").evaluate("el => getComputedStyle(el).display") != "none"

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(350)
        assert page.locator("#btn-autopilot").evaluate("el => getComputedStyle(el).display") == "none"
        assert page.locator(".navbar-vertical").evaluate("el => getComputedStyle(el).display") == "none"
        assert page.locator(".tk-mobile-nav").evaluate("el => getComputedStyle(el).display") == "grid"
        assert page.locator(".tk-mobile-nav > .nav-link").count() == 4
        mobile_overflow = page.evaluate("""() => ({
          scrollWidth: document.documentElement.scrollWidth,
          innerWidth: window.innerWidth,
          offenders: Array.from(document.querySelectorAll('body *')).map(el => {
            const r = el.getBoundingClientRect();
            return {tag: el.tagName, id: el.id, cls: String(el.className), left: r.left, right: r.right};
          }).filter(x => x.left < -1 || x.right > window.innerWidth + 1).slice(0, 12)
        })""")
        assert mobile_overflow["scrollWidth"] <= mobile_overflow["innerWidth"], mobile_overflow
        assert not console_errors, console_errors
        assert not failed_requests, failed_requests
        browser.close()
    print("PASS UI-78 approved top toolbar and existing Autopilot action")
    print("PASS UI-78 side/mobile navigation regression boundary")
finally:
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
    shutil.rmtree(tmp, ignore_errors=True)
