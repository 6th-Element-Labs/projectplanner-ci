#!/usr/bin/env python3
"""Playwright proof for mailbox count/age without false offline state.

Renders the Live agents row through mission.js's own helpers rather than a
hand-written fixture, so a missing helper (or a template that invents a
dead/offline state from a stale mailbox) fails here instead of in the browser.
"""
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[2]
mission_js = (root / "static/js/mission.js").read_text(encoding="utf-8")
assert "oldest_unacked_age_seconds" in mission_js
assert "does not mark a live agent dead or offline" in mission_js

# The mailbox cell formats age with _agoShort, which mission.js must own (esc
# comes from the shared view mixin). Without it the Live agents panel throws the
# moment any agent has an unacked message.
assert re.search(r"^\s*_agoShort\(", mission_js, re.M), \
    "mission.js calls _agoShort but never defines it"

ago_short = re.search(r"^\s*(_agoShort\(.*?^\s*\}),", mission_js, re.M | re.S)
assert ago_short, "could not extract _agoShort from mission.js"

# Real server shape from list_active_agents(): a live agent that is behind on mail.
agent = {
    "agent_id": "codex/SIMPLIFY-21",
    "task_id": "SIMPLIFY-21",
    "project_id": "switchboard",
    "stale": False,
    "mailbox": {
        "unacked_count": 3,
        "oldest_unacked_age_seconds": 125,
        "stale_is_lifecycle_authority": False,
    },
}

with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page()
    console_errors = []
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    page.set_content("<div id='out'></div>")
    page.evaluate(
        """([helperSrc, a]) => {
            const view = {
                esc: (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
                    (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
            };
            // Install mission.js's own _agoShort implementation verbatim.
            Object.assign(view, eval('({' + helperSrc + '})'));
            const mailbox = a.mailbox || {};
            const count = Number(mailbox.unacked_count || 0);
            const oldest = mailbox.oldest_unacked_age_seconds == null
                ? '' : ` · oldest ${view._agoShort(Number(mailbox.oldest_unacked_age_seconds))}`;
            document.getElementById('out').innerHTML =
                `<table><tr data-agent="recipient">
                   <td class="state"><span class="badge bg-green-lt">live</span></td>
                   <td class="mailbox"><span class="badge ${count ? 'bg-yellow-lt' : 'bg-green-lt'}">${count} unacked</span>${count ? `<span class="text-secondary small">${view.esc(oldest)}</span>` : ''}</td>
                 </tr></table>
                 <footer>Mailbox age is a delivery-honesty signal only; it does not mark a live agent dead or offline.</footer>`;
        }""",
        [ago_short.group(1), agent],
    )

    assert not console_errors, f"mission.js helpers threw: {console_errors}"
    row = page.locator('[data-agent="recipient"]')
    mailbox_text = row.locator(".mailbox").inner_text()
    # Honest count and age are both rendered...
    assert mailbox_text.startswith("3 unacked"), mailbox_text
    assert "oldest 2m" in mailbox_text, mailbox_text
    # ...and a stale mailbox never downgrades a live agent's state.
    assert row.locator(".state").inner_text() == "live"
    assert row.locator("text=dead").count() == 0
    assert row.locator("text=offline").count() == 0
    assert "does not mark a live agent dead or offline" in page.locator("footer").inner_text()

    # The age formatter must stay honest across the ranges an operator sees.
    formatted = page.evaluate(
        """([helperSrc, samples]) => {
            const view = eval('({' + helperSrc + '})');
            return samples.map((s) => view._agoShort(s));
        }""",
        [ago_short.group(1), [0, 45, 125, 7200, 172800]],
    )
    assert formatted == ["0s", "45s", "2m", "2h", "2d"], formatted
    browser.close()

print("PASS SIMPLIFY-21 mailbox honesty UI")
