"""HOST-2: Fleet renders and revokes repo-scoped Host grants in Chromium."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import threading

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SERVER = ThreadingHTTPServer(
    ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(ROOT / "static")))
threading.Thread(target=SERVER.serve_forever, daemon=True).start()

with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1200, "height": 820})
    requests = []
    empty = ('{"projects":[],"deliverables":[],"workstreams":[],"people":[],"items":[],'
             '"attention":[],"requests":[],"signals":[],"inbox":[],"wake_intents":[],'
             '"sessions":[],"prs":[],"deployments":[],"hosts":[],"grants":[]}')
    page.route("**/api/**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=empty))
    page.route("**/api/projects**", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"projects":[{"id":"switchboard","label":"Switchboard"}]}'))
    page.route("**/api/board**", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"workstreams":[]}'))
    page.route("**/api/people**", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{"people":[]}'))
    page.route("**/health/saturation**", lambda route: route.fulfill(
        status=200, content_type="application/json", body='{}'))

    def ixp(route):
        if route.request.url.endswith("/ixp/v1/agent-host-grants/revoke"):
            requests.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json", body='{"revoked":true}')
        else:
            route.fulfill(status=200, content_type="application/json", body=empty)

    page.route("**/ixp/v1/**", ixp)
    page.goto(
        f"http://127.0.0.1:{SERVER.server_port}/index.html?project=switchboard#tab-fleet",
        wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("#fleet-hosts-body")
    page.evaluate("""() => {
      TeepPlan.esc = (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;');
      TeepPlan.project = 'switchboard'; TeepPlan.isAdmin = true; TeepPlan._fleetFilter = 'all';
      TeepPlan._fleetRelease = {};
      TeepPlan._fleetHosts = [{host_id:'host/steve-existing-mac',hostname:"Steve's Mac",
        stale:false,heartbeat_at:Date.now()/1000,runtimes:[{runtime:'codex'}],
        limits:{max_sessions:8},capacity:{active_sessions:0},enrollment:{execution_policy:{}},
        readiness:{state:'ready',contract_matches:true},project_grants:[
          {grant_id:'hostgrant-simplemark',status:'active',target_project_id:'simplemark',
           canonical_repository:'StevenRidder/simplemark',runtime:'codex',max_concurrency:2},
          {grant_id:'hostgrant-old',status:'revoked',target_project_id:'old',
           canonical_repository:'example/old',runtime:'codex',max_concurrency:1}
        ]}];
      TeepPlan._renderFleetHosts();
    }""")
    card = page.locator(".tk-fleet-host-card")
    assert card.count() == 1, page.locator("#fleet-hosts-body").inner_html()
    assert card.locator("[data-host-grant]").is_visible()
    assert "simplemark · StevenRidder/simplemark · codex · 2 parallel" in card.inner_text()
    assert card.locator("[data-host-grant-revoke]").count() == 1
    page.once("dialog", lambda dialog: dialog.accept())
    card.locator("[data-host-grant-revoke]").click()
    page.wait_for_timeout(100)
    assert requests == [{"project": "switchboard", "grant_id": "hostgrant-simplemark",
                         "reason": "fleet_operator_revoke"}]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    browser.close()

SERVER.shutdown()
print("PASS HOST-2 Fleet Host grants in Chromium")
