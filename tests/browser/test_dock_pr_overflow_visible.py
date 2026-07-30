#!/usr/bin/env python3
"""The PR card's overflow control must be findable, not a sliver on the border.

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
PR = {
    "number": 1121, "title": "SQLite connections accumulate beyond the pool",
    "url": "https://github.com/x/y/pull/1121", "ci_state": "success",
    "mergeable_state": "clean", "queue_position": 0, "blocked": False,
    "head_sha": "abc1234", "updated_at": 0, "additions": 310, "deletions": 46,
    "tasks": [{"task_id": "BUG-246", "title": "SQLite connections"}],
}
QUEUED = {**PR, "number": 1122, "queue_position": 1}

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
            """(pr) => {
                const ctx = Object.create(TeepPlan);
                ctx.tasks = []; ctx._fleetTaskTitle = () => '';
                ctx._dockAutopilotHtml = () => '';
                document.getElementById('fleet-dock').innerHTML =
                    TeepPlan._dockPrHtml.call(ctx, pr);
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
    d = render(1280, PR)
    ov = box(d, ".dock-overflow-btn")
    merge = box(d, "[data-pr-merge]")
    ok(ov and ov["visible"], f"the overflow control renders on desktop: {ov}")
    ok(ov and ov["w"] >= 28 and ov["h"] >= 28,
       f"it is a real target, not a 19x22 sliver: {ov}")
    # It must sit BESIDE the primary action, not exiled to the card's far edge.
    ok(ov and merge and (ov["x"] - (merge["x"] + merge["w"])) < 120,
       f"overflow sits beside the primary action, got merge={merge} overflow={ov}")
    cls = d.evaluate(
        "() => { const e = document.querySelector('.dock-overflow-btn');"
        " return e ? e.className : null; }")
    ok(cls is not None and "btn-ghost-secondary" not in cls,
       f"the control carries a visible border, not a ghost style: {cls!r}")
    # And Close PR is reachable through it.
    ok(box(d, '[data-pr-close="1121"]') is not None,
       "Close PR is present inside the overflow")
    d.close()

    # ── phone ──────────────────────────────────────────────────────────────
    m = render(500, PR)
    ovm = box(m, ".dock-overflow-btn")
    mergem = box(m, "[data-pr-merge]")
    ok(ovm and ovm["w"] >= 44 and ovm["h"] >= 44,
       f"44x44 on a phone: {ovm}")
    # The primary action must not eat the row and squeeze the overflow away.
    ok(mergem and ovm and mergem["w"] + ovm["w"] <= 380,
       f"primary action leaves room for the overflow: merge={mergem} overflow={ovm}")
    m.close()

    # ── a queued PR still offers nothing to close ─────────────────────────
    q = render(1280, QUEUED)
    ok(box(q, ".dock-overflow-btn") is None,
       "a PR the merge queue owns exposes no overflow")
    q.close()
    browser.close()

if failures:
    for line in failures:
        print("FAIL", line)
    raise SystemExit(1)
print("PASS test_dock_pr_overflow_visible")
