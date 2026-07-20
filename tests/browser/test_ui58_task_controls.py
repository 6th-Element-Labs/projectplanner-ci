#!/usr/bin/env python3
"""UI-58: the task-modal Stop/Retry controls go through the command routes.

Proves the browser names only the task — never a runner id — and that Stop and
Retry hit the shared Task Session command endpoints, so the server owns which
execution is stopped or superseded. The card's live state is read from the
execution projection, not the runner-watch surface.
"""
from __future__ import annotations

import json
import os
import re
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

tmp = Path(tempfile.mkdtemp(prefix="ui58-controls-"))
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

# Seed the task server-side (like UI-27) so openTask's background loaders get
# real 200s — then "no console errors" is a meaningful assertion, not noise from
# an in-memory-only fixture.
import store  # noqa: E402

store.init_project_registry()
store.init_db("maxwell")
TASK_ID = store.create_task(
    {"workstream_id": "UI", "title": "UI-58 controls"},
    actor="ui58-test", project="maxwell")["task_id"]

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
        if server.poll() is not None:
            raise RuntimeError((server.stdout.read() if server.stdout else "")[-2000:])
        time.sleep(0.2)
    raise TimeoutError("app did not become ready")


RUNNING = {
    "schema": "switchboard.task_execution.v1", "command": "get_task_execution",
    "running": True, "starting": False, "has_ended_session": False,
    "resumable_review": False, "lifecycle_phase": "running",
    "execution_id": "run-ui58-live",
    "execution": {"active_runner": {"runner_session_id": "run-ui58-live",
                  "host_id": "host/ui58-mac", "status": "running"},
                  "active_host": {"host_id": "host/ui58-mac"}},
}
ENDED = {
    "schema": "switchboard.task_execution.v1", "command": "get_task_execution",
    "running": False, "starting": False, "has_ended_session": True,
    "resumable_review": False, "lifecycle_phase": "start_failed_retry",
    "execution_id": None, "execution": {"active_runner": None},
}

try:
    wait_ready()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        console_errors: list[str] = []
        page.on("console", lambda m: console_errors.append(m.text)
                if m.type == "error" else None)
        page.add_init_script(
            "window.mermaid={initialize:()=>{},render:async()=>({svg:'<svg></svg>'})};")
        page.goto(base + "/?project=maxwell")
        page.wait_for_function("() => typeof TeepPlan !== 'undefined'")
        page.wait_for_function("() => Array.isArray(TeepPlan.tasks) && TeepPlan.tasks.length > 0")

        posts: list[dict] = []

        def _record_post(route):
            posts.append({"url": route.request.url,
                          "body": json.loads(route.request.post_data or "{}")})
            route.fulfill(status=200, content_type="application/json",
                          body='{"schema":"switchboard.task_execution.v1","action":"started"}')

        page.route(re.compile(r"/api/tasks/[^/]+/execution/stop"), _record_post)
        page.route(re.compile(r"/api/tasks/[^/]+/execution/retry"), _record_post)

        # ---- running -> Stop, and Stop POSTs to the command route -----------
        page.route(re.compile(r"/api/tasks/[^/]+/execution\?"),
                   lambda r: r.fulfill(status=200, content_type="application/json",
                                       body=json.dumps(RUNNING)))
        page.evaluate("({id, proj}) => TeepPlan.openTask(id, proj)", {"id": TASK_ID, "proj": "maxwell"})
        page.wait_for_selector("#task-modal.show", timeout=5000)
        page.wait_for_selector("#task-primary-stop", state="visible", timeout=5000)
        ok(page.locator("#task-primary-stop").is_visible()
           and page.locator("#task-primary-watch-here").is_visible(),
           "a running task offers Stop alongside Watch live")
        ok(page.locator("#task-primary-retry").is_hidden(),
           "a running task hides Retry (nothing to supersede yet)")
        page.locator("#task-primary-stop").click()
        page.wait_for_timeout(400)
        stop_posts = [p for p in posts if p["url"].split("?")[0].endswith("/execution/stop")]
        ok(len(stop_posts) == 1 and stop_posts[0]["body"] == {"project": "maxwell"},
           "Stop POSTs to /execution/stop with only the project — no runner id")
        ok(not any(k in stop_posts[0]["body"] for k in
                   ("runner_session_id", "host_id", "wake_id", "execution_id")),
           "Stop names no execution identity; the server resolves it")

        # ---- ended/failed -> Retry, and Retry POSTs to the command route ----
        page.unroute(re.compile(r"/api/tasks/[^/]+/execution\?"))
        page.route(re.compile(r"/api/tasks/[^/]+/execution\?"),
                   lambda r: r.fulfill(status=200, content_type="application/json",
                                       body=json.dumps(ENDED)))
        page.evaluate("id => TeepPlan._loadTaskPrimaryRunner(id)", TASK_ID)
        page.wait_for_selector("#task-primary-retry", state="visible", timeout=5000)
        ok(page.locator("#task-primary-retry").is_visible(),
           "a task with an ended session offers Retry")
        page.locator("#task-primary-retry").click()
        page.wait_for_timeout(400)
        retry_posts = [p for p in posts if p["url"].split("?")[0].endswith("/execution/retry")]
        ok(len(retry_posts) == 1 and retry_posts[0]["body"] == {"project": "maxwell"},
           "Retry POSTs to /execution/retry with only the project — no runner id")

        ok(not console_errors, f"no console errors during the flow ({console_errors[:3]})")

    print(f"\nUI-58 task controls (Playwright): {passed} passed, {failed} failed")
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(1 if failed else 0)
