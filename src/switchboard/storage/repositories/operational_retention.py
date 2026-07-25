"""Age out operational scratch tables so they stop growing without bound.

`idempotency_keys` and `webhook_inbox` are the machinery's short-term memory, not
business truth — tasks, decisions, PRs and review verdicts live elsewhere. Neither had
any expiry, so both accumulated forever: 134 MB and 127 MB respectively, ~30% of the
database, on a box with less RAM than the database.

The windows below are chosen against the longest interval in which a duplicate could
still legitimately arrive, not picked for convenience:

* **idempotency_keys — 7 days.** The key exists so a repeated request is recognised
  rather than re-executed. The longest structural window in the system is one hour
  (EXTERNAL_CI_EXECUTION_LEASE_SECONDS, MAX_LEASE_TTL_SECONDS, DEFAULT_ABSOLUTE_TTL_SECONDS),
  with a 30-minute CI poll timeout under it. Seven days is ~168x that, so a key old
  enough to be dropped cannot still be protecting an in-flight retry.

* **webhook_inbox — 14 days, terminal rows only.** This table's unique delivery_guid IS
  the dedupe: drop a row and a redelivery of that guid would be re-enqueued and
  re-applied. GitHub's automatic retries are exhausted within about a day, so 14 days is
  well clear of them, and `_apply_row` routes through handlers that are idempotent by
  design as the backstop. Manual redelivery can still be initiated up to 30 days later
  from the GitHub UI — that is an operator action, and re-applying is safe, but it is why
  this window is deliberately longer than the idempotency one.

A row is never removed while it is still in flight: only terminal webhook rows are
eligible, so a `pending` delivery survives regardless of age. Nothing here touches
business history.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from db.connection import _conn
from .access import has_project


DEFAULT_PROJECT = "maxwell"
DAY_SECONDS = 86400.0

# Env-tunable so the window can be tightened or relaxed without a deploy.
IDEMPOTENCY_RETENTION_DAYS_ENV = "PM_IDEMPOTENCY_RETENTION_DAYS"
WEBHOOK_INBOX_RETENTION_DAYS_ENV = "PM_WEBHOOK_INBOX_RETENTION_DAYS"
DEFAULT_IDEMPOTENCY_RETENTION_DAYS = 7.0
DEFAULT_WEBHOOK_INBOX_RETENTION_DAYS = 14.0

# `pending` is the only non-terminal state webhook_inbox writes. Enumerate what may be
# deleted rather than what may not, so a NEW status added later is retained by default
# instead of silently becoming eligible for deletion.
WEBHOOK_INBOX_TERMINAL_STATUSES = ("applied", "ignored")

# Floor: never accept a window so short that it could delete a key still guarding an
# in-flight retry, even if someone sets the env var to 0.
MIN_RETENTION_DAYS = 1.0


def _retention_days(env_name: str, default_days: float) -> float:
    raw = str(os.environ.get(env_name) or "").strip()
    if not raw:
        return default_days
    try:
        value = float(raw)
    except ValueError:
        return default_days
    return max(MIN_RETENTION_DAYS, value)


def prune_operational_tables(project: str = DEFAULT_PROJECT,
                             now: Optional[float] = None,
                             idempotency_days: Optional[float] = None,
                             webhook_days: Optional[float] = None,
                             dry_run: bool = True,
                             actor: str = "system") -> Dict[str, Any]:
    """Delete aged-out operational scratch. Dry-run by default.

    Returns a per-table report of what is (or would be) removed, so a scheduled run can
    log real numbers instead of asserting success.
    """
    report: Dict[str, Any] = {
        "schema": "switchboard.operational_retention.v1",
        "project": project,
        "dry_run": bool(dry_run),
        "tables": {},
    }
    if not has_project(project):
        report["error"] = "unknown project"
        return report

    now = float(now if now is not None else time.time())
    idem_days = (idempotency_days if idempotency_days is not None
                 else _retention_days(IDEMPOTENCY_RETENTION_DAYS_ENV,
                                      DEFAULT_IDEMPOTENCY_RETENTION_DAYS))
    hook_days = (webhook_days if webhook_days is not None
                 else _retention_days(WEBHOOK_INBOX_RETENTION_DAYS_ENV,
                                      DEFAULT_WEBHOOK_INBOX_RETENTION_DAYS))
    idem_cutoff = now - idem_days * DAY_SECONDS
    hook_cutoff = now - hook_days * DAY_SECONDS

    with _conn(project) as c:
        # --- idempotency_keys -------------------------------------------------------
        row = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(response_json)), 0) "
            "FROM idempotency_keys WHERE created_at < ?", (idem_cutoff,)).fetchone()
        idem_stats = {"retention_days": idem_days, "cutoff": idem_cutoff,
                      "eligible_rows": row[0], "eligible_bytes": row[1], "deleted_rows": 0}
        if not dry_run and row[0]:
            idem_stats["deleted_rows"] = c.execute(
                "DELETE FROM idempotency_keys WHERE created_at < ?",
                (idem_cutoff,)).rowcount
        report["tables"]["idempotency_keys"] = idem_stats

        # --- webhook_inbox (terminal rows only) -------------------------------------
        placeholders = ",".join("?" for _ in WEBHOOK_INBOX_TERMINAL_STATUSES)
        params = (*WEBHOOK_INBOX_TERMINAL_STATUSES, hook_cutoff)
        row = c.execute(
            f"SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)), 0) FROM webhook_inbox "
            f"WHERE status IN ({placeholders}) AND received_at < ?", params).fetchone()
        hook_stats = {"retention_days": hook_days, "cutoff": hook_cutoff,
                      "terminal_statuses": list(WEBHOOK_INBOX_TERMINAL_STATUSES),
                      "eligible_rows": row[0], "eligible_bytes": row[1], "deleted_rows": 0}
        # Reported so a run can never be misread as "nothing left behind" when in-flight
        # deliveries were deliberately retained.
        hook_stats["retained_non_terminal"] = c.execute(
            f"SELECT COUNT(*) FROM webhook_inbox WHERE status NOT IN ({placeholders})",
            WEBHOOK_INBOX_TERMINAL_STATUSES).fetchone()[0]
        if not dry_run and row[0]:
            hook_stats["deleted_rows"] = c.execute(
                f"DELETE FROM webhook_inbox WHERE status IN ({placeholders}) "
                f"AND received_at < ?", params).rowcount
        report["tables"]["webhook_inbox"] = hook_stats

    report["total_eligible_rows"] = sum(
        t["eligible_rows"] for t in report["tables"].values())
    report["total_deleted_rows"] = sum(
        t["deleted_rows"] for t in report["tables"].values())
    report["total_eligible_bytes"] = sum(
        t["eligible_bytes"] for t in report["tables"].values())
    return report


__all__ = [
    "prune_operational_tables",
    "IDEMPOTENCY_RETENTION_DAYS_ENV",
    "WEBHOOK_INBOX_RETENTION_DAYS_ENV",
    "DEFAULT_IDEMPOTENCY_RETENTION_DAYS",
    "DEFAULT_WEBHOOK_INBOX_RETENTION_DAYS",
    "WEBHOOK_INBOX_TERMINAL_STATUSES",
    "MIN_RETENTION_DAYS",
]
