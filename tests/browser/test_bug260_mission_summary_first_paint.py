#!/usr/bin/env python3
"""Chromium regression for BUG-260's compact Deliverables first paint."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
MISSION_JS = (ROOT / "static" / "js" / "mission.js").read_text(encoding="utf-8")

SUMMARY = {
    "schema": "switchboard.mission_summary.v1",
    "project_id": "switchboard",
    "deliverable_id": "bug260-browser",
    "deliverable": {"id": "bug260-browser", "title": "Fast cockpit", "status": "active"},
    "progress": {"done_with_proof_ratio": 0.5},
    "counts": {"done_with_proof": 1, "active_work": 1, "blockers": 0},
    "active_work": [{"task_id": "BUG-260", "project_id": "switchboard",
                     "title": "Compact first paint", "status": "In Progress"}],
    "blockers": [],
    "next_actions": [{"kind": "review", "label": "Review compact cockpit"}],
}
SECOND_SUMMARY = {
    **SUMMARY,
    "deliverable_id": "bug260-second",
    "deliverable": {"id": "bug260-second", "title": "Fresh summary", "status": "active"},
    "counts": {"done_with_proof": 2, "active_work": 0, "blockers": 0},
    "active_work": [],
}

HTML = """<!doctype html><html><body>
<a id="toptab-mission" class="active"></a><main id="mission-page"></main>
<script>
window.PM_PROJECT = 'switchboard';
window.bootstrap = {Tab: {getOrCreateInstance: () => ({show() {}})}};
</script><script src="/mission.js"></script></body></html>"""


def main() -> int:
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors: list[str] = []
        requests: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        def route_request(route):
            url = route.request.url
            if url.endswith("/mission.js"):
                route.fulfill(status=200, content_type="application/javascript", body=MISSION_JS)
                return
            if url.rstrip("/").endswith("/audit"):
                route.fulfill(status=200, content_type="text/html", body=HTML)
                return
            requests.append(url)
            if "/mission_summary" in url:
                payload = SECOND_SUMMARY if "bug260-second" in url else SUMMARY
            elif "/mission_status" in url:
                payload = {**SUMMARY, "linked_tasks": [], "active_agents": []}
            elif "/dependency_graph" in url:
                payload = {"nodes": [], "edges": [], "stats": {}}
            elif "/autopilot" in url:
                payload = {"scopes": []}
            elif "/breakdown" in url:
                payload = {"proposals": []}
            elif "/kpis" in url or "/outcomes" in url:
                payload = {"kpis": [], "outcomes": []}
            else:
                payload = {}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

        page.route("**/*", route_request)
        page.goto("https://bug260.local/audit", wait_until="domcontentloaded")
        page.evaluate("""() => {
            const controller = Object.assign({}, SwitchboardMission.methods, {
                selectedDeliverableId: 'bug260-browser',
                deliverables: [{id: 'bug260-browser', title: 'Fast cockpit'},
                               {id: 'bug260-second', title: 'Fresh summary'}],
                _deliverablesProject: 'switchboard',
                esc: (value) => String(value == null ? '' : value),
                _ensureScript: async () => {},
                _pollDueWhileHidden: () => true,
                STATUS_COLOR: {}, DELIVERABLE_STATUS_COLOR: {},
                _missionBadge: (value) => `<span>${value || ''}</span>`,
                _syncHeaderDeliverable: () => {},
                loadAutopilotScopes: async () => { await fetch('api/deliverables/bug260-browser/autopilot_scopes'); controller.autopilotScopes = []; },
                loadBreakdownProposals: async () => { await fetch('api/deliverables/bug260-browser/breakdown_proposals'); },
                loadKpisAndOutcomes: async () => { await fetch('api/deliverables/bug260-browser/kpis'); },
                renderMissionPage: () => { document.getElementById('mission-page').dataset.detail = 'loaded'; },
            });
            window.controller = controller;
        }""")

        page.evaluate("() => controller.refreshMissionPage()")
        page.get_by_text("Fast cockpit").wait_for()
        first_paint = list(requests)
        assert len(first_paint) == 1 and "/mission_summary" in first_paint[0], first_paint

        page.evaluate("() => controller._missionLiveTick()")
        poll = requests[len(first_paint):]
        assert len(poll) == 1 and "/mission_summary" in poll[0], poll

        page.locator("#mission-open-work").click()
        page.wait_for_function("() => document.querySelector('#mission-page').dataset.detail === 'loaded'")
        deferred = requests[len(first_paint) + len(poll):]
        assert any("/mission_status" in url for url in deferred), deferred
        assert any("/autopilot_scopes" in url for url in deferred), deferred
        assert any("/breakdown_proposals" in url for url in deferred), deferred
        assert any("/kpis" in url for url in deferred), deferred

        page.evaluate("""async () => {
            controller.selectedDeliverableId = 'bug260-second';
            await controller.refreshMissionPage();
        }""")
        page.get_by_text("Fresh summary").wait_for()
        assert page.evaluate("() => controller._missionDetailLoaded") is False
        assert page.evaluate("() => controller.missionSummary.deliverable_id") == "bug260-second"
        assert not page_errors, page_errors
        browser.close()

    print("PASS BUG-260 Chromium first paint and polling request only mission_summary")
    print("PASS BUG-260 heavy panel reads remain deferred until the panel opens")
    print("PASS BUG-260 switching deliverables after details rerenders the new live summary")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"FAIL BUG-260: {error}", file=sys.stderr)
        raise
