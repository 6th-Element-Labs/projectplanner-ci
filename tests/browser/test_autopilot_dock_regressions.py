#!/usr/bin/env python3
"""PR #1022 Chromium proof: Watch/Kill/deploy-history actually render, not just source strings."""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from path_setup import ROOT  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


RUNNER_HEALTHY = {
    "runner_session_id": "rs-1",
    "task_id": "COORD-1",
    "condition": {"key": "working"},
    "available_actions": ["kill"],
}
RUNNER_SILENT = {
    "runner_session_id": "rs-2",
    "task_id": "COORD-2",
    "condition": {"key": "silent"},
    "available_actions": ["inject", "kill"],
}
# merged_at is epoch seconds: deployment_status.deployments_payload normalises
# GitHub's ISO mergedAt through open_prs._parse_github_ts before it reaches the UI.
DEPLOYMENTS = [
    {
        "number": 900, "deployed": True, "status": "deployed",
        "deploy_task_id": "COORD-9", "deploy_task_status": "Done",
        "title": "Shipped one",
        "url": "https://github.com/example/repo/pull/900",
        "merged_at": 1785200000, "merge_sha": "abc1234def",
    },
    {
        "number": 901, "deployed": False, "status": "undeployed",
        "deploy_task_id": "COORD-10", "title": "Not yet",
        "url": "https://github.com/example/repo/pull/901",
        "merged_at": 1785210000, "merge_sha": "27e70797fbaeff9",
    },
]


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page()
    errors = []
    page.on("console", lambda message: errors.append(message.text)
            if message.type == "error" else None)
    page.set_content('<main id="mount"></main>')
    for rel in (
        "static/js/api.js",
        "static/js/state.js",
        "static/js/board.js",
        "static/js/plan-chat.js",
        "static/js/mission.js",
        "static/js/runner-session.js",
        "static/js/proof-console.js",
        "static/js/project-admin.js",
        "static/js/settings.js",
        "static/js/scope.js",
        "static/js/attention.js",
        "static/js/fleet-dock.js",
        "static/app.js",
    ):
        page.add_script_tag(path=str(ROOT / rel))

    def render_runner_actions(runner):
        return page.evaluate(
            """(runner) => {
                const ctx = {
                    esc: (v) => String(v ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
                };
                return TeepPlan._dockRunnerActions.call(ctx, runner, runner.condition, false);
            }""", runner)

    healthy_html = render_runner_actions(RUNNER_HEALTHY)
    page.locator("#mount").evaluate("(node, html) => node.innerHTML = html", healthy_html)
    assert page.locator('[data-runner-watch-task="COORD-1"]').count() == 1, healthy_html
    assert "Watch" in page.locator('[data-runner-watch-task="COORD-1"]').inner_text()
    assert page.locator(".dropdown-item.text-danger").count() == 1, "Kill missing from overflow"
    assert "Kill" in page.locator(".dropdown-item.text-danger").inner_text()

    silent_html = render_runner_actions(RUNNER_SILENT)
    page.locator("#mount").evaluate("(node, html) => node.innerHTML = html", silent_html)
    nudge = page.locator('[data-runner-task="COORD-2"][data-runner-action="inject"]')
    assert nudge.count() == 1, silent_html
    assert "Nudge" in nudge.inner_text()
    assert page.locator('[data-runner-watch-task="COORD-2"]').count() == 1, "Watch dropped on silent runner"

    def render_deploy_body(deployments):
        return page.evaluate(
            """(deployments) => {
                const ctx = Object.create(TeepPlan);
                ctx._dockDeploymentBanner = () => '<div id="banner"></div>';
                const manifest = deployments.filter((x) => !x.deployed);
                const shipped = deployments.filter((x) => x.deployed);
                return ctx._dockDeploymentBanner({})
                    + (manifest.length ? `<div class="p-2"><div class="dock-bucket-label">Not yet deployed · ${manifest.length}</div>${manifest.map((x) => TeepPlan._dockDeploymentHtml.call(ctx, x)).join('')}</div>` : '<div class="p-3 text-secondary small">Nothing waiting to ship.</div>')
                    + (shipped.length ? `<div class="p-2"><div class="dock-bucket-label">Recently shipped · ${shipped.length}</div>${shipped.map((x) => TeepPlan._dockDeploymentHtml.call(ctx, x)).join('')}</div>` : '');
            }""", deployments)

    deploy_html = render_deploy_body(DEPLOYMENTS)
    page.locator("#mount").evaluate("(node, html) => node.innerHTML = html", deploy_html)
    shown = page.locator("#mount").inner_text()
    # Nothing in the dock may be collapsed: the operator reads this surface at a
    # glance, and a disclosure hid what was shipping behind a click.
    assert page.locator("details").count() == 0, deploy_html
    assert "Not yet deployed" in shown
    assert "Recently shipped" in shown
    # Both buckets must speak the same language — PR number, merge SHA and age
    # in every row, so what is shipping is never a mystery.
    assert "COORD-9" in shown, shown
    assert "abc1234" in shown, "shipped row lost its merge SHA"
    assert "#901" in shown, "un-deployed row lost its PR number"
    assert "#900" in shown, "shipped row lost its PR number"
    assert "NaN" not in shown, "merged_at is epoch seconds; a bad type shows NaN"

    # The Runners tab must not hide healthy runners behind a disclosure either.
    source = (ROOT / "static/app.js").read_text()
    assert "dock-healthy" not in source, "healthy runners are collapsed again"
    assert "Working normally ·" in source

    assert errors == [], errors
    browser.close()

print("PASS autopilot-dock-regressions: Watch/Kill/deploy-history render in a real DOM")
