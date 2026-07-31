#!/usr/bin/env python3
"""BREAKDOWN 59: "already in the queue" is the goal, not a failure.

Live on COORD-68, 2026-07-27. Completion enqueued PR #976 successfully, then a
later tick ran ``requeue_merge_group`` and GitHub answered:

    gh: Pull request is already in the queue

That was recorded as a FAILED completion effect. Each such tick counted as
non-progress, and enough of them tripped COORD-77's no-spin ladder into
``human(completion_not_converging)`` -- pulling the operator in to "resolve" a PR
that was CLEAN, rebased, correctly queued, and on track to merge unattended.

Autopilot is supposed to converge, not escalate. An effect whose provider
response means "the state you asked for already holds" is verified, never failed.
"""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from switchboard.application import completion_driver  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if condition:
        passed += 1
    else:
        failed += 1


normalize = completion_driver._normalize_already_queued

# --- the live regression -----------------------------------------------------
live = normalize({
    "returncode": 1,
    "stdout": "",
    "stderr": "gh: Pull request is already in the queue",
})
ok(live["returncode"] == 0,
   "the exact live gh refusal normalizes to success")
ok(live.get("idempotent_already_queued") is True,
   "the idempotent replay is marked, so the ledger stays auditable")

# --- it must not swallow real failures ---------------------------------------
for stderr in (
    "gh: Pull request is not mergeable",
    "gh: Resource not accessible by integration",
    "GitHub command timed out after 30 seconds",
    "merge queue is disabled for this repository",
):
    result = normalize({"returncode": 1, "stdout": "", "stderr": stderr})
    ok(result["returncode"] == 1,
       f"a genuine failure stays failed: {stderr[:44]!r}")
    ok("idempotent_already_queued" not in result,
       "a genuine failure is not marked idempotent")

# --- success passes through untouched ----------------------------------------
clean = normalize({"returncode": 0, "stdout": '{"data":{}}', "stderr": ""})
ok(clean["returncode"] == 0, "an ordinary success is unchanged")
ok("idempotent_already_queued" not in clean,
   "an ordinary success is not mislabelled as a replay")

# --- the marker is matched on stdout too -------------------------------------
on_stdout = normalize({
    "returncode": 1,
    "stdout": "Pull request is already in a merge queue",
    "stderr": "",
})
ok(on_stdout["returncode"] == 0,
   "the marker is honoured when gh reports it on stdout")

print(f"\nCOORD-68 already-queued is success: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
