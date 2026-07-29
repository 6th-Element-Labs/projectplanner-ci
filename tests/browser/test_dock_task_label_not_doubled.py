#!/usr/bin/env python3
"""A dock card must not print the task id twice ("QA-12 · QA-12").

Cards render "<task id> · <title>", and _fleetTaskTitle falls back to returning
the task id when the board title has not loaded. The two combined printed the id
back at the operator twice.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from path_setup import ROOT  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SCRIPTS = [
    "static/js/api.js", "static/js/state.js", "static/js/board.js",
    "static/js/plan-chat.js", "static/js/mission.js", "static/js/runner-session.js",
    "static/js/proof-console.js", "static/js/project-admin.js", "static/js/settings.js",
    "static/js/scope.js", "static/js/attention.js", "static/js/fleet-dock.js",
    "static/app.js",
]

failures = []


def ok(condition, message):
    if not condition:
        failures.append(message)


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_content('<div id="m"></div>')
    for rel in SCRIPTS:
        page.add_script_tag(path=str(ROOT / rel))

    out = page.evaluate(
        """() => {
            const ctx = Object.create(TeepPlan);
            ctx.tasks = [{task_id: 'QA-13', title: 'Real title here'}];
            ctx.missionStatus = null;
            const label = (id) => {
                const s = {runner_session_id: 'r' + id, task_id: id, status: 'running',
                           live: true, expires_at: 9e12, stale: false,
                           environment: {uptime_seconds: 3}};
                const d = document.createElement('div');
                d.innerHTML = TeepPlan._dockRunnerHtml.call(ctx, s);
                return d.querySelector('span.fw-medium').textContent.trim();
            };
            return {
                no_title: label('QA-12'),
                with_title: label('QA-13'),
                helper_same: TeepPlan._dockTaskLabel.call(ctx, 'QA-12'),
                helper_real: TeepPlan._dockTaskLabel.call(ctx, 'QA-13'),
            };
        }""")

ok(out["no_title"] == "QA-12",
   f"an id with no board title prints once, got: {out['no_title']!r}")
ok(out["with_title"] == "QA-13 · Real title here",
   f"a real title is still shown, got: {out['with_title']!r}")
ok(out["helper_same"] == "",
   "a title identical to the id contributes nothing")
ok(out["helper_real"] == "Real title here",
   "a distinct title is returned unchanged")
ok(errors == [], f"no console errors: {errors}")

app = (ROOT / "static/app.js").read_text()
ok("_dockTaskLabel" in app, "cards must go through the de-duplicating helper")
ok("${this.esc(taskId)} · ${this.esc(title)}" not in app,
   "the runner card must not unconditionally concatenate id and title")

if failures:
    for line in failures:
        print("FAIL", line)
    raise SystemExit(1)
print("PASS test_dock_task_label_not_doubled")
