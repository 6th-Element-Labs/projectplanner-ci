#!/usr/bin/env python3
"""Static regression checks for project-scoped frontend view state.

Run:
    python3 test_frontend_project_state.py
"""
import os
from scripts.frontend_test_source import read_frontend_source
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
source = read_frontend_source(ROOT)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


ok("groupModeKey()" in source, "frontend exposes a project-scoped group mode key helper")
ok("pm_group_mode:${window.PM_PROJECT || 'maxwell'}" in source,
   "group mode key includes the active project id")
ok("localStorage.getItem(this.groupModeKey())" in source,
   "groupMode reads the project-scoped key")
ok("localStorage.setItem(this.groupModeKey()" in source,
   "setGroupMode writes the project-scoped key")
ok(not re.search(r"localStorage\.(?:getItem|setItem)\('pm_group_mode'", source),
   "frontend no longer reads or writes the legacy global pm_group_mode key")

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
