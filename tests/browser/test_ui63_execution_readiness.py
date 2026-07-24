#!/usr/bin/env python3
"""UI-63 Chromium proof: Atlas red/green and Start repair guidance."""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from path_setup import ROOT  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


RED = {
    "schema": "switchboard.project_execution_readiness.v1",
    "passed": False,
    "status": "blocked",
    "reason_code": "scm_connection_missing",
    "message": "Project execution readiness is blocked.",
    "blockers": [{
        "code": "scm_connection_missing",
        "message": "SCM connection is unavailable.",
        "repair": "Create an SCM installation connection.",
    }],
    "states": {
        "configuration": {"passed": True, "status": "ready", "blockers": []},
        "provider": {"passed": True, "status": "ready", "blockers": []},
        "scm": {"passed": False, "status": "blocked", "blockers": [{
            "code": "scm_connection_missing",
            "message": "SCM connection is unavailable.",
            "repair": "Create an SCM installation connection.",
        }]},
        "persistent": {"passed": False, "status": "blocked", "blockers": [{
            "code": "persistent_capacity_unavailable",
            "message": "No eligible persistent Agent Host currently has capacity.",
            "repair": "Enroll an online persistent host.",
        }]},
        "ephemeral": {"passed": True, "status": "ready", "blockers": []},
        "autopilot": {"passed": True, "status": "disabled", "blockers": []},
    },
}
GREEN = {
    **RED,
    "passed": True,
    "status": "ready",
    "reason_code": "",
    "message": "Project is ready for Start and Autopilot admission.",
    "states": {
        key: {**value, "passed": True, "status": "ready", "blockers": []}
        for key, value in RED["states"].items()
    },
}


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page()
    errors = []
    page.on("console", lambda message: errors.append(message.text)
            if message.type == "error" else None)
    page.set_content('<main id="mount"></main><div id="runner-pty-gate"></div>')
    page.add_script_tag(path=str(ROOT / "static/js/settings.js"))
    page.add_script_tag(path=str(ROOT / "static/js/runner-session.js"))

    def render(payload):
        return page.evaluate(
            """async (payload) => {
                window.PM_PROJECT = 'atlas';
                const methods = window.SwitchboardSettings.methods;
                const ctx = {
                    esc: (value) => String(value ?? '').replaceAll('&', '&amp;')
                        .replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
                    _sfetch: async () => payload,
                    _settingsCard: methods._settingsCard,
                };
                return await methods._settingsExecutionSection.call(ctx);
            }""", payload)

    red_html = render(RED)
    page.locator("#mount").evaluate("(node, html) => node.innerHTML = html", red_html)
    assert page.locator("#execution-readiness-summary").inner_text().startswith("Blocked")
    assert page.locator('[data-readiness-state="scm"] .badge').inner_text() == "blocked"
    assert "Create an SCM installation connection." in page.locator(
        '[data-readiness-state="scm"]').inner_text()

    green_html = render(GREEN)
    page.locator("#mount").evaluate("(node, html) => node.innerHTML = html", green_html)
    assert page.locator("#execution-readiness-summary").inner_text().startswith("Ready")
    assert page.locator('[data-readiness-state="persistent"] .badge').inner_text() == "ready"

    page.evaluate(
        """(payload) => {
            window.fetch = async () => ({
                json: async () => ({
                    error_code: 'start_refused',
                    start_error: payload.reason_code,
                    message: payload.message,
                    execution_readiness: payload,
                }),
            });
            const methods = window.SwitchboardRunnerSession.methods;
            window.__gate = '';
            const ctx = {
                esc: (value) => String(value ?? '').replaceAll('&', '&amp;')
                    .replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
                _runnerPtyGate: (html) => {
                    window.__gate = html;
                    document.getElementById('runner-pty-gate').innerHTML = html;
                },
            };
            return methods.startTaskSession.call(ctx, 'ATLAS-1', {}, false);
        }""", RED)
    gate_html = page.evaluate("window.__gate")
    assert "Open execution readiness" in gate_html, gate_html
    gate = page.locator("#runner-pty-gate").inner_text()
    assert "SCM connection is unavailable." in gate
    assert "Open execution readiness" in gate
    assert errors == [], errors
    browser.close()

print("PASS UI-63 Chromium: Atlas red/green and Start repair guidance")
