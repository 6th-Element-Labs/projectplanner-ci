"""BUG-264 regression: mission loaders accept bodyless 304 responses."""

from path_setup import ROOT


MISSION_JS = (ROOT / "static/js/mission.js").read_text()
PLAYWRIGHT_RUNNER = (ROOT / "scripts/run_ui_playwright.py").read_text()
BROWSER_TEST = ROOT / "tests/browser/test_bug264_mission_304.py"


def ok(condition, message):
    if not condition:
        raise AssertionError(message)


for loader, model in (
    ("loadMissionStatus", "missionStatus"),
    ("loadMissionSummary", "missionSummary"),
    ("loadDependencyGraph", "missionGraph"),
):
    start = MISSION_JS.index(f"async {loader}")
    body = MISSION_JS[start:MISSION_JS.index("\n    async ", start + 10)]
    ok(f"if (res.status === 304) return this.{model};" in body,
       f"{loader} retains its cached model for a bodyless 304")

ok(BROWSER_TEST.is_file(), "browser-level Playwright regression exists")
ok('"BUG-264": "tests/browser/test_bug264_mission_304.py"' in PLAYWRIGHT_RUNNER,
   "the required Playwright runner discovers BUG-264")

print("BUG-264 mission 304 regression: PASS")
