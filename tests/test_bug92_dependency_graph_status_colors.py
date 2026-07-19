#!/usr/bin/env python3
"""BUG-92: dependency-map status colors match the task-status badges."""
from path_setup import ROOT


SOURCE = (ROOT / "static" / "js" / "mission.js").read_text(
    encoding="utf-8"
)


def ok(condition: bool, message: str) -> None:
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if not condition:
        raise SystemExit(1)


ok("['in_review', 'In review', '#eaf4fb', '#4299e1']" in SOURCE,
   "dependency-map legend uses the Azure In Review palette")
ok("in_review: 'bg-azure-lt'" in SOURCE,
   "dependency-map task pill uses the Azure In Review badge")
ok("in_review: 'bg-yellow-lt'" not in SOURCE,
   "dependency-map no longer renders In Review as yellow")

print("\nBUG-92 dependency-map status colors: 3 passed, 0 failed")
