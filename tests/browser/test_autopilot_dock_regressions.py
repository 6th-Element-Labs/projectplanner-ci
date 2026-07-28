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
DEPLOYMENTS = [
    {
        "number": 900, "deployed": True, "status": "deployed",
        "deploy_task_id": "COORD-9", "title": "Shipped one",
        "url": "https://github.com/example/repo/pull/900",
        "merged_at": "2026-07-27T10:00:00Z", "merge_sha": "abc1234def",
        "tasks": [{"task_id": "COORD-9", "title": "Shipped one"}],
    },
    {
        "number": 901, "deployed": False, "status": "undeployed",
        "title": "Not yet", "tasks": [{"task_id": "COORD-10", "title": "Not yet"}],
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
                    + (manifest.length ? `<div class="p-2"><div class="dock-bucket-label">Not yet deployed · ${manifest.length}</div>${manifest.map((x) => {
                        const task = (x.tasks || [])[0] || {};
                        return `<div class="border rounded p-2 mb-1"><strong>${ctx.esc(task.task_id || `PR #${x.number}`)}</strong> · ${ctx.esc(task.title || x.title)}</div>`;
                    }).join('')}</div>` : '<div class="p-3 text-secondary small">Nothing waiting to ship.</div>')
                    + (shipped.length ? `<details class="px-2 pb-2"><summary class="text-secondary small py-1" style="cursor:pointer;">Recently shipped · ${shipped.length}</summary>${shipped.map((x) => TeepPlan._dockDeploymentHtml.call(ctx, x)).join('')}</details>` : '');
            }""", deployments)

    deploy_html = render_deploy_body(DEPLOYMENTS)
    page.locator("#mount").evaluate("(node, html) => node.innerHTML = html", deploy_html)
    assert "Not yet deployed" in page.locator("#mount").inner_text()
    details = page.locator("details")
    assert details.count() == 1, deploy_html
    assert "Recently shipped" in details.locator("summary").inner_text()
    details.evaluate("(node) => node.open = true")
    assert "COORD-9" in details.inner_text()

    assert errors == [], errors
    browser.close()

print("PASS autopilot-dock-regressions: Watch/Kill/deploy-history render in a real DOM")
