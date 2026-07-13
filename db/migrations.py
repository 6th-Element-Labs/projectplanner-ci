"""Backward-compatible shim — prefer ``switchboard.storage.migrations``."""
import scripts.switchboard_path  # noqa: F401 — make src/switchboard importable

from switchboard.storage.migrations import (  # noqa: E402
    ADDITIVE_COLUMN_MIGRATIONS,
    DDL_MIGRATIONS,
    _is_duplicate_column,
    is_duplicate_column,
    run_additive_migrations,
)

__all__ = [
    "ADDITIVE_COLUMN_MIGRATIONS",
    "DDL_MIGRATIONS",
    "is_duplicate_column",
    "run_additive_migrations",
    "_is_duplicate_column",
]
