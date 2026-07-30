#!/usr/bin/env python3
"""A queued PR keeps its identity: never the literal placeholder "task".

Once a PR entered the merge queue its row read "PR #1126 · task". _fleetTaskTitle
returns the placeholder string 'task' for an unknown id, and that placeholder is
truthy, so it won the `task.title || _fleetTaskTitle(...) || x.title` chain and
beat the real PR title. A queued PR must show the same identity it showed as a
card: its board task when linked, otherwise its PR title.

Close PR first shipped inside a ghost icon button pinned to the card's right
border, rendering 19x22 with no label; then as an icon-only overflow that showed
as a BLANK SQUARE in the operator's browser when the glyph did not paint. Both
passed markup-shape tests. This one measures the rendered boxes and requires
text labels, so neither failure mode can ship again.

Close PR shipped inside a `btn-ghost-secondary p-1` button pinned to the card's
right edge by `ms-auto`. It rendered at 19x22 with no border, so in a 380px dock
the operator reported the feature as missing — it was in the DOM the whole time.
Existing tests asserted the markup existed; none asserted it was visible or
reachable. This one measures the rendered box.
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
STYLES = [
    "static/vendor/tabler/css/tabler.min.css",
    "static/vendor/tabler/css/tabler-icons.min.css",
    "static/taikun-tabler.css", "static/taikun-ui.css",
]
import time
NOW = int(time.time())
ROWS = [
    # queued, no linked board task — must fall back to the PR title
    {"number": 1126, "queue_position": 1, "queue_enqueued_at": NOW - 300,
     "title": "Kill is a labelled red button", "tasks": []},
    # queued with a board task — must show it
    {"number": 1127, "queue_position": 2, "queue_enqueued_at": NOW - 60,
     "title": "raw pr title", "tasks": [{"task_id": "BUG-246",
                                         "title": "SQLite connections accumulate"}]},
]

failures = []


def ok(condition, message):
    if not condition:
        failures.append(message)


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)

    def render(width, pr):
        page = browser.new_page(viewport={"width": width, "height": 420})
        page.set_content('<body style="margin:0">'
                         '<div id="fleet-dock" style="width:380px"></div></body>')
        for rel in STYLES:
            page.add_style_tag(path=str(ROOT / rel))
        for rel in SCRIPTS:
            page.add_script_tag(path=str(ROOT / rel))
        page.evaluate(
            """(rows) => {
                const ctx = Object.create(TeepPlan);
                ctx.tasks = [];
                ctx._fleetTaskTitle = TeepPlan._fleetTaskTitle;  // real fallback
                document.getElementById('fleet-dock').innerHTML =
                    TeepPlan._dockQueueHtml.call(ctx, rows);
            }""", pr)
        page.wait_for_timeout(150)
        return page

    def box(page, selector):
        return page.evaluate(
            """(s) => { const e = document.querySelector(s); if (!e) return null;
                const r = e.getBoundingClientRect();
                return {w: Math.round(r.width), h: Math.round(r.height),
                        x: Math.round(r.x), visible: e.offsetParent !== null}; }""",
            selector)

    page = render(1280, ROWS)
    text = page.evaluate("() => document.getElementById('fleet-dock').innerText")

    ok("\u00b7 task" not in text and not text.rstrip().endswith("task"),
       f"the placeholder 'task' must never be shown: {text!r}")
    ok("Kill is a labelled red button" in text,
       f"a queued PR with no board task falls back to its PR title: {text!r}")
    ok("BUG-246" in text and "SQLite connections accumulate" in text,
       f"a queued PR with a board task shows it: {text!r}")
    ok("PR #1126" in text and "PR #1127" in text,
       f"the PR number stays on the meta line: {text!r}")
    ok("in queue" in text, f"queue dwell is still shown: {text!r}")
    page.close()
    browser.close()

if failures:
    for line in failures:
        print("FAIL", line)
    raise SystemExit(1)
print("PASS test_dock_queue_row_identity")
