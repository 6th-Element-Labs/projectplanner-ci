"""DDL for the decision corpus (COORD-50, docs/DECISION-CORPUS-SPEC.md §4.3).

``decision_records`` is append-only and carries no authority: it gates nothing,
routes nothing, and cannot block or unblock work. It is storage in exactly the sense
ADR-0006 already blesses for the activity log and ``git_state``.

Two halves in one row:

* the **private half** (``snapshot_json``, ``decision_json``, ``classifier_version``,
  and the identity/routing columns) is the replay substrate and never leaves the
  tenant;
* the **projected half** (``reason_code``, ``route``, ``desired_role``,
  ``features_json``, ``features_version``) is materialized at write time against
  ``switchboard.decision_features.v1`` and is the only thing an export may read.

``UNIQUE(project, task_id, snapshot_hash)`` is what makes a row an *episode* rather
than a tick: the completion driver polls an unchanged head repeatedly, and without
this a task stuck for two hundred ticks would dominate every count and turn the
corpus into a measure of polling frequency.

``deliverable_id`` and ``host_id`` are private-half columns. They exist so counts can
be scoped to one deliverable or one host, and they are named in the export-exclusion
test for the same reason head SHAs and PR numbers are: they identify the environment.
"""
from __future__ import annotations


DECISION_RECORDS_SQL = (
    "CREATE TABLE IF NOT EXISTS decision_records ("
    "record_id TEXT PRIMARY KEY, "
    "project TEXT NOT NULL, "
    # identity / fencing
    "task_id TEXT NOT NULL, "
    "pr_number INTEGER NOT NULL DEFAULT 0, "
    "head_sha TEXT NOT NULL DEFAULT '', "
    "generation INTEGER NOT NULL DEFAULT 0, "
    "fence_epoch INTEGER NOT NULL DEFAULT 0, "
    "deliverable_id TEXT NOT NULL DEFAULT '', "
    "host_id TEXT NOT NULL DEFAULT '', "
    # private half: replay substrate, never exported
    "snapshot_hash TEXT NOT NULL, "
    "snapshot_json TEXT NOT NULL DEFAULT '{}', "
    "decision_json TEXT NOT NULL DEFAULT '{}', "
    "classifier_version TEXT NOT NULL, "
    # projected half: materialized at write time, export-safe
    "reason_code TEXT NOT NULL DEFAULT '', "
    "route TEXT NOT NULL DEFAULT '', "
    "desired_role TEXT NOT NULL DEFAULT '', "
    "features_json TEXT NOT NULL DEFAULT '{}', "
    "features_version TEXT NOT NULL, "
    # off-policy hygiene (spec §7): NULL = control arm, no advice was live
    "advice_version TEXT, "
    # episode accounting
    "tick_count INTEGER NOT NULL DEFAULT 1, "
    "first_seen_at REAL NOT NULL, "
    "last_seen_at REAL NOT NULL, "
    # outcome, backfilled by reconcile / webhook
    "outcome TEXT NOT NULL DEFAULT 'open', "
    "head_advanced INTEGER NOT NULL DEFAULT 0, "
    "generations_spent INTEGER NOT NULL DEFAULT 0, "
    "merged_sha TEXT NOT NULL DEFAULT '', "
    "human_intervened INTEGER NOT NULL DEFAULT 0, "
    "human_action TEXT NOT NULL DEFAULT '', "
    "snapshot_retained INTEGER NOT NULL DEFAULT 1, "
    "UNIQUE(project, task_id, snapshot_hash))"
)

__all__ = ["DECISION_RECORDS_SQL"]
