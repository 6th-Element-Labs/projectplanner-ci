#!/usr/bin/env python3
"""The dock must report the same attention total minimized and expanded.

The pill led with a single category ("2 broken") while the open dock counted
every attention item ("4 blocked"). Minimized — the state the operator leaves it
in — showed the smaller number, so two blocked PRs were invisible.
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

# Two dead runners and two blocked PRs: four things need the operator.
RUNNERS = [
    {"runner_session_id": "r1", "task_id": "A-1", "status": "exited", "stale": False},
    {"runner_session_id": "r2", "task_id": "A-2", "status": "exited", "stale": False},
    {"runner_session_id": "r3", "task_id": "A-3", "status": "running", "stale": False},
]
PRS = [
    {"number": 1, "blocked": True, "title": "one", "tasks": []},
    {"number": 2, "blocked": True, "title": "two", "tasks": []},
    {"number": 3, "blocked": False, "title": "three", "tasks": []},
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
    page.set_content('<div id="fleet-dock"></div>')
    for rel in SCRIPTS:
        page.add_script_tag(path=str(ROOT / rel))

    def render(collapsed):
        return page.evaluate(
            """([runners, prs, collapsed]) => {
                const host = document.getElementById('fleet-dock');
                const ctx = Object.create(TeepPlan);
                ctx._dockCollapsed = collapsed;
                ctx._dockAttention = {};
                ctx._fleetTaskTitle = () => '';
                ctx._renderFleetDock = () => {};
                document.getElementById('fleet-dock').innerHTML = '';
                TeepPlan._renderFleetDock.call(ctx, runners, prs,
                    {production: {last_deploy_ok: true}, deployments: []});
                return host.innerText;
            }""", [RUNNERS, PRS, collapsed])

    collapsed_text = render(True)
    expanded_text = render(False)

    ok("4 need you" in collapsed_text,
       f"collapsed pill must report all 4 attention items, got: {collapsed_text!r}")
    ok("4 need you" in expanded_text,
       f"expanded header must report the same 4, got: {expanded_text!r}")
    ok("2 broken" not in collapsed_text.split("need you")[0],
       "the pill must not lead with a single category count")
    # The mix is still spelled out, just not as the headline number.
    ok("2 broken" in collapsed_text and "2 PRs blocked" in collapsed_text,
       f"collapsed pill must break down the mix, got: {collapsed_text!r}")
    ok("blocked" not in expanded_text.split("need you")[0],
       "the old 'N blocked' headline must be gone from the open dock")
    ok(errors == [], f"no console errors: {errors}")
    browser.close()

if failures:
    for line in failures:
        print("FAIL", line)
    raise SystemExit(1)
print("PASS test_dock_attention_parity")
