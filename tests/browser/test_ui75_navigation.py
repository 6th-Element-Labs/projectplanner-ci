"""UI-75: verify the real navigation markup in desktop and mobile Chromium."""
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto((ROOT / "static" / "index.html").as_uri(), wait_until="domcontentloaded")
    # index.html uses an origin-root URL in production; load the same vendored
    # styles and bundle explicitly for this file:// hermetic test.
    page.add_style_tag(path=str(ROOT / "static" / "vendor" / "tabler" / "css" / "tabler.min.css"))
    page.add_style_tag(path=str(ROOT / "static" / "vendor" / "tabler" / "css" / "tabler-icons.min.css"))
    page.add_style_tag(path=str(ROOT / "static" / "taikun-tabler.css"))
    page.add_style_tag(path=str(ROOT / "static" / "taikun-ui.css"))
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
    assert page.locator(".navbar-vertical").evaluate("el => getComputedStyle(el).display") != "none"
    assert page.locator(".tk-brand-full").evaluate("el => getComputedStyle(el).display") != "none"
    assert page.locator("#toolbar-context").evaluate("el => getComputedStyle(el).display") != "none"

    page.set_viewport_size({"width": 390, "height": 844})
    assert page.locator(".tk-mobile-nav").evaluate("el => getComputedStyle(el).display") == "grid"
    assert page.locator(".tk-mobile-nav > .nav-link").count() == 4
    assert page.locator(".navbar-vertical").evaluate("el => getComputedStyle(el).display") == "none"
    assert page.locator(".tk-brand-full").evaluate("el => getComputedStyle(el).display") == "none"
    assert page.locator(".tk-brand-mobile").evaluate("el => getComputedStyle(el).display") != "none"
    assert page.locator(".tk-toolbar-filter").evaluate("el => getComputedStyle(el).display") == "none"
    assert page.locator(".tk-toolbar-export").evaluate("el => getComputedStyle(el).display") == "none"
    assert page.locator(".tk-toolbar").evaluate(
        "el => Math.round(el.getBoundingClientRect().height) <= 56"
    )
    assert page.locator("#mobile-system-health").count() == 1
    assert page.locator("#mobile-fleet-badge").count() == 1

    # Header actions remain one compact row instead of wrapping under the brand.
    tops = page.locator(".tk-toolbar .navbar-brand, #btn-ack-inbox, #btn-new-task, #user-menu").evaluate_all(
        "els => els.map(el => Math.round(el.getBoundingClientRect().top))"
    )
    assert max(tops) - min(tops) <= 8

    page.evaluate("""() => {
      const host = document.getElementById('fleet-dock');
      host.innerHTML = '<button id="fleet-mobile-activity">Autopilot · 2 working</button>';
    }""")
    activity = page.locator("#fleet-mobile-activity").bounding_box()
    mobile_nav = page.locator(".tk-mobile-nav").bounding_box()
    assert activity["y"] + activity["height"] <= mobile_nav["y"] - 8

    page.locator('[data-tk-mobile-tab="#toptab-mission"]').first.click()
    page.wait_for_timeout(50)
    assert page.locator("#toptab-mission").evaluate("el => el.classList.contains('active')")
    assert page.locator('[data-tk-mobile-tab="#toptab-mission"]').first.evaluate(
        "el => el.classList.contains('active')"
    )

    page.locator(".tk-mobile-more").click()
    page.locator("#mobile-system-health").click()
    assert page.evaluate("location.hash") == "#tab-settings/capacity"

    browser.close()

print("PASS UI-75/UI-77 responsive navigation shell")
