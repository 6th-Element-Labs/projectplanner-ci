#!/usr/bin/env python3
"""BUG-180: a failed mirror run must not poison its SHA forever.

`external_ci_runs` is idempotent per source SHA, and the resume path treated all four
terminal statuses alike. But `success`/`failure` mean CI RAN and published a verdict,
while `error`/`cancelled` mean the run ABORTED and nothing was ever posted. Resuming the
second group as if it were done meant no status could ever appear for that SHA — and the
resume returned without setting `ok`, so callers read a dead run as a successful dispatch.

A PR head escapes this by pushing a new commit. A merge-group head SHA is minted by
GitHub and cannot change, so the native merge queue waited forever (PR #870 sat
AWAITING_CHECKS ~25 min and had to be dequeued and admin-merged).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="bug180-mirror-retry-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"
# This suite patches _execute_run rather than injecting a CommandRunner, so it never
# shells out to git. Satisfy the credential pre-flight explicitly — the thing under test
# here is terminal-state retry, not authentication.
os.environ["PM_GITHUB_TOKEN"] = "test-token-not-used"

import store  # noqa: E402
import external_ci_mirror  # noqa: E402


P = "switchboard"
SHA = "f46cbff2b2b7d7d57c51f0f003a63bc5d3f23c95"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def make_request(sha, **extra):
    req = {
        "source_project": P,
        "source_repo": "6th-Element-Labs/projectplanner",
        "source_sha": sha,
        "mirror_repo": "6th-Element-Labs/projectplanner-ci",
        "mirror_branch": f"ci/mg-{sha[:12]}",
        "workflow": "verify.yml",
        "push_triggered": True,
        "poll_after_push": False,
    }
    req.update(extra)
    return req


executions = []


def fake_execute(run, source_path, actor, project, runner, sleep_fn, now_fn, request):
    """Stand in for the real push/dispatch so the test never touches GitHub."""
    executions.append(run.get("run_id"))
    return store.update_external_ci_run(
        run["run_id"], {"status": "triggered"}, actor=actor, project=project)


try:
    store.init_db(P)
    external_ci_mirror._execute_run = fake_execute
    checkout = str(TMP)

    # A first dispatch runs normally.
    first = external_ci_mirror.request_external_ci_mirror_run(
        make_request(SHA), checkout, actor="test", project=P)
    run_id = first.get("run_id")
    ok(bool(run_id) and len(executions) == 1,
       "first dispatch for a SHA executes")

    # Drive it into the abortive terminal state the live incident hit.
    store.update_external_ci_run(
        run_id, {"status": "error", "failure_class": "mirror_sync_failed",
                 "failure_reason": "could not read Username for 'https://github.com'"},
        actor="test", project=P)

    # --- Without opting in, the refusal must be LOUD, not success-shaped. -------------
    before = len(executions)
    plain = external_ci_mirror.request_external_ci_mirror_run(
        make_request(SHA), checkout, actor="test", project=P)
    ok(len(executions) == before,
       "a poisoned SHA does not silently re-execute without opting in")
    ok(plain.get("ok") is False,
       "the refused resume reports ok=False (it used to leave ok unset, reading as success)")
    ok(plain.get("skip_reason") == "previous_run_failed",
       "the refusal names itself: previous_run_failed")
    ok(bool(plain.get("error")),
       "the refusal carries an error explaining no CI status was produced")
    ok(plain.get("retryable") is True,
       "the refused run is marked retryable so a caller knows recovery is possible")

    # --- Opting in must ACTUALLY re-run, reusing the immutable SHA's run row. ---------
    before = len(executions)
    retried = external_ci_mirror.request_external_ci_mirror_run(
        make_request(SHA, retry_terminal_error=True), checkout, actor="test", project=P)
    ok(len(executions) == before + 1,
       "retry_terminal_error re-executes the aborted run")
    ok(retried.get("run_id") == run_id,
       "the retry reuses the same run row (the SHA is immutable, so it is one run)")
    ok(retried.get("status") not in {"error", "cancelled"},
       "the retried run leaves the abortive terminal state")

    # --- A run that produced a real VERDICT must NOT be re-run. -----------------------
    # This is the line that keeps idempotency meaningful: CI genuinely reported, the
    # status exists on the SHA, and re-running would be waste and churn.
    verdict_sha = "a" * 40
    external_ci_mirror.request_external_ci_mirror_run(
        make_request(verdict_sha), checkout, actor="test", project=P)
    verdict_run = store.list_external_ci_runs(source_sha=verdict_sha, project=P)[0]
    for terminal in ("success", "failure"):
        store.update_external_ci_run(
            verdict_run["run_id"], {"status": terminal}, actor="test", project=P)
        before = len(executions)
        resumed = external_ci_mirror.request_external_ci_mirror_run(
            make_request(verdict_sha, retry_terminal_error=True),
            checkout, actor="test", project=P)
        ok(len(executions) == before,
           f"a {terminal} run is NOT re-executed even with retry_terminal_error")
        ok(resumed.get("retryable") is not True,
           f"a {terminal} run is not marked retryable")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
