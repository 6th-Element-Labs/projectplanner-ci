#!/usr/bin/env python3
"""Retention for the two operational scratch tables that had no expiry.

`idempotency_keys` and `webhook_inbox` are dedupe/queue machinery, not business truth,
and together reached 261 MB (~30% of a database already larger than the box's RAM).
These assertions pin the safety properties, not just that deletion happens: an in-flight
delivery survives regardless of age, a fresh key is never dropped, and a bare call is a
dry run.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="operational-retention-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
import webhook_inbox as inbox_mod  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.storage.repositories import operational_retention as ret  # noqa: E402


P = "switchboard"
DAY = 86400.0
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def seed(now):
    """One row per (table, age, status) combination we care about."""
    inbox_mod._ensure(P)
    with _conn(P) as c:
        c.execute("DELETE FROM idempotency_keys")
        c.execute("DELETE FROM webhook_inbox")
        for label, age_days in (("fresh", 0.5), ("old", 30.0)):
            c.execute(
                "INSERT INTO idempotency_keys(idem_key, operation, actor, request_hash,"
                " response_json, created_at) VALUES (?,?,?,?,?,?)",
                (f"idem-{label}", "claim", "test", "hash", '{"x":1}', now - age_days * DAY))
            for status in ("applied", "ignored", "pending"):
                c.execute(
                    "INSERT INTO webhook_inbox(delivery_guid, event, project,"
                    " requested_project, signature_verified, headers_json, payload,"
                    " status, attempts, result_json, received_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"guid-{label}-{status}", "push", P, P, 1, "{}", '{"p":1}',
                     status, 0, "{}", now - age_days * DAY, now))


def keys(table, col="idem_key"):
    with _conn(P) as c:
        return {r[0] for r in c.execute(f"SELECT {col} FROM {table}")}


try:
    store.init_db(P)
    now = time.time()

    # 1. A bare call must never write. Safe-by-default matters for a job that deletes.
    seed(now)
    report = ret.prune_operational_tables(project=P, now=now)
    ok(report["dry_run"] is True, "a bare call is a DRY RUN")
    ok(report["total_deleted_rows"] == 0, "the dry run deletes nothing")
    ok(report["total_eligible_rows"] > 0, "...but still reports what it would delete")

    # 2. Apply: old goes, fresh stays, in-flight stays regardless of age.
    seed(now)
    report = ret.prune_operational_tables(project=P, now=now, dry_run=False)
    idem = keys("idempotency_keys")
    ok("idem-fresh" in idem, "a fresh idempotency key is kept (a retry could still land)")
    ok("idem-old" not in idem, "an aged-out idempotency key is deleted")

    hooks = keys("webhook_inbox", "delivery_guid")
    ok("guid-old-applied" not in hooks and "guid-old-ignored" not in hooks,
       "aged-out TERMINAL webhook rows are deleted")
    ok("guid-old-pending" in hooks,
       "an in-flight (pending) delivery survives even at 30 days old — never drop "
       "work that has not been applied")
    ok("guid-fresh-applied" in hooks and "guid-fresh-ignored" in hooks,
       "recent terminal webhook rows are kept inside the redelivery window")
    ok(report["tables"]["webhook_inbox"]["retained_non_terminal"] >= 1,
       "the report states how many in-flight rows were deliberately retained")

    # 3. An unknown status must be RETAINED, not guessed at. If someone adds a new
    #    webhook state later, the sweep must not silently start deleting it.
    seed(now)
    with _conn(P) as c:
        c.execute("UPDATE webhook_inbox SET status='quarantined' "
                  "WHERE delivery_guid='guid-old-applied'")
    ret.prune_operational_tables(project=P, now=now, dry_run=False)
    ok("guid-old-applied" in keys("webhook_inbox", "delivery_guid"),
       "an UNKNOWN webhook status is retained, not treated as terminal")

    # 4. The window floor: a nonsense env value cannot collapse retention to zero.
    saved = os.environ.get(ret.IDEMPOTENCY_RETENTION_DAYS_ENV)
    os.environ[ret.IDEMPOTENCY_RETENTION_DAYS_ENV] = "0"
    try:
        ok(ret._retention_days(ret.IDEMPOTENCY_RETENTION_DAYS_ENV, 7.0)
           == ret.MIN_RETENTION_DAYS,
           "a 0-day retention setting is clamped to the 1-day floor")
        seed(now)
        ret.prune_operational_tables(project=P, now=now, dry_run=False)
        ok("idem-fresh" in keys("idempotency_keys"),
           "even at the floor, a key from 12 hours ago survives")
    finally:
        os.environ.pop(ret.IDEMPOTENCY_RETENTION_DAYS_ENV, None)
        if saved is not None:
            os.environ[ret.IDEMPOTENCY_RETENTION_DAYS_ENV] = saved

    # 5. Reachable through the store facade, which is how the job calls it.
    ok(callable(store.prune_operational_tables),
       "prune_operational_tables is exported on store for jobs.retention_sweep")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
