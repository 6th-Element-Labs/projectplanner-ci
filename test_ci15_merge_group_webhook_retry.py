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
        "merge_authorization": {
            "published": False,
            "skip_reason": "merge_authorization_publish_failed",
            "error": "GitHub API rate limit exceeded: HTTP 403",
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
        "merge_authorization": {"published": True, "state": "success"},
    }
    second = webhook_inbox.drain(P)
    recovered_row = row("ci15-rate-limit")
    ok(second["applied"] == 1, "a later drain retries the same delivery")
    ok(recovered_row["status"] == "applied", "successful retry becomes applied")
    ok(recovered_row["last_error"] is None, "successful retry clears the transient error")

    # Authorization is independently required.  A successful CI dispatch must not
    # conceal a failed authorization callback.
    webhook_inbox.github_sync.handle_merge_group = lambda *_args, **_kwargs: {
        "action": "merge_group_ci_dispatched",
        "scratchpad_dispatched": True,
        "verify": {"ok": True, "status": "pending"},
        "merge_authorization": {
            "published": False,
            "skip_reason": "merge_authorization_publish_failed",
            "error": "GitHub API rate limit exceeded: HTTP 403",
        },
    }
    webhook_inbox.enqueue_event(
        P,
        delivery_guid="ci15-auth-rate-limit",
        event="merge_group",
        payload_bytes=json.dumps(payload),
    )
    third = webhook_inbox.drain(P)
    auth_row = row("ci15-auth-rate-limit")
    ok(third["retry_pending"] == 1, "authorization callback failure is retryable")
    ok("merge_group_authorization_failed" in auth_row["last_error"],
       "retry reason identifies the missing authorization status")
finally:
    webhook_inbox.github_sync.handle_merge_group = original
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
