#!/usr/bin/env python3
"""Fail-closed contract for the audited CI-repair administrator lane."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "ci_repair_merge_tested", ROOT / "scripts" / "ci_repair_merge.py")
repair = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(repair)

SHA = "a" * 40
BASE = "b" * 40
RUN = "30239999999"
RUN_URL = f"https://github.com/6th-Element-Labs/projectplanner-ci/actions/runs/{RUN}"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def fake_gh_factory(*, move_base=False):
    calls = []
    base_reads = {"count": 0}

    def fake_gh(*args):
        calls.append(args)
        joined = " ".join(args)
        if args[:2] == ("pr", "view") and "mergeCommit" not in joined:
            return {
                "state": "OPEN",
                "headRefOid": SHA,
                "baseRefName": "master",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "url": "https://github.com/6th-Element-Labs/projectplanner/pull/999",
            }
        if args[:2] == ("pr", "view") and "mergeCommit" in joined:
            return {
                "state": "MERGED",
                "mergedAt": "2026-07-27T04:00:00Z",
                "mergeCommit": {"oid": "c" * 40},
                "url": "https://github.com/6th-Element-Labs/projectplanner/pull/999",
            }
        if args[:2] == ("api", "repos/6th-Element-Labs/projectplanner/git/ref/heads/master"):
            base_reads["count"] += 1
            value = "d" * 40 if move_base and base_reads["count"] > 1 else BASE
            return {"object": {"sha": value}}
        if args[:2] == (
            "api", f"repos/6th-Element-Labs/projectplanner/compare/{BASE}...{SHA}"
        ):
            return {"merge_base_commit": {"sha": BASE}, "status": "ahead"}
        if args[:2] == ("run", "view"):
            return {
                "status": "completed",
                "conclusion": "success",
                "displayTitle": f"verify {SHA} (ci_repair)",
                "url": RUN_URL,
            }
        if args[:2] == (
            "api", f"repos/6th-Element-Labs/projectplanner/commits/{SHA}/status"
        ):
            return {
                "statuses": [{
                    "context": repair.CONTEXT,
                    "state": "success",
                    "target_url": RUN_URL,
                }]
            }
        if args[:2] == ("api", "user"):
            return {"login": "operator"}
        return None

    return fake_gh, calls


original_gh = repair.gh
original_argv = sys.argv
try:
    fake_gh, calls = fake_gh_factory()
    repair.gh = fake_gh
    sys.argv = [
        "ci_repair_merge.py",
        "--pr", "999",
        "--sha", SHA,
        "--run-url", RUN_URL,
        "--reason", "repair the CI path",
        "--merge",
    ]
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        rc = repair.main()
    receipt = json.loads(output.getvalue())
    ok(rc == 0 and receipt["merged"] and receipt["merge_sha"] == "c" * 40,
       "green exact-head receipt can perform one audited administrator squash merge")
    ok(any(call[:2] == ("label", "create") for call in calls)
       and any(call[:2] == ("pr", "comment") for call in calls),
       "repair path records ci-repair label and immutable audit comment")
    ok(any("--match-head-commit" in call for call in calls if call[:2] == ("pr", "merge")),
       "administrator merge is fenced to the tested exact head")

    moving_gh, moving_calls = fake_gh_factory(move_base=True)
    repair.gh = moving_gh
    sys.argv = [
        "ci_repair_merge.py",
        "--pr", "999",
        "--sha", SHA,
        "--run-url", RUN_URL,
        "--reason", "repair the CI path",
        "--merge",
    ]
    try:
        repair.main()
    except RuntimeError as exc:
        ok("base branch moved" in str(exc),
           "repair refuses when base moves after exact-SHA verification")
    else:
        ok(False, "moving base must refuse repair")
    ok(not any(call[:2] == ("pr", "merge") for call in moving_calls),
       "moving-base refusal happens before any administrator merge")
finally:
    repair.gh = original_gh
    sys.argv = original_argv

print(f"\nci_repair_merge: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
