#!/usr/bin/env python3
"""BUG-92: dependency-map status colors match the task-status badges."""
from path_setup import ROOT


SOURCE = (ROOT / "static" / "js" / "mission.js").read_text(
    encoding="utf-8"
)
STATE = (ROOT / "static" / "js" / "state.js").read_text(encoding="utf-8")
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def ok(condition: bool, message: str) -> None:
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if not condition:
        raise SystemExit(1)


ok("['in_review', 'In review', '#ffe083', '#e0a800']" in SOURCE,
   "dependency-map legend uses the yellow In Review palette")
ok("in_review: 'bg-yellow-lt'" in SOURCE,
   "dependency-map task pill uses the yellow In Review badge")
ok("in_review: 'bg-azure-lt'" not in SOURCE,
   "dependency-map no longer renders In Review as Azure")
ok("'In Review': 'yellow'" in STATE,
   "task-status badges use the yellow In Review palette")
ok("'In Review': 'yellow'" in APP,
   "proposal status badges use the yellow In Review palette")

print("\nBUG-92 status colors: 5 passed, 0 failed")
