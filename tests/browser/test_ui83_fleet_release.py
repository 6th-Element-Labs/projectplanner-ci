"""UI-83: Fleet release truth is clean and responsive in real Chromium."""
from pathlib import Path
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import threading

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
SERVER = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(ROOT / "static")))
threading.Thread(target=SERVER.serve_forever, daemon=True).start()


def load(page):
    page.route("**/api/projects**", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"projects":[{"id":"switchboard","label":"Switchboard"}]}'))
    page.route("**/api/deliverables**", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"deliverables":[]}'))
    page.goto(f"http://127.0.0.1:{SERVER.server_port}/index.html?project=switchboard#tab-fleet",
              wait_until="domcontentloaded")
    page.wait_for_selector("#tab-fleet")
    page.evaluate("""() => {
      TeepPlan.esc = (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;');
      TeepPlan.project = 'switchboard';
      TeepPlan.isAdmin = false;
      TeepPlan._fleetRelease = {
        version: '0.4.21', bundle_digest: 'sha256:promoted', archive_present: true,
        download_url: '/ixp/v1/host_releases/hostrel-0421/bundle?project=switchboard'
      };
      TeepPlan._fleetHosts = [
        {host_id:'host/steve-mbp-co16', hostname:"Steve's MacBook Pro", stale:false,
         heartbeat_at:Date.now()/1000-12, agent_host_version:'0.4.20', bundle_digest:'sha256:old',
         contract_fingerprint:'eac1:match', runtimes:[{runtime:'codex'}],
         limits:{max_sessions:4}, capacity:{active_sessions:1},
         readiness:{state:'update_available', installed_version:'0.4.20',
           installed_digest:'sha256:old', required_version:'0.4.21',
           required_digest:'sha256:promoted', contract_matches:true, actionable:true,
           detail:'Installed 0.4.20; 0.4.21 is promoted.'}},
        {host_id:'host/offline', hostname:'Studio Mac', stale:true,
         heartbeat_at:Date.now()/1000-3600, agent_host_version:'0.4.21',
         readiness:{state:'offline', installed_version:'0.4.21', required_version:'0.4.21',
           contract_matches:true, detail:'No heartbeat inside the lease TTL.'}}
      ];
      TeepPlan._fleetFilter = 'all';
      TeepPlan._renderFleetHosts();
    }""")


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    desktop = browser.new_page(viewport={"width": 1440, "height": 900})
    load(desktop)
    assert desktop.locator("#tab-fleet").count() == 1
    assert desktop.locator(".tk-fleet-host-card").count() == 2
    assert "0.4.20" in desktop.locator(".tk-fleet-host-card").first.inner_text()
    assert "0.4.21" in desktop.locator(".tk-fleet-host-card").first.inner_text()
    assert "Digest differs" in desktop.locator(".tk-fleet-host-card").first.inner_text()
    assert desktop.locator("#fleet-update-banner").is_visible()
    assert desktop.locator("#fleet-download-host").is_enabled()
    desktop.evaluate("""() => {
      TeepPlan._fleetHosts[0].readiness.state = 'update_failed';
      TeepPlan._fleetHosts[0].update_error = '<img src=x onerror=window.fleetXss=true>';
      TeepPlan._renderFleetHosts();
    }""")
    assert "Update failed" in desktop.locator(".tk-fleet-host-card").first.inner_text()
    assert desktop.locator("#fleet-update-banner img").count() == 0
    assert desktop.evaluate("window.fleetXss !== true")
    (ROOT / ".artifacts").mkdir(exist_ok=True)
    desktop.screenshot(path=str(ROOT / ".artifacts" / "ui83-fleet-desktop.png"), full_page=True)

    mobile = browser.new_page(viewport={"width": 390, "height": 844})
    load(mobile)
    mobile.evaluate("TeepPlan._fleetFilter='attention'; TeepPlan._renderFleetHosts()")
    assert mobile.locator(".tk-fleet-host-card").count() == 2  # behind + offline
    assert mobile.locator(".tk-fleet-host-actions .btn").first.evaluate(
        "el => el.getBoundingClientRect().height >= 44"
    )
    assert mobile.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert mobile.locator("#fleet-download-host").is_visible()
    mobile.screenshot(path=str(ROOT / ".artifacts" / "ui83-fleet-mobile.png"), full_page=True)
    browser.close()

SERVER.shutdown()

print("PASS UI-83 responsive Fleet release projection")
