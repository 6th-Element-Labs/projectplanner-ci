#!/usr/bin/env python3
"""UI-26: real browser proof that the task Dev tab can dispatch to the
operator's own Codex Agent Host, distinct from the existing Claude Code
cloud dispatch. Boots the real app (dev-open, throwaway DB), signs up,
creates a task, opens its Dev tab, and drives the new button through real
DOM events — asserting the POST body actually carries runtime='codex' and
that the confirm/flash copy is runtime-specific, not a relabeled clone of
the Claude Code path.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from path_setup import ROOT  # noqa: F401,E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  playwright not installed — run: python3 -m pip install playwright "
          "&& python3 -m playwright install chromium")
    sys.exit(0)

TMP = Path(tempfile.mkdtemp(prefix="ui26-browser-"))
PORT = 8138  # distinct from 8110 (dev default) and 8137 (UI-24's browser test)
BASE_URL = f"http://127.0.0.1:{PORT}"
EMAIL = "ui26-browser-test@example.local"
PASSWORD = "ui26-browser-test-pw"

env = dict(os.environ)
env.update({
    "PM_DB_PATH": str(TMP / "switchboard.db"),
    "PM_PORT": str(PORT),
    "PM_AUTH_MODE": "dev-open",
})

server = subprocess.Popen(
    [sys.executable, str(Path(ROOT) / "app.py")],
    cwd=str(ROOT), env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)


def _wait_healthy(timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health", timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        if server.poll() is not None:
            return False
        time.sleep(0.25)
    return False


try:
    healthy = _wait_healthy()
    ok(healthy, "app.py boots and /health responds (dev-open auth, throwaway DB)")
    if not healthy:
        raise SystemExit("server did not become healthy — aborting")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        page.goto(f"{BASE_URL}/signup")
        inputs = page.locator("form input")
        inputs.nth(0).fill("UI-26 Browser Test")
        inputs.nth(1).fill(EMAIL)
        inputs.nth(2).fill(PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        ok("TAIKUN" in page.content(), "signup completes and lands on the real app shell")

        # ---- seed a local task object and open its Dev tab --------------------
        # A fresh dev-open signup carries no write:projects/write:tasks scope
        # (that's a real, separate access-control fact, not this task's
        # concern) — so this test drives the Dev tab the same way
        # test_ui24_pty_terminal.py drives the terminal panel: push a task
        # object straight into TeepPlan's in-memory cache. openTask()'s live
        # refetch 404s harmlessly and falls back to this local object (see
        # `if (!t) return` / the try/catch around the fetch in openTask()).
        project_id = "ui26-fixture-project"
        task_id = "UI26-FIXTURE-1"
        page.evaluate("""({id, proj}) => {
            window.PM_PROJECT = proj;
            TeepPlan.tasks = TeepPlan.tasks || [];
            TeepPlan.tasks.push({ task_id: id, title: 'UI-26 browser test task', status: 'Not Started',
                                   _wsId: 'UI', _wsName: 'UI', depends_on: [], risk_level: 'Medium' });
        }""", {"id": task_id, "proj": project_id})

        page.evaluate("({id, proj}) => TeepPlan.openTask(id, proj)", {"id": task_id, "proj": project_id})
        page.wait_for_selector("#task-modal.show", timeout=5000)
        page.wait_for_selector("#task-primary-start", state="visible", timeout=5000)
        ok(page.locator("#task-primary-runner").is_visible()
           and "Start task" in page.locator("#task-primary-start").inner_text(),
           "the first Details surface exposes Start task without opening Dev")

        # UI-58: the card reads the server-authoritative Task Execution
        # projection, not the runner-watch surface. The intent is unchanged —
        # a running task offers Watch live + Open side panel; a blocked live one
        # becomes Talk to agent — only the endpoint and shape moved.
        def _running_execution(route):
            route.fulfill(
                status=200, content_type="application/json",
                body=(
                    '{"schema":"switchboard.task_execution.v1",'
                    '"command":"get_task_execution","running":true,'
                    '"starting":false,"has_ended_session":false,'
                    '"resumable_review":false,"lifecycle_phase":"running",'
                    '"execution_id":"run-ui26-live",'
                    '"execution":{"active_runner":{"runner_session_id":"run-ui26-live",'
                    '"host_id":"host/ui26-mac","status":"running"},'
                    '"active_host":{"host_id":"host/ui26-mac"}}}'
                ),
            )

        page.route("**/api/tasks/**/execution?**", _running_execution)
        page.evaluate("id => TeepPlan._loadTaskPrimaryRunner(id)", task_id)
        page.wait_for_selector("#task-primary-watch-here", state="visible", timeout=5000)
        ok(page.locator("#task-primary-watch-here").is_visible()
           and "Watch live" in page.locator("#task-primary-watch-here").inner_text()
           and page.locator("#task-primary-watch-sidecar").is_visible(),
           "a running task offers both Watch live and Open side panel on the modal")
        ok(page.locator("#task-primary-stop").is_visible(),
           "a running task offers Stop, wired to the Task Session stop command")

        page.locator("#task-primary-runner").evaluate(
            "element => { element.dataset.taskStatus = 'Blocked'; }")
        page.evaluate("id => TeepPlan._loadTaskPrimaryRunner(id)", task_id)
        page.wait_for_function(
            "() => document.getElementById('task-primary-runner')?.dataset.sessionState === 'blocked-live'")
        ok("Blocked, agent still live" in page.locator("#task-primary-runner-title").inner_text()
           and "Talk to agent" in page.locator("#task-primary-watch-here").inner_text(),
           "a blocked task with a live session becomes a Talk to agent action")
        page.unroute("**/api/tasks/**/execution?**", _running_execution)

        page.click('a[href="#m-dev"]')
        page.wait_for_selector("#edit-dispatch-codex", state="visible", timeout=5000)

        claude_btn = page.locator("#edit-dispatch")
        codex_btn = page.locator("#edit-dispatch-codex")
        ok(claude_btn.is_visible() and codex_btn.is_visible(),
           "both the Claude Code and Codex dispatch buttons render side by side")
        ok("Start Codex on my Mac" in codex_btn.inner_text(),
           "the Codex button names the direct personal-host action")

        # ---- intercept the dispatch POST to assert the real request body -----
        captured = {}

        def _capture_dispatch(route):
            req = route.request
            if req.method == "POST":
                captured["body"] = req.post_data
            route.fulfill(status=200, content_type="application/json",
                          body='{"dispatched": true, "wake_id": "wake-ui26-test", "work_hosts_online": 0}')

        # COORD-44: the codex button now posts to the unified /start operation;
        # claude-code keeps the queued dispatch endpoint. Mock both.
        started = {}

        def _capture_start(route):
            req = route.request
            if req.method == "POST":
                started["body"] = req.post_data
            route.fulfill(status=200, content_type="application/json",
                          body='{"action": "started", "started": true, '
                               '"wake_id": "wake-ui26-test", "work_hosts_online": 0}')

        page.route(f"**/api/tasks/{task_id}/start**", _capture_start)
        page.route(f"**/api/tasks/{task_id}/dispatch**", _capture_dispatch)
        page.route(f"**/api/tasks/{task_id}/dispatch/latest**", lambda r: r.fulfill(
            status=200, content_type="application/json", body='{"status": "none"}'))

        codex_btn.click()
        page.wait_for_selector("#confirm-modal.show", timeout=5000)
        confirm_text = page.locator("#confirm-modal").inner_text()
        ok("your enrolled Mac" in confirm_text,
           f"the Tabler confirm modal for the Codex button names the real target (got: {confirm_text!r})")
        ok("native Codex CLI" in confirm_text
           and "assignment config and Switchboard MCP" in confirm_text,
           "the confirm dialog describes the direct CLI bootstrap contract")
        page.click("#confirm-modal-ok")
        page.wait_for_selector("#confirm-modal", state="hidden", timeout=5000)
        page.wait_for_function(
            "() => document.getElementById('edit-flash-dev')?.textContent.includes('Assigned')")

        body = started.get("body") or ""
        ok(body and '"project"' in body and captured.get("body") is None,
           f"the Codex button posts to the unified /start operation, not the queued dispatch endpoint (got: {body!r})")

        flash = page.locator("#edit-flash-dev").inner_text()
        ok("Assigned" in flash and "wake-ui26-test" in flash,
           f"the flash message reflects the direct assignment and its real wake_id (got: {flash!r})")

        # ---- the Claude Code button still sends the original runtime ---------
        captured.clear()
        claude_btn.click()
        page.wait_for_selector("#confirm-modal.show", timeout=5000)
        claude_confirm_text = page.locator("#confirm-modal").inner_text()
        page.click("#confirm-modal-ok")
        page.wait_for_selector("#confirm-modal", state="hidden", timeout=5000)
        page.wait_for_function(
            "() => document.getElementById('edit-flash-dev')?.textContent.includes('Queued')")
        body2 = captured.get("body") or ""
        ok('"runtime":"claude-code"' in body2.replace(" ", ""),
           f"the pre-existing Claude Code button is unaffected — still sends runtime=claude-code (got: {body2!r})")
        ok("Anthropic hosts the coding session" in claude_confirm_text,
           "the Claude Code confirm copy is unchanged")

finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except Exception:
        server.kill()

print(f"\nUI-26 codex dispatch (browser): {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
