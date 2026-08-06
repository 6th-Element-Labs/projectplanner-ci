#!/usr/bin/env python3
"""CI-15: transient merge-group side-effect failures remain retryable."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


TMP = Path(tempfile.mkdtemp(prefix="ci15-webhook-retry-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import store  # noqa: E402
import webhook_inbox  # noqa: E402


P = "switchboard"
SHA = "319829539d8d230c458accf0ca43639ff8a708a1"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def row(guid):
    with store._conn(P) as conn:
        return dict(conn.execute(
            "SELECT status, attempts, last_error, result_json "
            "FROM webhook_inbox WHERE delivery_guid=?",
            (guid,),
        ).fetchone())


payload = {
    "action": "checks_requested",
    "repository": {"full_name": "6th-Element-Labs/projectplanner"},
    "merge_group": {
        "head_sha": SHA,
        "head_ref": f"refs/heads/gh-readonly-queue/master/pr-942-{SHA}",
    },
}

original = webhook_inbox.github_sync.handle_merge_group
try:
    store.init_project_registry()
    store.init_db(P)

    # This is the production incident shape: GitHub rate-limited mirror dispatch,
    # but the handler returned a structured skip instead of raising.
    webhook_inbox.github_sync.handle_merge_group = lambda *_args, **_kwargs: {
        "action": "merge_group_ci_skipped",
        "scratchpad_skip_reason": "GitHub API rate limit exceeded: HTTP 403",
        "verify": {
            "ok": False,
            "stall": "dispatch",
            "failure_class": "mirror_sync_failed",
        },
    }
    webhook_inbox.enqueue_event(
        P,
        delivery_guid="ci15-rate-limit",
        event="merge_group",
        payload_bytes=json.dumps(payload),
    )
    first = webhook_inbox.drain(P)
    failed_row = row("ci15-rate-limit")
    ok(first["retry_pending"] == 1, "rate-limited merge-group delivery is retryable")
    ok(failed_row["status"] == "pending", "failed delivery is not marked applied")
    ok(failed_row["attempts"] == 1, "failed delivery consumes one bounded retry")
    ok("merge_group_ci_dispatch_failed" in failed_row["last_error"],
       "retry reason identifies the missing CI dispatch")

    webhook_inbox.github_sync.handle_merge_group = lambda *_args, **_kwargs: {
        "action": "merge_group_ci_dispatched",
        "scratchpad_dispatched": True,
        "verify": {"ok": True, "status": "pending"},
    }
    second = webhook_inbox.drain(P)
    recovered_row = row("ci15-rate-limit")
    ok(second["applied"] == 1, "a later drain retries the same delivery")
    ok(recovered_row["status"] == "applied", "successful retry becomes applied")
    ok(recovered_row["last_error"] is None, "successful retry clears the transient error")

    # --- BUG-323 end-to-end: an unwritable CI checkout must stay retryable ------------
    # The whole chain runs for real here — handle_merge_group -> verify(ensure=True) ->
    # dispatch_scratchpad_ref -> request_external_ci_mirror_run — with only the two
    # GitHub network calls stubbed. The dispatch-stage refusal must NOT be recorded as a
    # settled delivery: the merge-group SHA is minted by GitHub and can never change, so
    # a swallowed refusal leaves the queue waiting forever for a status that cannot come.
    webhook_inbox.github_sync.handle_merge_group = original
    store.set_project_repo_topology(
        project=P,
        canonical_repo="6th-Element-Labs/projectplanner",
        public_ci_repo="6th-Element-Labs/projectplanner-ci",
    )
    readonly_source = TMP / "readonly-ci-source"
    (readonly_source / ".git").mkdir(parents=True)
    os.chmod(readonly_source / ".git", 0o555)
    csd = webhook_inbox.github_sync.ci_scratchpad_dispatch
    saved_env = {name: os.environ.get(name) for name in
                 ("SWITCHBOARD_CI_SOURCE_PATH", "PM_GITHUB_TOKEN")}
    saved_token_fn, saved_commit_fn = csd.cvd._token, csd.cvd.verify_commit_exists
    os.environ["SWITCHBOARD_CI_SOURCE_PATH"] = str(readonly_source)
    os.environ["PM_GITHUB_TOKEN"] = "test-token"
    csd.cvd._token = lambda *a, **k: "tok"
    csd.cvd.verify_commit_exists = lambda *a, **k: None
    try:
        if os.access(readonly_source / ".git", os.W_OK):
            # Announced, never silent: root bypasses mode bits, so this environment
            # cannot express the read-only checkout under test.
            print("  SKIP  unwritable-checkout merge-group retry (euid can write any mode)")
        else:
            webhook_inbox.enqueue_event(
                P,
                delivery_guid="bug323-readonly-checkout",
                event="merge_group",
                payload_bytes=json.dumps(payload),
            )
            drained = webhook_inbox.drain(P)
            readonly_row = row("bug323-readonly-checkout")
            handled = webhook_inbox.github_sync.handle_merge_group(payload, P)
            ok(drained["retry_pending"] == 1 and readonly_row["status"] == "pending",
               "an unwritable CI checkout leaves the merge-group delivery retryable")
            ok("merge_group_ci_dispatch_failed" in (readonly_row["last_error"] or ""),
               "the retry reason still names the missing CI dispatch")
            ok(handled["action"] == "merge_group_ci_skipped"
               and handled["scratchpad_skip_reason"] == "source_checkout_not_writable",
               "the environment fault is reported as a skip, naming its own cause")
            ok(handled["verify"]["ok"] is False
               and handled["verify"]["status"] == "pending"
               and handled["verify"]["stall"] == "dispatch"
               and handled["verify"]["failure_class"] == "mirror_sync_failed",
               "verify surfaces pending/dispatch for the abort instead of a red verdict")
            ok(all(ctx["state"] != "failure" for ctx in handled["verify"]["contexts"]),
               "no required context is fabricated as failing for an environment fault")
            ok(not store.list_external_ci_runs(source_sha=SHA, project=P),
               "the refused merge-group dispatch records no external CI run")
    finally:
        os.chmod(readonly_source / ".git", 0o755)
        csd.cvd._token, csd.cvd.verify_commit_exists = saved_token_fn, saved_commit_fn
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
finally:
    webhook_inbox.github_sync.handle_merge_group = original
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
