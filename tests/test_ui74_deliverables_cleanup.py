#!/usr/bin/env python3
"""UI-74: simplified responsive Deliverables page preserves real controls."""
from path_setup import ROOT

MISSION = (ROOT / "static/js/mission.js").read_text(encoding="utf-8")
APP = (ROOT / "static/app.js").read_text(encoding="utf-8")
PROOF = (ROOT / "static/js/proof-console.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/taikun-scope.css").read_text(encoding="utf-8")


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(condition)
    failed += int(not condition)


ok('>Autopilot</button>' in MISSION
   and 'data-autopilot-action="start"' in MISSION
   and 'data-autopilot-scope="deliverable"' in MISSION,
   "deliverable Autopilot keeps the existing start action")

for view in ("overview", "map", "verification"):
    ok(f"mission-view-{view}" in MISSION, f"Deliverables exposes the {view} view")
ok("Work map" in MISSION and "Completion verification" in MISSION,
   "secondary views are plainly named")

ok("data-mission-watch-task" in MISSION
   and "data-mission-kill-task" in MISSION
   and "_taskAutopilotButtonHtml" in MISSION
   and "requestRunnerControl(" in APP
   and "data-mission-kill-runner" in APP,
   "task rows reuse Start, Watch, and confirmed runner Kill controls")

ok("data-linked-task" in MISSION
   and "openLinkedTask" in MISSION
   and "mission-work-table" in MISSION
   and "@media (max-width: 767.98px)" in CSS
   and ".mission-task-controls" in CSS,
   "task details and responsive Tabler layout remain wired")

ok("Detailed verification" in PROOF and "operator diagnostics" in PROOF,
   "Proof Console is presented as verification diagnostics")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
