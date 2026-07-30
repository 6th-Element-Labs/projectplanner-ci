#!/usr/bin/env python3
"""The PR card's actions must be two labelled, equal-size buttons.

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
    merge = box(d, "[data-pr-merge]")
    close = box(d, "[data-pr-close]")
    ok(merge and merge["visible"], f"Merge renders: {merge}")
    ok(close and close["visible"], f"Close renders: {close}")
    ok(merge and close and merge["w"] == close["w"] and merge["h"] == close["h"],
       f"the two actions are the same size: merge={merge} close={close}")
    ok(merge and merge["w"] >= 60 and merge["h"] >= 24,
       f"they are real targets, not slivers: {merge}")

    labels = d.evaluate(
        """() => {
            const t = s => { const e = document.querySelector(s);
                return e ? e.textContent.trim() : null; };
            return {merge: t('[data-pr-merge]'), close: t('[data-pr-close]')};
        }""")
    ok(labels["merge"] in ("Merge", "Enqueue"), f"Merge is labelled: {labels}")
    ok(labels["close"] == "Close", f"Close is labelled, not icon-only: {labels}")

    tone = d.evaluate(
        """() => {
            const c = s => document.querySelector(s).className;
            return {merge: c('[data-pr-merge]'), close: c('[data-pr-close]')};
        }""")
    ok("btn-success" in tone["merge"] or "btn-azure" in tone["merge"],
       f"Merge is green/blue, never the red brand primary: {tone['merge']!r}")
    ok("btn-danger" in tone["close"],
       f"Close is red — it is the destructive action: {tone['close']!r}")
    ok("btn-primary" not in tone["merge"],
       "Merge must not use the red brand primary beside a red Close")
    d.close()

    # ── phone ──────────────────────────────────────────────────────────────
    m = render(500, PR)
    mm, mc = box(m, "[data-pr-merge]"), box(m, "[data-pr-close]")
    ok(mm and mc and mm["w"] == mc["w"], f"equal width on a phone: {mm} {mc}")
    ok(mm and mm["h"] >= 44 and mc["h"] >= 44, f"44px tall on a phone: {mm} {mc}")
    ok(mm and mc and (mm["w"] + mc["w"]) <= 380, "both fit the dock row")
    m.close()

    # ── a queued PR offers neither ────────────────────────────────────────
    q = render(1280, QUEUED)
    ok(box(q, "[data-pr-close]") is None,
       "a PR the merge queue owns exposes no Close")
    ok(box(q, "[data-pr-merge]") is None,
       "a queued PR exposes no Merge either")
    q.close()
    browser.close()

if failures:
    for line in failures:
        print("FAIL", line)
    raise SystemExit(1)
print("PASS test_dock_pr_actions")
