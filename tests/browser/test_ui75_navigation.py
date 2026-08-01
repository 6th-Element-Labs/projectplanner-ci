"""UI-75: verify the real navigation markup in desktop and mobile Chromium."""
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto((ROOT / "static" / "index.html").as_uri(), wait_until="domcontentloaded")
    # index.html uses an origin-root URL in production; load the same vendored
    # bundle explicitly for this file:// hermetic test.
    page.add_script_tag(path=str(ROOT / "static" / "vendor" / "bootstrap" / "bootstrap.bundle.min.js"))

    assert page.locator(".tk-nav-section").all_text_contents() == [
        "Shape",
        "Deliver",
        "Collaborate",
        "Utilities",
    ]
    assert page.locator("#toptab-mission .nav-link-title").text_content() == "Deliverables"
    assert page.locator("#toptab-fleet .nav-link-title").text_content() == "Fleet"
    assert page.locator(".tk-mobile-nav").evaluate("el => getComputedStyle(el).display") == "none"

    page.set_viewport_size({"width": 390, "height": 844})
    assert page.locator(".tk-mobile-nav").evaluate("el => getComputedStyle(el).display") == "grid"
    assert page.locator(".tk-mobile-nav > .nav-link").count() == 4

    page.locator('[data-tk-mobile-tab="#toptab-mission"]').first.click()
    page.wait_for_timeout(50)
    assert page.locator("#toptab-mission").evaluate("el => el.classList.contains('active')")
    assert page.locator('[data-tk-mobile-tab="#toptab-mission"]').first.evaluate(
        "el => el.classList.contains('active')"
    )

    browser.close()

print("PASS UI-75 grouped desktop navigation and delegated mobile tabs")
