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
    console_errors = []
    failed_requests = []
    page.on("console", lambda message: console_errors.append(message.text)
            if message.type == "error" else None)
    page.on("requestfailed", lambda request: failed_requests.append(
        f"{request.method} {request.url}: {request.failure}"))
    page.on("response", lambda response: failed_requests.append(
        f"HTTP {response.status} {response.url}") if response.status >= 400 else None)
    empty_api = ('{"projects":[],"deliverables":[],"workstreams":[],"people":[],'
                 '"items":[],"attention":[],"requests":[],"signals":[],"inbox":[],'
                 '"digests":[],"wake_intents":[],"sessions":[],"prs":[],'
                 '"deployments":[],"hosts":[]}')
    page.route("**/api/**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=empty_api))
    page.route("**/ixp/v1/**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=empty_api))
    page.route("**/api/projects**", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"projects":[{"id":"switchboard","label":"Switchboard"}]}'))
    page.route("**/api/deliverables**", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"deliverables":[]}'))
    page.route("**/health/saturation**", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{}'))
    page.route("**/api/board**", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"workstreams":[]}'))
    page.route("**/api/people**", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"people":[]}'))
    page.route("**/tally/v1/project**", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{}'))
    page.goto(f"http://127.0.0.1:{SERVER.server_port}/index.html?project=switchboard#tab-fleet",
              wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
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
        ,{host_id:'host/plan-vm-message-wake', hostname:'ip-172-31-45-202', stale:false,
         heartbeat_at:Date.now()/1000-5, agent_host_version:'0.2.0',
         bundle_digest:'sha256:source-tree', contract_fingerprint:'eac1:match',
         release_management:'deployment_managed', runtimes:[{runtime:'claude-code'}],
         limits:{max_sessions:1}, capacity:{active_sessions:0,release_management:'deployment_managed'},
         readiness:{state:'ready', installed_version:'0.2.0', installed_digest:'sha256:source-tree',
           required_version:'', required_digest:'', contract_matches:true, actionable:false,
           release_management:'deployment_managed',
           detail:'Managed by the Switchboard deployment; Host Adapter releases do not apply.'}}
      ];
      TeepPlan._fleetFilter = 'all';
      TeepPlan._renderFleetHosts();
    }""")
    return console_errors, failed_requests


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    desktop = browser.new_page(viewport={"width": 1440, "height": 900})
    desktop_console_errors, desktop_failed_requests = load(desktop)
    assert desktop.locator("#tab-fleet").count() == 1
    assert desktop.locator(".tk-fleet-host-card").count() == 3
    assert "0.4.20" in desktop.locator(".tk-fleet-host-card").first.inner_text()
    assert "0.4.21" in desktop.locator(".tk-fleet-host-card").first.inner_text()
    assert "Digest differs" in desktop.locator(".tk-fleet-host-card").first.inner_text()
    assert desktop.locator("#fleet-update-banner").is_visible()
    assert desktop.locator("#fleet-download-host").is_enabled()
    managed = desktop.locator(".tk-fleet-host-card").filter(has_text="ip-172-31-45-202")
    assert "Managed deployment" in managed.inner_text()
    assert "Not applicable" in managed.inner_text()
    assert managed.locator("[data-host-update]").count() == 0
    assert managed.locator("[data-fleet-download]").count() == 0
    assert desktop_console_errors == [], desktop_console_errors
    assert desktop_failed_requests == [], desktop_failed_requests
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
    mobile_console_errors, mobile_failed_requests = load(mobile)
    mobile.evaluate("TeepPlan._fleetFilter='attention'; TeepPlan._renderFleetHosts()")
    assert mobile.locator(".tk-fleet-host-card").count() == 2  # behind + offline
    assert mobile.locator(".tk-fleet-host-actions .btn").first.evaluate(
        "el => el.getBoundingClientRect().height >= 44"
    )
    assert mobile.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert mobile.locator("#fleet-download-host").is_visible()
    assert mobile_console_errors == [], mobile_console_errors
    assert mobile_failed_requests == [], mobile_failed_requests
    mobile.screenshot(path=str(ROOT / ".artifacts" / "ui83-fleet-mobile.png"), full_page=True)
    print(f"Chromium {browser.version}; desktop/mobile console errors 0; failed requests 0")
    browser.close()

SERVER.shutdown()

print("PASS UI-83 responsive Fleet release projection")
