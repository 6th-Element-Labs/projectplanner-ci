"""Durable Mission Bot v4 item and append-only event journal."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from db.connection import _conn

STATES = frozenset({"ACTIVE", "WAITING", "HUMAN", "DONE"})
ROLES = frozenset({"implementation", "review_merge", "remediation"})
TERMINAL_KINDS = frozenset({"github_merge", "offline"})
DONE_ACTORS = frozenset({"canonical_provenance_projector", "offline_verifier"})


class MissionJournalError(ValueError):
    """A command violated the v4 journal contract."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class MissionJournalRepository:
    def __init__(self, connector: Callable[[str], Any] = _conn):
        self._connector = connector

    @contextmanager
    def _connection(self, project: str) -> Iterator[sqlite3.Connection]:
        with self._connector(project) as connection:
            connection.row_factory = sqlite3.Row
            yield connection

    def get_item(self, task_id: str, *, project: str) -> dict[str, Any] | None:
        with self._connection(project) as c:
            item = _row(c.execute(
                "SELECT * FROM mission_items WHERE project_id=? AND task_id=?",
                (project, task_id),
            ).fetchone())
            if item is not None:
                item["latest_sequence"] = int(c.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM mission_events "
                    "WHERE project_id=? AND task_id=?",
                    (project, task_id),
                ).fetchone()[0])
            return item

    def task_ids_for_head(self, head_sha: str, *, project: str) -> list[str]:
        if not head_sha:
            return []
        with self._connection(project) as c:
            rows = c.execute(
                "SELECT task_id FROM task_git_state WHERE lower(head_sha)=lower(?)",
                (head_sha,),
            ).fetchall()
        return [str(row["task_id"]) for row in rows]

    def active_task_ids(self, *, project: str) -> list[str]:
        with self._connection(project) as c:
            rows = c.execute(
                "SELECT task_id FROM mission_items WHERE project_id=? AND state<>'DONE'",
                (project,),
            ).fetchall()
        return [str(row["task_id"]) for row in rows]

    def waiting_items_due(self, *, project: str, due_before: float) -> list[dict[str, Any]]:
        with self._connection(project) as c:
            rows = c.execute(
                "SELECT task_id,updated_at FROM mission_items "
                "WHERE project_id=? AND state='WAITING' AND updated_at<=?",
                (project, due_before),
            ).fetchall()
        return [dict(row) for row in rows]

    def ensure_item(
        self, task_id: str, *, project: str, requested_role: str = "implementation",
        state: str = "ACTIVE", now: float | None = None,
    ) -> dict[str, Any]:
        self._validate(state=state, requested_role=requested_role)
        timestamp = time.time() if now is None else now
        with self._connection(project) as c:
            c.execute(
                "INSERT OR IGNORE INTO mission_items("
                "project_id,task_id,state,requested_role,created_at,updated_at"
                ") VALUES (?,?,?,?,?,?)",
                (project, task_id, state, requested_role, timestamp, timestamp),
            )
        return self.get_item(task_id, project=project) or {}

    def append_event(
        self, task_id: str, *, project: str, event_type: str, source_plane: str,
        idempotency_key: str, occurred_at: float | None = None,
        pr_number: int | None = None, head_sha: str | None = None,
        generation: int | None = None, execution_id: str | None = None,
        external_ref: str = "", payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not idempotency_key.strip():
            raise MissionJournalError("idempotency_key_required", "idempotency key is required")
        timestamp = time.time() if occurred_at is None else occurred_at
        payload_json = json.dumps(dict(payload or {}), sort_keys=True, separators=(",", ":"))
        with self._connection(project) as c:
            c.execute("BEGIN IMMEDIATE")
            duplicate = c.execute(
                "SELECT * FROM mission_events WHERE project_id=? AND idempotency_key=?",
                (project, idempotency_key),
            ).fetchone()
            if duplicate is not None:
                return {**dict(duplicate), "payload": json.loads(duplicate["payload_json"]),
                        "created": False}
            sequence = int(c.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM mission_events "
                "WHERE project_id=? AND task_id=?",
                (project, task_id),
            ).fetchone()[0])
            event_id = f"missionevent-{uuid.uuid4().hex}"
            c.execute(
                "INSERT INTO mission_events(event_id,project_id,task_id,sequence,event_type,"
                "source_plane,occurred_at,pr_number,head_sha,generation,execution_id,"
                "external_ref,payload_json,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, project, task_id, sequence, event_type, source_plane, timestamp,
                 pr_number, head_sha, generation, execution_id, external_ref, payload_json,
                 idempotency_key),
            )
            return {
                "event_id": event_id, "project_id": project, "task_id": task_id,
                "sequence": sequence, "event_type": event_type, "source_plane": source_plane,
                "occurred_at": timestamp, "pr_number": pr_number, "head_sha": head_sha,
                "generation": generation, "execution_id": execution_id,
                "external_ref": external_ref, "payload_json": payload_json,
                "payload": dict(payload or {}), "idempotency_key": idempotency_key,
                "created": True,
            }

    def update_item(
        self, task_id: str, *, project: str, state: str, requested_role: str,
        expected_version: int, handled_through: int | None = None,
        human_request_id: str = "", terminal_kind: str = "", terminal_ref: str = "",
        authority: str = "", now: float | None = None,
    ) -> dict[str, Any]:
        self._validate(state=state, requested_role=requested_role)
        if state == "DONE":
            if authority not in DONE_ACTORS or terminal_kind not in TERMINAL_KINDS or not terminal_ref:
                raise MissionJournalError(
                    "done_authority_required",
                    "DONE requires canonical provenance or privileged offline verification",
                )
        elif terminal_kind or terminal_ref:
            raise MissionJournalError(
                "terminal_provenance_without_done",
                "terminal provenance may only be stored with DONE",
            )
        timestamp = time.time() if now is None else now
        with self._connection(project) as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                "SELECT * FROM mission_items WHERE project_id=? AND task_id=?",
                (project, task_id),
            ).fetchone()
            if current is None:
                raise MissionJournalError("mission_not_found", "mission item does not exist")
            if int(current["version"]) != expected_version:
                raise MissionJournalError(
                    "stale_generation",
                    f"expected version {expected_version}, current version {current['version']}",
                )
            cursor = int(current["handled_through"] if handled_through is None else handled_through)
            latest = int(c.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM mission_events "
                "WHERE project_id=? AND task_id=?",
                (project, task_id),
            ).fetchone()[0])
            if cursor < 0 or cursor > latest:
                raise MissionJournalError("invalid_event_cursor", "handled_through exceeds history")
            c.execute(
                "UPDATE mission_items SET state=?,requested_role=?,handled_through=?,"
                "version=version+1,human_request_id=?,terminal_kind=?,terminal_ref=?,updated_at=? "
                "WHERE project_id=? AND task_id=? AND version=?",
                (state, requested_role, cursor, human_request_id, terminal_kind, terminal_ref,
                 timestamp, project, task_id, expected_version),
            )
        return self.get_item(task_id, project=project) or {}

    @staticmethod
    def _validate(*, state: str, requested_role: str) -> None:
        if state not in STATES:
            raise MissionJournalError("invalid_state", f"unsupported mission state: {state}")
        if requested_role not in ROLES:
            raise MissionJournalError("invalid_role", f"unsupported requested role: {requested_role}")


default_mission_journal_repository = MissionJournalRepository()
