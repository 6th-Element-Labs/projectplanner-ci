"""Coordination terminal status sets shared by cleanup and receipts."""
from __future__ import annotations

TERMINAL_RECEIPT_STATUSES = frozenset({"done", "void", "superseded"})
TERMINAL_WAKE_STATUSES = frozenset({"completed", "failed", "cancelled"})
TERMINAL_RUNNER_STATUSES = frozenset({"exited", "killed", "failed", "completed", "expired"})
