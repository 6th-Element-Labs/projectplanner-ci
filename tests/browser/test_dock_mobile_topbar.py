#!/usr/bin/env python3
"""Fleet uses mobile navigation state instead of a second floating console."""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from path_setup import ROOT  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SCRIPTS = [
    "static/js/api.js", "static/js/state.js", "static/js/board.js",
    "static/js/plan-chat.js", "static/js/mission.js", "static/js/runner-session.js",
    "static/js/proof-console.js", "static/js/project-admin.js", "static/js/settings.js",
    "static/js/scope.js", "static/js/attention.js", "static/js/fleet-dock.js",
    "static/app.js",
]
STYLES = [
    "static/vendor/tabler/css/tabler.min.css",
    "static/vendor/tabler/css/tabler-icons.min.css",
    "static/taikun-tabler.css", "static/taikun-ui.css",
]
SETUP = """()=>{
  const now = Math.floor(Date.now()/1000);
  const R=[{runner_session_id:'a',task_id:'QA-12',status:'running',live:true,
            expires_at:now+180,updated_at:now,stale:false,
            environment:{uptime_seconds:180,last_output_at:now-10},
            available_actions:['kill']}];
  const ctx=Object.create(TeepPlan);
  ctx._dockCollapsed=false; ctx._dockAttention={}; ctx.tasks=[];
  ctx._loadFleetDock=()=>{};
  const render=()=>TeepPlan._renderFleetDock.call(
      ctx,R,[],{production:{last_deploy_ok:true},deployments:[]});
  ctx._renderFleetDock=render;
  window.__ctx=ctx; render();
}"""
MIN_TARGET = 44

failures = []


def ok(condition, message):
    if not condition:
        failures.append(message)


def box(page, selector):
    return page.evaluate(
        """(s)=>{const e=document.querySelector(s); if(!e) return null;
            if(e.offsetParent===null && getComputedStyle(e).display==='none') return {hidden:true};
            const r=e.getBoundingClientRect();
            return {w:Math.round(r.width),h:Math.round(r.height),x:Math.round(r.x)};}""", selector)


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)

    def open_dock(width):
        page = browser.new_page(viewport={"width": width, "height": 844})
        page.set_content('<body style="margin:0"><div id="fleet-dock"></div>'
                         '<nav class="tk-mobile-nav"><a data-tk-mobile-tab="#toptab-fleet">'
                         '<span id="mobile-fleet-badge"></span></a></nav></body>')
        for rel in STYLES:
            page.add_style_tag(path=str(ROOT / rel))
        for rel in SCRIPTS:
            page.add_script_tag(path=str(ROOT / rel))
        page.evaluate(SETUP)
        page.wait_for_timeout(250)
        return page

    # ── phone ──────────────────────────────────────────────────────────────
    m = open_dock(390)
    ok(m.locator("#fleet-dock > .card").count() == 0,
       "the desktop Fleet console must not float over mobile navigation")
    ok(m.locator("#fleet-mobile-activity").count() == 1,
       "active work gets one compact mobile activity bar")
    ok(m.locator("#mobile-fleet-badge").inner_text() == "1",
       "passive Fleet state appears on the mobile destination")
    ok("show" in (m.locator("#mobile-fleet-badge").get_attribute("class") or ""),
       "the Fleet destination badge is visible while work is active")
    m.close()

    # ── desktop: the compact bar is untouched ──────────────────────────────
    d = open_dock(1280)
    ok(box(d, "#fleet-dock-grab") == {"hidden": True},
       "the grabber is phone-only and must not appear on desktop")
    ok(box(d, ".dock-min-mobile") == {"hidden": True},
       "the left-hand minimize is phone-only")
    desk_min = box(d, "#fleet-dock-min-desktop")
    ok(desk_min and not desk_min.get("hidden"),
       f"desktop keeps its own compact minimize, got {desk_min}")
    inline = d.evaluate(
        """()=>{const hd=document.querySelector('#fleet-dock .card-header');
            const t=hd.querySelector('.fw-medium').getBoundingClientRect();
            const s=hd.querySelector('.dock-hd-status .ms-auto').getBoundingClientRect();
            return Math.abs(s.top - t.top) < 6;}""")
    ok(inline, "desktop keeps title and attention badge on one line")
    d.click("#fleet-dock-min-desktop")
    d.wait_for_timeout(150)
    ok(d.evaluate("()=>!!document.getElementById('fleet-dock-pill')"),
       "the desktop minimize still collapses the dock")
    d.close()
    browser.close()

# The header must not hard-code a single minimize position any more.
app = (ROOT / "static/app.js").read_text()
ok("fleet-mobile-activity" in app and "mobile-fleet-badge" in app,
   "mobile Fleet state is rendered into the nav hierarchy")
css = (ROOT / "static/taikun-ui.css").read_text()
ok("display: contents" in css,
   "desktop keeps its original single-row layout by dissolving the mobile wrappers")

if failures:
    for line in failures:
        print("FAIL", line)
    raise SystemExit(1)
print("PASS test_dock_mobile_topbar")
