#!/usr/bin/env python3
"""Runner-card actions must be labelled, equal-size buttons — Kill included.

Kill lived behind an icon-only dropdown trigger (`btn-ghost-secondary p-1` +
`ti-dots-vertical`) pinned to the card edge by `ms-auto`. It rendered as a blank
square when the glyph did not paint, so the operator reported Kill as missing —
the same defect that had just hidden Close on PR cards. No dock action may be
icon-only or hidden in an overflow.

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
RUNNER = {
    "runner_session_id": "a", "task_id": "QA-27", "status": "running", "live": True,
    "expires_at": NOW + 180, "updated_at": NOW, "stale": False,
    "environment": {"uptime_seconds": 180, "last_output_at": NOW - 10},
    "available_actions": ["kill"],
}
NO_KILL = {**RUNNER, "task_id": "QA-28", "available_actions": []}

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
            """(runner) => {
                const ctx = Object.create(TeepPlan);
                ctx.tasks = []; ctx._dockAttention = {};
                ctx._fleetTaskTitle = () => '';
                document.getElementById('fleet-dock').innerHTML =
                    TeepPlan._dockRunnerHtml.call(ctx, runner);
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

    # ── desktop ────────────────────────────────────────────────────────────
    d = render(1280, RUNNER)
    watch = box(d, "[data-runner-watch-task]")
    kill = box(d, '[data-runner-action="kill"]')
    ok(watch and watch["visible"], f"Watch renders: {watch}")
    ok(kill and kill["visible"], f"Kill renders as its own button: {kill}")
    ok(watch and kill and watch["w"] == kill["w"] and watch["h"] == kill["h"],
       f"Watch and Kill are the same size: watch={watch} kill={kill}")

    labels = d.evaluate(
        """() => {
            const t = s => { const e = document.querySelector(s);
                return e ? e.textContent.trim() : null; };
            return {watch: t('[data-runner-watch-task]'),
                    kill: t('[data-runner-action="kill"]')};
        }""")
    ok(labels["watch"] == "Watch", f"Watch is labelled: {labels}")
    ok(labels["kill"] == "Kill", f"Kill is labelled, not an icon: {labels}")

    tone = d.evaluate(
        """() => document.querySelector('[data-runner-action="kill"]').className""")
    ok("btn-danger" in tone, f"Kill reads as destructive: {tone!r}")
    ok("dropdown-item" not in tone, f"Kill is not buried in an overflow: {tone!r}")

    # No dock action may be an icon-only ghost — the blank-square failure mode.
    ghosts = d.evaluate(
        "() => document.querySelectorAll('#fleet-dock .btn-ghost-secondary').length")
    ok(ghosts == 0, f"no icon-only ghost controls on a runner card: {ghosts}")
    ok(d.evaluate("() => document.querySelectorAll('#fleet-dock .dropdown').length") == 0,
       "runner cards carry no dropdowns")
    d.close()

    # ── phone ──────────────────────────────────────────────────────────────
    m = render(500, RUNNER)
    mw, mk = box(m, "[data-runner-watch-task]"), box(m, '[data-runner-action="kill"]')
    ok(mw and mk and mw["w"] == mk["w"], f"equal width on a phone: {mw} {mk}")
    ok(mw and mw["h"] >= 44 and mk["h"] >= 44, f"44px tall on a phone: {mw} {mk}")
    ok(mw and mk and (mw["w"] + mk["w"]) <= 380,
       "Watch must not crowd Kill off the row")
    m.close()

    # ── a runner the host will not let us stop offers no Kill ─────────────
    n = render(1280, NO_KILL)
    ok(box(n, '[data-runner-action="kill"]') is None,
       "Kill is only offered when the host allows it")
    ok(box(n, "[data-runner-watch-task]") is not None,
       "Watch is still offered for any bound runner")
    n.close()
    browser.close()

if failures:
    for line in failures:
        print("FAIL", line)
    raise SystemExit(1)
print("PASS test_dock_runner_actions")
