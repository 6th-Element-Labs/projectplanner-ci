"""Backward-compatible shim — prefer ``switchboard.storage.repositories.provenance``."""
import scripts.switchboard_path  # noqa: F401 — make src/switchboard importable

from switchboard.storage.repositories.provenance import (  # noqa: E402
    StoreProvenanceRepository,
    close_stale_reconcile_alert_inbox,
    default_provenance_repository,
    github_webhook_deliveries,
    mark_task_default_branch_commit,
    mark_task_merged,
    mark_task_offline_done,
    mark_task_pr_opened,
    reconcile,
    retire_merged_branch,
    run_reconcile_alerts,
    update_canonical_main_sha,
)

__all__ = [
    "StoreProvenanceRepository",
    "default_provenance_repository",
    "mark_task_pr_opened",
    "mark_task_merged",
    "mark_task_default_branch_commit",
    "mark_task_offline_done",
    "github_webhook_deliveries",
    "update_canonical_main_sha",
    "retire_merged_branch",
    "reconcile",
    "run_reconcile_alerts",
    "close_stale_reconcile_alert_inbox",
]
