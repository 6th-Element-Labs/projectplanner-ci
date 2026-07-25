#!/usr/bin/env python3
"""Two fail-quietly papercuts on the completion/dispatch path.

1. Completion evidence was validated scalar-only, so the richer structured form an agent
   naturally produces — {"schema": ..., "ok": true, "exit_code": 0} — stringified into
   something matching nothing and read as FALSE. complete_claim then refused with
   `missing_diff_check`, giving no hint that the SHAPE, not the result, was wrong. This
   sits on the path of every code_strict completion.

2. request_external_ci_mirror_run discovered missing GitHub credentials only once the
   push was already underway, leaving a run in `error` — which (before BUG-180) poisoned
   that SHA forever. The classic trigger is running it by hand as root instead of as the
   service account. Refuse up front and leave no wreckage.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="evidence-shape-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
import external_ci_mirror  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    truthy = store._evidence_truthy
    has_diff_check = store._completion_evidence_has_diff_check

    # --- scalars keep working exactly as before -------------------------------------
    for value in (True, "true", "yes", "ok", "clean", "passed", "1"):
        ok(truthy(value) is True, f"scalar {value!r} still reads as clean")
    for value in (False, None, "no", "false", "dirty", ""):
        ok(truthy(value) is False, f"scalar {value!r} still reads as not-clean")

    # --- structured evidence is now understood ---------------------------------------
    # This exact object is what a careful agent records, and what silently failed before.
    real_world = {"schema": "switchboard.git_diff_check.v1", "ok": True,
                  "exit_code": 0, "dirty": False, "conflict_markers": False,
                  "changed_files": ["a.py", "b.py"]}
    ok(truthy(real_world) is True,
       "a structured git_diff_check object with ok=true reads as clean")
    ok(has_diff_check({"git_diff_check": real_world}, {}) is True,
       "complete_claim's diff-check gate accepts the structured object")

    # --- ...but an object that says it FAILED must never read as a pass ---------------
    # This is the line that matters: accepting shape must not mean accepting anything.
    ok(truthy({"ok": False}) is False, "{'ok': false} is not clean")
    ok(truthy({"dirty": True}) is False,
       "{'dirty': true} is not clean even though the object is non-empty")
    ok(truthy({"ok": True, "dirty": True}) is False,
       "a negative field wins over an affirmative one (fail closed on contradiction)")
    ok(truthy({"failed": True}) is False, "{'failed': true} is not clean")
    ok(truthy({"status": "failed"}) is False, "{'status': 'failed'} is not clean")
    ok(truthy({"status": "clean"}) is True, "{'status': 'clean'} is clean")
    ok(truthy({}) is False,
       "an empty object is refused rather than assumed clean")
    ok(truthy({"schema": "x", "changed_files": []}) is False,
       "an object with no verdict field is refused, not guessed at")
    ok(has_diff_check({"git_diff_check": {"ok": False}}, {}) is False,
       "the diff-check gate still blocks when the object reports a failure")

    # --- the dispatch guard ----------------------------------------------------------
    saved = {name: os.environ.pop(name, None)
             for name in external_ci_mirror.GITHUB_CREDENTIAL_ENV_VARS}
    try:
        result = external_ci_mirror.request_external_ci_mirror_run(
            {"source_project": "switchboard", "source_sha": "a" * 40},
            str(TMP), actor="test", project="switchboard")
        ok(result.get("skip_reason") == "github_credentials_unavailable",
           "a credential-less dispatch is refused up front, by name")
        ok(result.get("ok") is False and result.get("dispatched") is False,
           "the refusal is not success-shaped")
        ok("sudo -u projectplanner" in str(result.get("error")),
           "the error names the actual fix (run as the service account)")
        runs = store.list_external_ci_runs(source_sha="a" * 40, project="switchboard")
        ok(not runs,
           "no run row is created, so there is no poisoned SHA to clean up afterwards")
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value

    # With a credential present the guard stands down (it must not block real dispatch).
    os.environ["PM_GITHUB_TOKEN"] = "test-token"
    try:
        ok(external_ci_mirror._github_credential_error() == "",
           "the guard stands down when a token is present")
    finally:
        os.environ.pop("PM_GITHUB_TOKEN", None)

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
