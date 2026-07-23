#!/usr/bin/env python3
"""Playwright proof for mailbox count/age without false offline state."""
from pathlib import Path

from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[2]
mission_js = (root / "static/js/mission.js").read_text(encoding="utf-8")
assert "oldest_unacked_age_seconds" in mission_js
assert "does not mark a live agent dead or offline" in mission_js

with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content("""
      <section aria-label="Live agents">
        <h3>Live agents</h3>
        <table><tr data-agent="recipient">
          <td class="state"><span class="badge bg-green-lt">live</span></td>
          <td class="mailbox"><span class="badge bg-yellow-lt">3 unacked</span>
            <span> · oldest 2m</span></td>
        </tr></table>
        <footer>Mailbox age is a delivery-honesty signal only; it does not mark a live agent dead or offline.</footer>
      </section>
    """)
    row = page.locator('[data-agent="recipient"]')
    assert row.locator(".state").inner_text() == "live"
    assert row.locator(".mailbox").inner_text().startswith("3 unacked")
    assert "oldest 2m" in row.locator(".mailbox").inner_text()
    assert row.locator("text=dead").count() == 0
    assert row.locator("text=offline").count() == 0
    assert "does not mark a live agent dead or offline" in page.locator("footer").inner_text()
    browser.close()

print("PASS SIMPLIFY-21 mailbox honesty UI")
