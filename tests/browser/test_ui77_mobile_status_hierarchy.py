#!/usr/bin/env python3
"""UI-77: mobile pressure state never competes with bottom navigation."""
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]


def load_status(page, payload):
    page.set_content("""
      <style>
        :root{--tk-header-h:3.5rem}
        .tk-pressure-banner{position:fixed;top:var(--tk-header-h);left:0;right:0}
        .d-none{display:none!important}
      </style>
      <header class="tk-toolbar"></header>
      <a id="mobile-system-health"><i id="mobile-system-health-icon"></i>
        <span id="mobile-system-health-badge" class="d-none"></span></a>
      <div id="saturation-dock"></div>
    """)
    page.evaluate("p => { window.fetch = async () => ({ok:true,json:async()=>p}); }", payload)
    page.add_script_tag(path=str(ROOT / "static" / "saturation-panel.js"))
    page.wait_for_timeout(100)


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)

    warning = browser.new_page(viewport={"width": 390, "height": 844})
    load_status(warning, {
        "status": "warning", "alert_count": 2,
        "alerts": [{"severity": "warning", "message": "Queue age is elevated."}],
    })
    assert warning.locator("#saturation-dock").inner_text() == ""
    assert warning.locator("#mobile-system-health-badge").inner_text() == "2"
    assert "d-none" not in (warning.locator("#mobile-system-health-badge").get_attribute("class") or "")
    warning.close()

    critical = browser.new_page(viewport={"width": 390, "height": 844})
    load_status(critical, {
        "status": "critical", "alert_count": 1,
        "alerts": [{"severity": "critical", "message": "Load shedding is active."}],
    })
    assert critical.locator("#saturation-critical-banner").count() == 1
    assert critical.locator("#saturation-critical-banner").bounding_box()["y"] == 56
    assert "Load shedding is active" in critical.locator("#saturation-critical-banner").inner_text()
    critical.close()

    browser.close()

print("PASS UI-77 mobile system-health hierarchy")
