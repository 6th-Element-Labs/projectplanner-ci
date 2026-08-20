"""Durable, bounded Mission Bot v5 launch retry state."""
from __future__ import annotations

import time
from typing import Any

from db.connection import _conn, _write_through


class MissionLaunchAttemptRepository:
    def __init__(self, connector=_conn, write_through=_write_through):
        self._connector = connector
        self._write_through = write_through

    def get(self, task_id: str, *, project: str, mission_key: str) -> dict[str, Any] | None:
        with self._connector(project) as connection:
            row = connection.execute(
                "SELECT * FROM mission_launch_attempts "
                "WHERE project_id=? AND task_id=? AND mission_key=?",
                (project, task_id, mission_key),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_failure(
        self,
        task_id: str,
        *,
        project: str,
        mission_key: str,
        requested_role: str,
        reason: str,
        start_error: str,
        max_attempts: int,
        base_delay_seconds: int,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else float(now)

        def write() -> dict[str, Any]:
            with self._connector(project) as connection:
                prior = connection.execute(
                    "SELECT retry_count,created_at FROM mission_launch_attempts "
                    "WHERE project_id=? AND task_id=? AND mission_key=?",
                    (project, task_id, mission_key),
                ).fetchone()
                count = int(prior["retry_count"] if prior else 0) + 1
                exhausted = count >= max(1, int(max_attempts))
                delay = max(1, int(base_delay_seconds)) * (2 ** max(0, count - 1))
                next_retry_at = None if exhausted else timestamp + delay
                created_at = float(prior["created_at"] if prior else timestamp)
                connection.execute(
                    "INSERT INTO mission_launch_attempts("
                    "project_id,task_id,mission_key,requested_role,retry_count,reason,"
                    "start_error,next_retry_at,exhausted,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(project_id,task_id,mission_key) DO UPDATE SET "
                    "requested_role=excluded.requested_role,retry_count=excluded.retry_count,"
                    "reason=excluded.reason,start_error=excluded.start_error,"
                    "next_retry_at=excluded.next_retry_at,exhausted=excluded.exhausted,"
                    "updated_at=excluded.updated_at",
                    (
                        project, task_id, mission_key, requested_role, count, reason,
                        start_error, next_retry_at, int(exhausted), created_at, timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM mission_launch_attempts "
                    "WHERE project_id=? AND task_id=? AND mission_key=?",
                    (project, task_id, mission_key),
                ).fetchone()
                return dict(row)

        return self._write_through(project, write)

    def clear(self, task_id: str, *, project: str, mission_key: str) -> bool:
        def write() -> bool:
            with self._connector(project) as connection:
                deleted = connection.execute(
                    "DELETE FROM mission_launch_attempts "
                    "WHERE project_id=? AND task_id=? AND mission_key=?",
                    (project, task_id, mission_key),
                )
                return deleted.rowcount > 0

        return bool(self._write_through(project, write))


default_mission_launch_attempt_repository = MissionLaunchAttemptRepository()


__all__ = [
    "MissionLaunchAttemptRepository",
    "default_mission_launch_attempt_repository",
]
