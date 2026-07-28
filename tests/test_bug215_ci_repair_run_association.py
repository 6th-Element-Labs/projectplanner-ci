#!/usr/bin/env python3
"""BUG-215: a fresh repair dispatch must never bind an older same-SHA run."""
from __future__ import annotations

import sys
from types import SimpleNamespace

from path_setup import ROOT  # noqa: F401

# This unit exercises provider-run selection only. Keep it runnable in the canonical
# per-file gate without initializing the storage compatibility facade.
sys.modules["store"] = SimpleNamespace(
    DEFAULT_PROJECT="switchboard",
    EXTERNAL_CI_EXECUTION_LEASE_SECONDS=900,
    EXTERNAL_CI_TERMINAL_STATUSES={"success", "failure", "error", "cancelled"},
)
import external_ci_mirror


SHA = "b36dd3aa156dfb99ba5fe29a6d2d5c779372452e"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


old_run = {
    "databaseId": 30395192086,
    "status": "completed",
    "conclusion": "cancelled",
    "createdAt": "2026-07-28T20:00:00Z",
    "displayTitle": f"verify {SHA} (ci_repair)",
}
new_run = {
    "databaseId": 30396232028,
    "status": "queued",
    "conclusion": "",
    "createdAt": "2026-07-28T21:00:01Z",
    "displayTitle": f"verify {SHA} (ci_repair)",
}
dispatch_time = external_ci_mirror._github_timestamp("2026-07-28T21:00:00Z")

selected = external_ci_mirror._select_run(
    [old_run, new_run],
    triggered_after=dispatch_time,
    source_sha=SHA,
    purpose="ci_repair",
)
ok(selected is not None and selected["databaseId"] == 30396232028,
   "the newly dispatched run owns the repair lifecycle")

selected_before_new_run_exists = external_ci_mirror._select_run(
    [old_run],
    triggered_after=dispatch_time,
    source_sha=SHA,
    purpose="ci_repair",
)
ok(selected_before_new_run_exists is None,
   "an older terminal same-SHA run is ignored while the new run is not visible yet")

wrong_purpose = {
    **new_run,
    "databaseId": 30396232029,
    "displayTitle": f"verify {SHA} (head)",
}
ok(external_ci_mirror._select_run(
       [wrong_purpose],
       triggered_after=dispatch_time,
       source_sha=SHA,
       purpose="ci_repair",
   ) is None,
   "a post-dispatch run with the wrong purpose is ignored")

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
