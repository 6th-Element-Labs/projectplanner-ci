#!/usr/bin/env python3
"""Playwright regression for UI-82's conversation-first Scope workspace."""
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
from playwright.sync_api import Route, sync_playwright  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="scope-v4-browser-"))
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
        chat_posts: list[dict] = []
        chat_deletes: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        def chat_history(route: Route) -> None:
            route.fulfill(status=200, content_type="application/json", body=(
                '{"project":"maxwell","session":"scope","messages":['
                '{"role":"assistant","content":"Start with the measurable outcome.","payload":{"sources":["docs/INDEX.md"]}}]}'
            ))

        def chat_command(route: Route) -> None:
            if route.request.method == "POST":
                chat_posts.append(route.request.post_data_json)
                route.fulfill(status=202, content_type="application/json", body=(
                    '{"run_id":"scope-test-run","project":"maxwell","status":"pending"}'
                ))
            else:
                chat_deletes.append(route.request.url)
                route.fulfill(status=200, content_type="application/json", body='{"cleared":"scope"}')

        page.route("**/api/chat/history?*", chat_history)
        page.route("**/api/chat?*", chat_command)
        page.route("**/api/chat/runs/scope-test-run?*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"run_id":"scope-test-run","status":"completed","answer":"Proposed wording only.","sources":["docs/INDEX.md"]}',
        ))

        page.goto(f"{base}/?project=maxwell#tab-scope", wait_until="networkidle")
        page.wait_for_selector("#scope-chat-log .tk-scope-bubble")
        ok(page.locator(".tk-scope-conversation").is_visible(),
           "desktop makes the Scope discussion the primary workspace")
        ok(not page.locator("#scope-artifact-drawer").evaluate("el => el.classList.contains('show')")
           and page.locator("#scope-artifact-drawer").get_attribute("aria-hidden") == "true",
           "the artifact begins closed instead of competing with discussion")

        conversation_before = page.locator(".tk-scope-conversation").bounding_box()
        page.locator("#scope-artifact-open").click()
        page.wait_for_selector("#scope-artifact-drawer.show")
        page.wait_for_timeout(300)
        conversation_after = page.locator(".tk-scope-conversation").bounding_box()
        drawer_box = page.locator("#scope-artifact-drawer").bounding_box()
        ok(bool(conversation_before and conversation_after and drawer_box
                and conversation_after["width"] < conversation_before["width"]
                and conversation_after["x"] < drawer_box["x"]),
           "desktop artifact drawer compresses the conversation like Watch")
        ok(page.locator("#scope-switch button").count() == 5,
           "artifact preserves all five server-backed kickoff sections")

        page.locator("#scope-drawer-close").click()
        page.locator("#scope-chat-input").fill("Define the proof without changing approval state.")
        page.locator("#scope-chat-send").click()
        page.wait_for_selector("text=Proposed wording only.")
        ok(bool(chat_posts and chat_posts[-1].get("session") == "scope"),
           "web Scope messages use the durable project scope session")
        kickoff = page.evaluate("async () => (await (await fetch('api/kickoff?project=maxwell')).json())")
        ok(not kickoff.get("build_authorized") and kickoff["gates"][0]["s"] != "ok",
           "conversation does not silently approve the kickoff record")

        page.locator("#scope-review").click()
        page.wait_for_selector("#scope-artifact-drawer.expanded")
        page.locator('[data-scope-approve="vision"]').click()
        page.wait_for_selector('[data-scope-revise="vision"]')
        kickoff = page.evaluate("async () => (await (await fetch('api/kickoff?project=maxwell')).json())")
        ok(kickoff["gates"][0]["s"] == "ok",
           "only the explicit artifact approval updates kickoff history")

        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{base}/?project=maxwell&viewport=mobile#tab-scope", wait_until="networkidle")
        page.wait_for_selector("#scope-artifact-open")
        ok(page.locator("#scope-artifact-open").is_visible(),
           "mobile keeps the persistent Scope version control by the composer")
        ok(page.locator("#scope-back").is_visible()
           and page.locator("#scope-back").inner_text().strip() == "Overview",
           "mobile Scope provides an explicit exit to the main page")
        ok(page.locator("#scope-chat-clear").inner_text().strip() == "Delete conversation",
           "Scope conversation deletion is a visible labelled action")
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator("#scope-chat-clear").click()
        page.wait_for_selector("#scope-chat-empty")
        ok(len(chat_deletes) == 1,
           "Delete conversation confirms and clears the durable Scope session")

        page.evaluate("""() => {
            document.querySelector('#fleet-dock').innerHTML =
              '<div class="card" style="position:fixed;right:1rem;bottom:1rem;z-index:1031">Fleet sheet</div>';
        }""")
        fleet_box = page.locator("#fleet-dock > .card").bounding_box()
        nav_box = page.locator(".tk-mobile-nav").bounding_box()
        ok(bool(fleet_box and nav_box
                and fleet_box["y"] + fleet_box["height"] <= nav_box["y"] + 1),
           "expanded Fleet sheet preserves the mobile navigation escape route")
        page.evaluate("document.querySelector('#fleet-dock').innerHTML = ''")

        page.evaluate("""() => {
            document.querySelector('#saturation-dock').innerHTML =
              '<button id="saturation-critical-banner" type="button" class="alert alert-danger tk-pressure-banner text-start w-100" role="alert">'
              + '<span class="d-flex align-items-center gap-2"><i class="ti ti-alert-triangle"></i>'
              + '<span class="flex-fill"><strong>System pressure is critical.</strong> Load shedding is active.</span>'
              + '<i class="ti ti-chevron-right"></i></span></button>';
        }""")
        banner_box = page.locator("#saturation-critical-banner").bounding_box()
        scope_back_box = page.locator("#scope-back").bounding_box()
        ok(bool(banner_box and scope_back_box
                and scope_back_box["y"] >= banner_box["y"] + banner_box["height"]),
           "critical system status leaves the mobile Scope exit unobscured")

        page.locator("#scope-back").click()
        page.wait_for_selector("#tab-exec.active")
        ok(page.evaluate("location.hash") == "#tab-exec",
           "top-level navigation records the selected page in the URL")
        page.go_back(wait_until="networkidle")
        page.wait_for_selector("#tab-scope.active")
        ok(page.evaluate("location.hash") == "#tab-scope",
           "browser Back returns to Scope with the visible pane and URL aligned")

        page.locator("#scope-artifact-open").click()
        page.wait_for_selector("#scope-artifact-drawer.show")
        mobile_drawer = page.locator("#scope-artifact-drawer").bounding_box()
        ok(bool(mobile_drawer and mobile_drawer["width"] <= 390 and mobile_drawer["y"] > 0),
           "mobile opens the artifact as a bounded bottom sheet")
        page.locator("#scope-drawer-expand").click()
        expanded = page.locator("#scope-artifact-drawer").bounding_box()
        ok(bool(expanded and expanded["width"] <= 390 and expanded["y"] <= 1),
           "mobile Scope expands to a full-screen review state")
        ok(not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"),
           "mobile Scope has no page-level horizontal overflow")
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
