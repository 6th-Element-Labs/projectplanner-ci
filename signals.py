"""Compatibility facade for the port-driven Coord plan-signal projection.

The pure projection lives in ``switchboard.services.coord.signals`` so the
standalone Coord boundary and the monolith cannot drift.  This root module
retains the established cache and monkeypatch surface for existing callers.
"""
from __future__ import annotations

from typing import Any

import store

from switchboard.services.coord.signals import compute_plan_signals as _compute_from_port


class _StoreSignalData:
    def list_tasks(self, project: str) -> list[dict[str, Any]]:
        return list(store.list_tasks(project=project))

    def get_meta(self, key: str, default: Any, project: str) -> Any:
        return store.get_meta(key, default, project=project)


_DATA = _StoreSignalData()


def compute_plan_signals(due_soon_days: int = 7, project: str = "maxwell") -> dict:
    """Cached plan-health projection shared with the Coord process boundary."""
    return store.ttl_read_cache(
        "plan_signals",
        f"{project}\x00{due_soon_days}",
        store.project_task_stamp(project),
        lambda: _compute_plan_signals(due_soon_days=due_soon_days, project=project),
    )


def _compute_plan_signals(due_soon_days: int = 7, project: str = "maxwell") -> dict:
    """Uncached compatibility hook retained for tests and diagnostic callers."""
    return _compute_from_port(_DATA, project=project, due_soon_days=due_soon_days)
