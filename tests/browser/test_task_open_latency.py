#!/usr/bin/env python3
"""Playwright audit: task modal must paint before slow get_task returns."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
STATE_JS = (ROOT / "static" / "js" / "state.js").read_text(encoding="utf-8")


STUBS = """
window.SwitchboardBoard = { methods: {} };
window.SwitchboardPlanChat = { methods: {} };
window.SwitchboardClosure = { methods: {} };
window.SwitchboardMission = { methods: {} };
window.SwitchboardRunnerSession = { methods: {} };
window.SwitchboardProofConsole = { methods: {} };
window.SwitchboardProjectAdmin = { methods: {} };
window.SwitchboardSettings = { methods: {} };
window.bootstrap = { Modal: { getOrCreateInstance(el) {
  return { show() { el.classList.add('show'); } };
}}};
window.PM_PROJECT = 'switchboard';
"""


HTML = f"""<!DOCTYPE html>
<html><body>
<div id="task-modal" class="modal">
  <h1 id="task-modal-title"></h1>
  <div id="task-modal-body"></div>
</div>
<script>{STUBS}</script>
<script src="/state.js"></script>
<script src="/app.js"></script>
<script>window.TeepPlan = TeepPlan;</script>
</body></html>
"""


def main() -> int:
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        def handle_route(route):
            url = route.request.url
            if url.endswith("/state.js"):
                route.fulfill(
                    status=200, content_type="application/javascript", body=STATE_JS)
                return
            if url.endswith("/app.js"):
                route.fulfill(
                    status=200, content_type="application/javascript", body=APP_JS)
                return
            if "/api/tasks/" in url and route.request.method == "GET":
                time.sleep(1.5)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "task_id": "PERF-1",
                        "title": "Fresh from API",
                        "status": "In Progress",
                        "depends_on": [],
                        "activity": [],
                    }),
                )
                return
            if url.rstrip("/").endswith("/audit"):
                route.fulfill(status=200, content_type="text/html", body=HTML)
                return
            route.fulfill(status=404, body="missing")

        page.route("**/*", handle_route)
        page.goto("https://perf.local/audit", wait_until="domcontentloaded")
        if errors:
            print("PAGE_ERRORS", errors[:5], file=sys.stderr)
            browser.close()
            return 1

        page.evaluate("""() => {
            TeepPlan.tasks = [{
                task_id: 'PERF-1', title: 'Cached board card', status: 'Not Started',
                depends_on: [], risk_level: 'Low', _wsId: 'PERF', _wsName: 'Perf'
            }];
            TeepPlan._renderTaskModal = (t) => {
                if (window.__paintMs == null) {
                    window.__paintMs = performance.now() - window.__openStarted;
                    window.__firstTitle = t.title;
                }
                const modal = document.getElementById('task-modal');
                modal.classList.add('show');
                modal.dataset.taskId = t.task_id;
                document.getElementById('task-modal-title').textContent =
                    `${t.task_id} ${t.title}`;
                window.__lastTitle = t.title;
            };
        }""")

        # Do not await openTask — page.evaluate waits for returned promises, which
        # would hide the whole point of measuring paint-before-fetch.
        page.evaluate(
            "() => { window.__openStarted = performance.now(); void TeepPlan.openTask('PERF-1'); }")
        page.wait_for_selector("#task-modal.show", timeout=2000)
        paint_ms = page.evaluate("() => window.__paintMs")
        first_title = page.evaluate("() => window.__firstTitle")
        page.wait_for_function(
            "() => window.__lastTitle === 'Fresh from API'", timeout=5000)
        refreshed = page.evaluate("() => window.__lastTitle")
        browser.close()

    ok_paint = paint_ms is not None and paint_ms < 500
    ok_cached = first_title == "Cached board card"
    ok_refresh = refreshed == "Fresh from API"
    print(json.dumps({
        "test": "task_open_latency",
        "modal_paint_ms": None if paint_ms is None else round(paint_ms, 1),
        "budget_ms": 500,
        "cached_title": first_title,
        "refreshed_title": refreshed,
        "pass": ok_paint and ok_cached and ok_refresh,
    }, indent=2))
    if not (ok_paint and ok_cached and ok_refresh):
        print("FAIL: openTask still blocks paint on get_task or skipped refresh",
              file=sys.stderr)
        return 1
    print("PASS: task modal paints before slow get_task returns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
