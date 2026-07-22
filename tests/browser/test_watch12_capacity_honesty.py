#!/usr/bin/env python3
"""Chromium proves Start renders WATCH-12's immediate capacity readback."""
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]

with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content('<div id="gate"></div><button id="runner-pty-start-retry"></button>')
    page.add_script_tag(path=str(ROOT / "static/js/runner-session.js"))
    result = page.evaluate("""async () => {
        const calls = [];
        const methods = window.SwitchboardRunnerSession.methods;
        const app = {
            esc: (value) => String(value),
            _runnerPtyGate: (html, tone) => {
                document.getElementById('gate').innerHTML = html;
                calls.push({ html, tone });
            },
            openRunnerSessionPanel: async () => true,
        };
        app.startTaskSession = methods.startTaskSession.bind(app);
        window.PM_PROJECT = 'switchboard';
        let request = 0;
        window.fetch = async () => {
            request += 1;
            return {
                json: async () => request === 1 ? {
                    action: 'starting', starting: true,
                    capacity: {
                        pending_ahead: 2,
                        matching_online_hosts: [{
                            host_id: 'host/full', active_sessions: 8,
                            max_sessions: 8, available_sessions: 0,
                        }],
                    },
                } : { running: true, panel: { state: 'live' } },
            };
        };
        const originalTimeout = window.setTimeout;
        window.setTimeout = (fn) => originalTimeout(fn, 0);
        await app.startTaskSession('WATCH-12', {}, false);
        return calls;
    }""")
    browser.close()

assert any("Queued behind 8 runs on host/full" in call["html"]
           and call["tone"] == "warning" for call in result), result
print("PASS Chromium renders the immediate queued-behind-N capacity readback")
