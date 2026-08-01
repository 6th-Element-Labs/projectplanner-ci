"""BUG-260 regression: Deliverables first paint and polling stay compact."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MISSION_JS = (ROOT / "static/js/mission.js").read_text()
ROUTER = (ROOT / "src/switchboard/api/routers/deliverables.py").read_text()
REPOSITORY = (ROOT / "src/switchboard/storage/repositories/deliverables.py").read_text()
PLAYWRIGHT_RUNNER = (ROOT / "scripts/run_ui_playwright.py").read_text()
BROWSER_TEST = ROOT / "tests/browser/test_bug260_mission_summary_first_paint.py"


def ok(condition, message):
    if not condition:
        raise AssertionError(message)


summary_loader = MISSION_JS.index("async loadMissionSummary")
refresh = MISSION_JS.index("async refreshMissionPage")
live_tick = MISSION_JS.index("async _missionLiveTick")
live_end = MISSION_JS.index("_startMissionLive()", live_tick)
refresh_body = MISSION_JS[refresh:MISSION_JS.index("_agoShort", refresh)]
live_body = MISSION_JS[live_tick:live_end]

ok(summary_loader >= 0, "mission summary has a dedicated client read")
ok("await this.loadMissionSummary(this.selectedDeliverableId)" in refresh_body,
   "first paint awaits the compact summary")
for heavy in (
    "loadDependencyGraph(this.selectedDeliverableId)",
    "loadAutopilotScopes(this.selectedDeliverableId)",
    "loadBreakdownProposals(this.selectedDeliverableId)",
    "loadKpisAndOutcomes()",
):
    ok(heavy not in refresh_body, f"first paint must not await {heavy}")
ok("await this.loadMissionSummary(id)" in live_body,
   "live polling uses the compact summary")
ok("loadMissionStatus(id)" not in live_body and "loadDependencyGraph(id)" not in live_body,
   "live polling does not reread full mission or graph payloads")
ok('"/api/deliverables/{deliverable_id}/mission_summary"' in ROUTER,
   "thin REST adapter exposes mission_summary")
ok('"schema": "switchboard.mission_summary.v1"' in REPOSITORY,
   "summary is a typed bounded read model")
summary_repo = REPOSITORY[REPOSITORY.index("def get_mission_summary"):
                          REPOSITORY.index("def _live_execution_for")]
ok("get_mission_status(" not in summary_repo,
   "summary repository path must not invoke the full mission status projection")
ok("_build_mission_summary" in summary_repo and "_batch_mission_summary_links" in summary_repo,
   "summary uses a dedicated bounded repository projection")
ok("this._missionDetailLoaded = false" in refresh_body,
   "refreshing or switching deliverables resets detail-panel state")
for field in ('"counts"', '"blockers"', '"active_work"', '"next_actions"'):
    ok(field in REPOSITORY, f"summary includes {field}")
ok(BROWSER_TEST.is_file(), "browser-level Playwright regression exists")
ok('"BUG-260": "tests/browser/test_bug260_mission_summary_first_paint.py"' in PLAYWRIGHT_RUNNER,
   "the required Playwright runner discovers the BUG-260 browser regression")

print("BUG-260 mission summary first-paint regression: PASS")
