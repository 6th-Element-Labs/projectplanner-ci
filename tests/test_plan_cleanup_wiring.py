#!/usr/bin/env python3
"""Plan cleanup contract: the approved four-view shell keeps its live hooks."""
from pathlib import Path

from path_setup import ROOT

STATIC = Path(ROOT) / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")
BOARD = (STATIC / "js" / "board.js").read_text(encoding="utf-8")
CSS = (STATIC / "taikun-ui.css").read_text(encoding="utf-8")

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(condition)
    failed += int(not condition)


plan_start = INDEX.index('id="tab-plan-hub"')
plan_end = INDEX.index('id="tab-inbox-hub"')
PLAN = INDEX[plan_start:plan_end]

ok('class="tab-pane tk-plan-page" id="tab-plan-hub"' in INDEX,
   "Plan uses the dedicated clean page surface")
ok("Shape the work, sequence it, and track delivery." in PLAN, "approved Plan heading copy is present")
ok('id="plan-group-assignee"' in PLAN and 'id="plan-hide-done"' in PLAN,
   "View options preserve grouping and completed-task controls")
ok(PLAN.count('class="nav-link') == 4, "Plan keeps exactly four primary views")
for href in ("#tab-epics", "#tab-board", "#tab-gantt", "#tab-plan"):
    ok(f'href="{href}"' in PLAN, f"Plan view remains wired: {href}")
for hook in ("epics-content", "board", "gantt", "milestones-table", "path-table"):
    ok(f'id="{hook}"' in PLAN, f"existing renderer hook is preserved: {hook}")

ok("tk-plan-epic-row" in APP and "tk-plan-progress" in APP,
   "live workstream data renders in the approved summary-row treatment")
ok("data-plan-phase" not in APP and "tk-plan-phase-filters" not in CSS,
   "historical phase taxonomy does not create a second navigation row")
ok("setPlanGroups(show)" in APP, "View options can expand and collapse the existing task groups")
ok("plan-milestone-summary" in APP and "plan-path-summary" in APP,
   "Milestone and critical-path counts come from the live plan")
ok("plan-board-summary" in BOARD, "Board summary comes from the filtered live task set")
ok(".tk-plan-tabs" in CSS and ".tk-plan-epic-row" in CSS,
   "desktop Plan styling is part of the Taikun design layer")
ok("#tab-plan-hub #board{ display:block" in CSS and "#milestones-table thead{ display:none" in CSS,
   "mobile Board and Milestones reflow instead of overflowing")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
