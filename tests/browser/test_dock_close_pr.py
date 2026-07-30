#!/usr/bin/env python3
"""Close PR: the operator can drop a PR they do not want, and it leaves the dock.

GitHub has no delete — close is the operation, and it is reversible. The control
lives in the overflow (it is the one action here that throws work away), is
hidden while the merge queue owns the PR, and the card disappears immediately
rather than lingering until the next poll.
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
OPEN_PR = {"number": 900, "title": "A PR nobody wants", "url": "https://github.com/x/y/pull/900",
           "ci_state": "success", "mergeable_state": "clean", "queue_position": 0,
           "blocked": False, "tasks": [], "head_sha": "abc", "updated_at": 0}
QUEUED_PR = {**OPEN_PR, "number": 901, "queue_position": 1}

failures = []


def ok(condition, message):
    if not condition:
        failures.append(message)


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_content('<body style="margin:0"><div id="fleet-dock"></div></body>')
    for rel in SCRIPTS:
        page.add_script_tag(path=str(ROOT / rel))

    # Record the close call instead of hitting GitHub, and report the PR still open
    # on the first refetch so the suppression bridge is genuinely exercised.
    setup = """(prs) => {
        const ctx = Object.create(TeepPlan);
        ctx._dockCollapsed = false;
        ctx._dockAttention = {};
        ctx._dockTab = 'prs';
        ctx.tasks = [];
        ctx._fleetTaskTitle = () => '';
        window.__calls = [];
        window.__prs = prs;
        window.fetch = async (url, opts) => {
            window.__calls.push({url, body: JSON.parse((opts || {}).body || '{}')});
            return {ok: true, status: 200, json: async () => ({status: 'closed'})};
        };
        window.confirm = (msg) => { window.__confirm = msg; return true; };
        ctx._loadFleetDock = async () => render();
        const render = () => TeepPlan._renderFleetDock.call(
            ctx, [], window.__prs, {production: {last_deploy_ok: true}, deployments: []});
        ctx._renderFleetDock = render;
        window.__ctx = ctx;
        window.__render = render;
        render();
    }"""
    page.evaluate(setup, [OPEN_PR, QUEUED_PR])
    page.wait_for_timeout(200)

    # The control exists for an open PR as a labelled, destructive button beside
    # the primary action. It was an icon-only overflow item until the operator
    # reported it rendering as a blank square; sizing/tone is pinned in detail by
    # tests/browser/test_dock_pr_actions.py.
    btn = page.locator('[data-pr-close="900"]')
    ok(btn.count() == 1, "an open PR offers Close")
    ok("Close" in btn.inner_text(),
       f"the control carries a text label, not an icon: {btn.inner_text()!r}")
    cls = btn.get_attribute("class") or ""
    ok("btn-danger" in cls, f"Close reads as destructive: {cls!r}")
    ok("btn-primary" not in cls,
       "Close must never wear the red brand primary used for the merge action")

    # A queued PR renders in the merge-queue stack, which carries no card actions
    # at all — so there is nothing to close from. The route-level refusal is the
    # real guard and is proved in tests/test_dock_close_pr_route.py, because this
    # harness can never reach it.
    ok(page.locator('[data-pr-close="901"]').count() == 0,
       "a queued PR exposes no Close control")
    ok("Merge queue" in page.locator("#fleet-dock").inner_text(),
       "a queued PR is shown in the merge-queue stack, not as a closable card")

    # Clicking confirms, calls the close route, and the card leaves at once even
    # though the server still reports the PR as open.
    page.click('[data-pr-close="900"]')
    page.wait_for_timeout(300)
    confirm = page.evaluate("() => window.__confirm || ''")
    ok("Close PR #900" in confirm, f"the confirm names the PR: {confirm!r}")
    ok("reopen" in confirm.lower(),
       "the confirm must say closing is reversible, not imply deletion")
    calls = page.evaluate("() => window.__calls")
    ok(len(calls) == 1 and calls[0]["url"].endswith("/api/pull-requests/900/close"),
       f"exactly one call, to the close route: {calls}")
    ok(page.locator('[data-pr-close="900"]').count() == 0,
       "the closed PR leaves the dock immediately, without waiting for a poll")
    ok(page.locator('[data-pr-close="901"]').count() == 0
       and "901" in page.locator("#fleet-dock").inner_text(),
       "the other PR is untouched")

    # Suppression is a bridge, not a hiding place: once GitHub stops reporting it
    # open, the id is forgotten — so a reopened PR shows up again.
    page.evaluate("() => { window.__prs = window.__prs.filter(p => p.number !== 900); window.__render(); }")
    page.wait_for_timeout(150)
    ok(page.evaluate("() => (window.__ctx._dockClosedPrs || []).length") == 0,
       "the suppressed id is forgotten once the server agrees it is closed")
    page.evaluate("() => { window.__prs = [Object.assign({}, window.__prs[0], {number: 900, queue_position: 0}), window.__prs[0]]; window.__render(); }")
    page.wait_for_timeout(150)
    ok(page.locator('[data-pr-close="900"]').count() == 1,
       "a reopened PR is shown again, never permanently masked")

    ok(errors == [], f"no console errors: {errors}")
    browser.close()

board = (ROOT / "src/switchboard/api/routers/board.py").read_text()
ok('"/api/pull-requests/{pr_number}/close"' in board, "the close route exists")
ok('"pr", "close", str(pr_number)' in board, "it closes via the GitHub CLI")
ok('"delete"' not in board.split("close_pull_request")[1].split("async def")[0],
   "closing must not delete anything")
ok("queue_position" in board.split("close_pull_request")[1].split("async def")[0],
   "the route refuses to close a PR the merge queue owns")

if failures:
    for line in failures:
        print("FAIL", line)
    raise SystemExit(1)
print("PASS test_dock_close_pr")
