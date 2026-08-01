#!/usr/bin/env python3
"""Chromium regression for bodyless 304 mission responses."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
MISSION_JS = (ROOT / "static" / "js" / "mission.js").read_text(encoding="utf-8")
PAYLOADS = {
    "mission_status": {"deliverable_id": "bug264", "linked_tasks": [{"task_id": "BUG-264"}]},
    "mission_summary": {"deliverable_id": "bug264", "counts": {"active_work": 1}},
    "dependency_graph": {"nodes": [{"id": "BUG-264"}], "edges": []},
}
HTML = "<!doctype html><html><body><script src='/mission.js'></script></body></html>"


def main() -> int:
    request_counts = {name: 0 for name in PAYLOADS}
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        def route_request(route):
            url = route.request.url
            if url.endswith("/mission.js"):
                route.fulfill(status=200, content_type="application/javascript", body=MISSION_JS)
                return
            if url.rstrip("/").endswith("/test"):
                route.fulfill(status=200, content_type="text/html", body=HTML)
                return
            for name, payload in PAYLOADS.items():
                if f"/{name}" in url:
                    request_counts[name] += 1
                    if request_counts[name] == 1:
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
                    else:
                        route.fulfill(status=304)
                    return
            route.fulfill(status=404, content_type="application/json", body='{"detail":"not found"}')

        page.route("**/*", route_request)
        page.goto("https://bug264.local/test", wait_until="domcontentloaded")
        result = page.evaluate("""async () => {
            const controller = Object.assign({}, SwitchboardMission.methods);
            const first = await Promise.all([
                controller.loadMissionStatus('bug264'),
                controller.loadMissionSummary('bug264'),
                controller.loadDependencyGraph('bug264'),
            ]);
            const second = await Promise.all([
                controller.loadMissionStatus('bug264'),
                controller.loadMissionSummary('bug264'),
                controller.loadDependencyGraph('bug264'),
            ]);
            return {first, second, retained: [
                controller.missionStatus,
                controller.missionSummary,
                controller.missionGraph,
            ]};
        }""")

        expected = list(PAYLOADS.values())
        assert result["first"] == expected, result
        assert result["second"] == expected, result
        assert result["retained"] == expected, result
        assert request_counts == {name: 2 for name in PAYLOADS}, request_counts
        assert not page_errors, page_errors
        browser.close()

    print("PASS BUG-264 bodyless 304 responses retain cached mission models")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"FAIL BUG-264: {error}", file=sys.stderr)
        raise
