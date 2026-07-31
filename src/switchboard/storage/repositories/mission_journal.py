"""Dormant Mission Bot v4 persistence contract.

This repository stores a project-partitioned mission row and append-only
material-event history.  It is an audit/persistence substrate only: callers
must not treat these rows as runner liveness, coordination-scope authority,
task status, merge authority, or canonical Done provenance.

``mission_items.version`` is optimistic row concurrency only.  It is not an
ADR-0008 scope fence or an execution generation.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from db.connection import _conn, _write_through
from switchboard.domain.execution_liveness import TERMINAL_EXECUTION_STATES

STATES = frozenset({"ACTIVE", "WAITING", "HUMAN", "DONE"})
ROLES = frozenset({"implementation", "review_merge", "remediation"})
TERMINAL_KINDS = frozenset({"github_merge", "offline"})
EVENT_TYPES = frozenset({
    "mission_started",
    "task_changed",
    "github_changed",
    "runner_ended",
    "agent_yielded",
    "human_answered",
    "observation_due",
    "terminal_provenance_persisted",
})
EVENT_SOURCE_PLANES = {
    "mission_started": "coordination",
    "task_changed": "coordination",
    "github_changed": "communication",
    "runner_ended": "capacity",
    "agent_yielded": "coordination",
    "human_answered": "coordination",
    "observation_due": "coordination",
    "terminal_provenance_persisted": "coordination",
}
EVENT_PAYLOAD_KEYS = {
    "mission_started": frozenset({
        "scope_id", "scope_generation", "scope_fence", "start_ref",
    }),
    "task_changed": frozenset({
        "change_ref", "changed_fields", "dependency_ids", "command_ref",
    }),
    "github_changed": frozenset({
        "delivery_id", "repository", "event_action", "object_type",
        "object_id", "material_fingerprint", "status_context", "status_state",
        "target_url", "review_id", "review_state", "queue_entry_id",
        "queue_state", "merge_group_sha", "policy_ref",
    }),
    "runner_ended": frozenset({
        "runner_session_id", "terminal_status", "reason_code", "receipt_ref",
    }),
    "agent_yielded": frozenset({
        "outcome", "requested_role", "observed_through",
        "latest_sequence_at_yield", "cursor_current",
    }),
    "human_answered": frozenset({"human_request_id", "answer_ref"}),
    "observation_due": frozenset({"wait_started_at", "due_at"}),
    "terminal_provenance_persisted": frozenset({
        "terminal_kind", "terminal_ref",
    }),
}


class MissionJournalError(ValueError):
    """A command violated the dormant mission-journal contract."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class MissionJournalRepository:
    """Persist mission evidence without acquiring lifecycle authority."""

    def __init__(
        self,
        connector: Callable[[str], Any] = _conn,
        write_through: Callable[[str, Callable[[], Any]], Any] = _write_through,
        terminal_verifier: Callable[[str, str, str, str], bool] | None = None,
    ):
        self._connector = connector
        self._write_through = write_through
        self._terminal_verifier = terminal_verifier

    @contextmanager
    def _connection(self, project: str) -> Iterator[sqlite3.Connection]:
        with self._connector(project) as connection:
            connection.row_factory = sqlite3.Row
            yield connection

    @staticmethod
    def _item_in(
        connection: sqlite3.Connection, task_id: str, project: str,
    ) -> dict[str, Any] | None:
        item = _row(connection.execute(
            "SELECT * FROM mission_items WHERE project_id=? AND task_id=?",
            (project, task_id),
        ).fetchone())
        if item is not None:
            item["latest_sequence"] = int(connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM mission_events "
                "WHERE project_id=? AND task_id=?",
                (project, task_id),
            ).fetchone()[0])
        return item

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
        event = dict(row)
        event["payload"] = json.loads(event["payload_json"] or "{}")
        return event

    @classmethod
    def _append_event_in(
        cls,
        connection: sqlite3.Connection,
        task_id: str,
        *,
        project: str,
        event_type: str,
        source_plane: str,
        idempotency_key: str,
        occurred_at: float,
        pr_number: int | None,
        head_sha: str | None,
        generation: int | None,
        execution_id: str | None,
        external_ref: str,
        payload: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        cls._validate_event(
            event_type=event_type,
            source_plane=source_plane,
            pr_number=pr_number,
            head_sha=head_sha,
            generation=generation,
            execution_id=execution_id,
            external_ref=external_ref,
            payload=payload,
        )
        if not idempotency_key.strip():
            raise MissionJournalError(
                "idempotency_key_required", "idempotency key is required",
            )
        item = connection.execute(
            "SELECT 1 FROM mission_items WHERE project_id=? AND task_id=?",
            (project, task_id),
        ).fetchone()
        if item is None:
            raise MissionJournalError(
                "mission_not_found",
                "mission events require an existing mission item",
            )
        payload_json = json.dumps(
            dict(payload or {}), sort_keys=True, separators=(",", ":"),
        )
        duplicate = connection.execute(
            "SELECT * FROM mission_events WHERE project_id=? AND idempotency_key=?",
            (project, idempotency_key),
        ).fetchone()
        if duplicate is not None:
            expected = (
                task_id,
                event_type,
                source_plane,
                pr_number,
                head_sha,
                generation,
                execution_id,
                external_ref,
                payload_json,
            )
            persisted = (
                duplicate["task_id"],
                duplicate["event_type"],
                duplicate["source_plane"],
                duplicate["pr_number"],
                duplicate["head_sha"],
                duplicate["generation"],
                duplicate["execution_id"],
                duplicate["external_ref"],
                duplicate["payload_json"],
            )
            if expected != persisted:
                raise MissionJournalError(
                    "idempotency_conflict",
                    "idempotency key already identifies a different mission event",
                )
            return {**cls._event_from_row(duplicate), "created": False}
        sequence = int(connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM mission_events "
            "WHERE project_id=? AND task_id=?",
            (project, task_id),
        ).fetchone()[0])
        event_id = f"missionevent-{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO mission_events(event_id,project_id,task_id,sequence,event_type,"
            "source_plane,occurred_at,pr_number,head_sha,generation,execution_id,"
            "external_ref,payload_json,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, project, task_id, sequence, event_type, source_plane,
                occurred_at, pr_number, head_sha, generation, execution_id,
                external_ref, payload_json, idempotency_key,
            ),
        )
        return {
            "event_id": event_id,
            "project_id": project,
            "task_id": task_id,
            "sequence": sequence,
            "event_type": event_type,
            "source_plane": source_plane,
            "occurred_at": occurred_at,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "generation": generation,
            "execution_id": execution_id,
            "external_ref": external_ref,
            "payload_json": payload_json,
            "payload": dict(payload or {}),
            "idempotency_key": idempotency_key,
            "created": True,
        }

    def get_item(self, task_id: str, *, project: str) -> dict[str, Any] | None:
        with self._connection(project) as connection:
            return self._item_in(connection, task_id, project)

    def task_ids_for_head(self, head_sha: str, *, project: str) -> list[str]:
        """Return tasks whose canonical git projection names this exact head."""
        normalized = str(head_sha or "").strip()
        if not normalized:
            return []
        with self._connection(project) as connection:
            rows = connection.execute(
                "SELECT task_id FROM task_git_state WHERE lower(head_sha)=lower(?)",
                (normalized,),
            ).fetchall()
        return [str(row["task_id"]) for row in rows]

    def task_ids_for_pr_number(self, pr_number: int, *, project: str) -> list[str]:
        """Return tasks bound to one canonical pull-request number."""
        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
            return []
        with self._connection(project) as connection:
            rows = connection.execute(
                "SELECT task_id FROM task_git_state WHERE pr_number=?",
                (pr_number,),
            ).fetchall()
        return [str(row["task_id"]) for row in rows]

    def active_task_ids(self, *, project: str) -> list[str]:
        """Return existing nonterminal missions for repository-wide observations."""
        with self._connection(project) as connection:
            rows = connection.execute(
                "SELECT task_id FROM mission_items "
                "WHERE project_id=? AND state<>'DONE' ORDER BY task_id",
                (project,),
            ).fetchall()
        return [str(row["task_id"]) for row in rows]

    def waiting_items_due(
        self,
        *,
        project: str,
        due_before: float,
        task_id: str = "",
    ) -> list[dict[str, Any]]:
        """Read persisted waits eligible for the passive observation backstop."""
        where = (
            "m.project_id=? AND m.state='WAITING' AND m.updated_at<=? "
            "AND NOT EXISTS ("
            "SELECT 1 FROM mission_events e "
            "WHERE e.project_id=m.project_id AND e.task_id=m.task_id "
            "AND e.occurred_at>m.updated_at)"
        )
        params: list[Any] = [project, due_before]
        if task_id:
            where += " AND m.task_id=?"
            params.append(task_id)
        with self._connection(project) as connection:
            rows = connection.execute(
                f"SELECT m.task_id,m.updated_at FROM mission_items m WHERE {where}",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_events(
        self,
        task_id: str,
        *,
        project: str,
        after_sequence: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return one bounded, forward-only page of persisted evidence."""
        if isinstance(after_sequence, bool):
            raise MissionJournalError(
                "invalid_event_cursor", "after_sequence must be nonnegative",
            )
        try:
            cursor = int(after_sequence)
            page_size = int(limit)
        except (TypeError, ValueError) as exc:
            raise MissionJournalError(
                "invalid_history_page", "history cursor and limit must be integers",
            ) from exc
        if cursor < 0:
            raise MissionJournalError(
                "invalid_event_cursor", "after_sequence must be nonnegative",
            )
        page_size = max(1, min(page_size, 201))
        with self._connection(project) as connection:
            rows = connection.execute(
                "SELECT * FROM mission_events WHERE project_id=? AND task_id=? "
                "AND sequence>? ORDER BY sequence ASC LIMIT ?",
                (project, task_id, cursor, page_size),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def yield_execution(
        self,
        task_id: str,
        *,
        project: str,
        execution_id: str,
        generation: int,
        observed_through: int,
        outcome: str,
        requested_role: str,
        actor: str,
        head_sha: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Record an authenticated exact-execution yield.

        This coordination write does not stop a process.  The application
        command separately asks Capacity to expire this exact execution lease.
        A stale history cursor remains ACTIVE so it cannot hide a newer event.
        """
        self._validate(state="ACTIVE", requested_role=requested_role)
        normalized_outcome = str(outcome or "").strip().lower()
        if normalized_outcome not in {"continue", "waiting"}:
            raise MissionJournalError(
                "invalid_outcome", "outcome must be continue or waiting",
            )
        if isinstance(generation, bool) or isinstance(observed_through, bool):
            raise MissionJournalError(
                "invalid_execution_identity", "generation and cursor must be integers",
            )
        try:
            exact_generation = int(generation)
            cursor = int(observed_through)
        except (TypeError, ValueError) as exc:
            raise MissionJournalError(
                "invalid_execution_identity", "generation and cursor must be integers",
            ) from exc
        if exact_generation <= 0 or cursor < 0:
            raise MissionJournalError(
                "invalid_execution_identity",
                "generation must be positive and cursor nonnegative",
            )
        exact_execution_id = str(execution_id or "").strip()
        exact_actor = str(actor or "").strip()
        if not exact_execution_id or not exact_actor:
            raise MissionJournalError(
                "exact_execution_identity_required",
                "execution_id and authenticated actor are required",
            )
        timestamp = time.time() if now is None else now

        def write() -> dict[str, Any]:
            with self._connection(project) as connection:
                connection.execute("BEGIN IMMEDIATE")
                item = connection.execute(
                    "SELECT * FROM mission_items WHERE project_id=? AND task_id=?",
                    (project, task_id),
                ).fetchone()
                if item is None:
                    raise MissionJournalError(
                        "mission_not_found", "mission item does not exist",
                    )
                runners = connection.execute(
                    "SELECT * FROM runner_sessions WHERE task_id=? "
                    "AND status IN ('running','stopping') "
                    "ORDER BY started_at DESC, runner_session_id DESC",
                    (task_id,),
                ).fetchall()
                if not runners:
                    raise MissionJournalError(
                        "current_execution_required",
                        "no current execution is authenticated",
                    )
                if len(runners) != 1:
                    raise MissionJournalError(
                        "ambiguous_current_execution",
                        "more than one current execution is registered for this task",
                    )
                runner = runners[0]
                metadata = json.loads(runner["metadata_json"] or "{}")
                live_execution_id = str(metadata.get("execution_id") or "")
                live_generation = int(metadata.get("execution_generation") or 0)
                live_head = str(metadata.get("execution_head_sha") or "").strip()
                live_role = str(
                    metadata.get("execution_role") or metadata.get("role") or ""
                ).strip().lower()
                if (
                    exact_execution_id != live_execution_id
                    or exact_generation != live_generation
                    or exact_actor != str(runner["agent_id"] or "")
                ):
                    raise MissionJournalError(
                        "stale_execution",
                        "yield does not match the authenticated current execution",
                    )
                supplied_head = str(head_sha or "").strip()
                if live_head and supplied_head != live_head:
                    raise MissionJournalError(
                        "stale_head", "yield head does not match assignment head",
                    )
                lease = connection.execute(
                    "SELECT * FROM resource_leases WHERE id=? "
                    "AND resource_type='execution'",
                    (exact_execution_id,),
                ).fetchone()
                if (
                    lease is None
                    or int(lease["execution_generation"] or 0) != live_generation
                ):
                    raise MissionJournalError(
                        "execution_lease_required",
                        "current execution lease is unavailable",
                    )
                identity = {
                    "runner_session_id": str(runner["runner_session_id"] or ""),
                    "execution_id": exact_execution_id,
                    "execution_connection_id": str(
                        metadata.get("execution_connection_id") or ""
                    ),
                    "generation": live_generation,
                    "fence_epoch": int(lease["fence_epoch"] or 0),
                    "role": live_role,
                    "head_sha": live_head,
                }
                latest = int(connection.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM mission_events "
                    "WHERE project_id=? AND task_id=?",
                    (project, task_id),
                ).fetchone()[0])
                if cursor > latest:
                    raise MissionJournalError(
                        "invalid_event_cursor", "observed_through exceeds history",
                    )
                cursor_current = cursor == latest
                idempotency_key = (
                    f"yield:{task_id}:{exact_execution_id}:{exact_generation}:"
                    f"{cursor}:{normalized_outcome}:{requested_role}"
                )
                duplicate = connection.execute(
                    "SELECT * FROM mission_events WHERE project_id=? "
                    "AND idempotency_key=?",
                    (project, idempotency_key),
                ).fetchone()
                if duplicate is not None:
                    event = self._event_from_row(duplicate)
                    if (
                        str(event.get("task_id") or "") != task_id
                        or str(event.get("execution_id") or "") != exact_execution_id
                        or int(event.get("generation") or 0) != exact_generation
                        or str(event.get("head_sha") or "") != supplied_head
                    ):
                        raise MissionJournalError(
                            "idempotency_conflict",
                            "yield key already identifies a different mission event",
                        )
                    persisted_payload = dict(event.get("payload") or {})
                    return {
                        "schema": "switchboard.mission_yield.v4",
                        "task_id": task_id,
                        "execution_id": exact_execution_id,
                        "generation": exact_generation,
                        "outcome": normalized_outcome,
                        "observed_through": cursor,
                        "latest_sequence": int(
                            persisted_payload.get("latest_sequence_at_yield") or 0
                        ),
                        "cursor_current": bool(
                            persisted_payload.get("cursor_current")
                        ),
                        "state": "ACTIVE",
                        "pending_state": (
                            "WAITING"
                            if normalized_outcome == "waiting"
                            and bool(persisted_payload.get("cursor_current"))
                            else None
                        ),
                        "event_id": event["event_id"],
                        "created": False,
                        "execution_identity": identity,
                    }
                event = self._append_event_in(
                    connection,
                    task_id,
                    project=project,
                    event_type="agent_yielded",
                    source_plane="coordination",
                    idempotency_key=idempotency_key,
                    occurred_at=timestamp,
                    pr_number=None,
                    head_sha=supplied_head or None,
                    generation=exact_generation,
                    execution_id=exact_execution_id,
                    external_ref="",
                    payload={
                        "outcome": normalized_outcome,
                        "requested_role": requested_role,
                        "observed_through": cursor,
                        "latest_sequence_at_yield": latest,
                        "cursor_current": cursor_current,
                    },
                )
                if event["created"]:
                    handled = (
                        int(event["sequence"])
                        if cursor_current
                        else int(item["handled_through"] or 0)
                    )
                    connection.execute(
                        "UPDATE mission_items SET state='ACTIVE',requested_role=?,"
                        "handled_through=?,version=version+1,updated_at=? "
                        "WHERE project_id=? AND task_id=?",
                        (requested_role, handled, timestamp, project, task_id),
                    )
                persisted_payload = dict(event.get("payload") or {})
                return {
                    "schema": "switchboard.mission_yield.v4",
                    "task_id": task_id,
                    "execution_id": exact_execution_id,
                    "generation": exact_generation,
                    "outcome": normalized_outcome,
                    "observed_through": cursor,
                    "latest_sequence": int(
                        persisted_payload.get("latest_sequence_at_yield") or 0
                    ),
                    "cursor_current": bool(persisted_payload.get("cursor_current")),
                    "state": "ACTIVE",
                    "pending_state": (
                        "WAITING"
                        if normalized_outcome == "waiting"
                        and bool(persisted_payload.get("cursor_current"))
                        else None
                    ),
                    "event_id": event["event_id"],
                    "created": bool(event["created"]),
                    "execution_identity": identity,
                }

        return self._write_through(project, write)

    def create_mission(
        self,
        task_id: str,
        *,
        project: str,
        requested_role: str = "implementation",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Atomically ensure the row and its one ``mission_started`` event."""
        self._validate(state="ACTIVE", requested_role=requested_role)
        timestamp = time.time() if now is None else now

        def write() -> dict[str, Any]:
            with self._connection(project) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO mission_items("
                    "project_id,task_id,state,requested_role,created_at,updated_at"
                    ") VALUES (?,?,?,?,?,?)",
                    (
                        project, task_id, "ACTIVE", requested_role,
                        timestamp, timestamp,
                    ),
                )
                event = self._append_event_in(
                    connection,
                    task_id,
                    project=project,
                    event_type="mission_started",
                    source_plane="coordination",
                    idempotency_key=f"mission_started:{task_id}",
                    occurred_at=timestamp,
                    pr_number=None,
                    head_sha=None,
                    generation=None,
                    execution_id=None,
                    external_ref="",
                    payload=None,
                )
                item = self._item_in(connection, task_id, project)
                return {"mission": item or {}, "event": event}

        return self._write_through(project, write)

    def ensure_item(
        self,
        task_id: str,
        *,
        project: str,
        requested_role: str = "implementation",
        state: str = "ACTIVE",
        now: float | None = None,
    ) -> dict[str, Any]:
        self._validate(state=state, requested_role=requested_role)
        if state != "ACTIVE":
            raise MissionJournalError(
                "invalid_initial_state", "new mission items must start ACTIVE",
            )
        return self.create_mission(
            task_id,
            project=project,
            requested_role=requested_role,
            now=now,
        )["mission"]

    def append_event(
        self,
        task_id: str,
        *,
        project: str,
        event_type: str,
        source_plane: str,
        idempotency_key: str,
        occurred_at: float | None = None,
        pr_number: int | None = None,
        head_sha: str | None = None,
        generation: int | None = None,
        execution_id: str | None = None,
        external_ref: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if occurred_at is None else occurred_at
        if event_type == "terminal_provenance_persisted":
            detail = dict(payload or {})
            terminal_kind = str(detail.get("terminal_kind") or "")
            terminal_ref = str(detail.get("terminal_ref") or "")
            if (
                self._terminal_verifier is None
                or not self._terminal_verifier(
                    project, task_id, terminal_kind, terminal_ref,
                )
            ):
                raise MissionJournalError(
                    "terminal_provenance_unverified",
                    "terminal event requires already-persisted verified provenance",
                )

        def write() -> dict[str, Any]:
            with self._connection(project) as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self._append_event_in(
                    connection,
                    task_id,
                    project=project,
                    event_type=event_type,
                    source_plane=source_plane,
                    idempotency_key=idempotency_key,
                    occurred_at=timestamp,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    generation=generation,
                    execution_id=execution_id,
                    external_ref=external_ref,
                    payload=payload,
                )

        return self._write_through(project, write)

    def update_item(
        self,
        task_id: str,
        *,
        project: str,
        state: str,
        requested_role: str,
        expected_version: int,
        handled_through: int | None = None,
        human_request_id: str = "",
        terminal_kind: str = "",
        terminal_ref: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Update passive row state with row-CAS and provenance guardrails."""
        self._validate(state=state, requested_role=requested_role)
        if state == "DONE":
            if (
                terminal_kind not in TERMINAL_KINDS
                or not terminal_ref
            ):
                raise MissionJournalError(
                    "terminal_provenance_required",
                    "DONE requires a typed terminal-provenance reference",
                )
            if (
                self._terminal_verifier is None
                or not self._terminal_verifier(
                    project, task_id, terminal_kind, terminal_ref,
                )
            ):
                raise MissionJournalError(
                    "terminal_provenance_unverified",
                    "DONE requires already-persisted canonical or offline provenance",
                )
        elif terminal_kind or terminal_ref:
            raise MissionJournalError(
                "terminal_provenance_without_done",
                "terminal provenance may only be stored with DONE",
            )
        if state == "HUMAN" and not human_request_id.strip():
            raise MissionJournalError(
                "human_request_required", "HUMAN requires an explicit request reference",
            )
        if state != "HUMAN" and human_request_id:
            raise MissionJournalError(
                "human_request_without_human",
                "a Human request reference may only be stored with HUMAN",
            )
        timestamp = time.time() if now is None else now

        def write() -> dict[str, Any]:
            with self._connection(project) as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT * FROM mission_items WHERE project_id=? AND task_id=?",
                    (project, task_id),
                ).fetchone()
                if current is None:
                    raise MissionJournalError(
                        "mission_not_found", "mission item does not exist",
                    )
                if int(current["version"]) != expected_version:
                    raise MissionJournalError(
                        "stale_row_version",
                        f"expected row version {expected_version}, "
                        f"current version {current['version']}",
                    )
                if str(current["state"]) == "DONE":
                    raise MissionJournalError(
                        "terminal_state_immutable",
                        "persisted terminal provenance cannot be rewritten",
                    )
                cursor = int(
                    current["handled_through"]
                    if handled_through is None
                    else handled_through
                )
                latest = int(connection.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM mission_events "
                    "WHERE project_id=? AND task_id=?",
                    (project, task_id),
                ).fetchone()[0])
                if cursor < int(current["handled_through"]):
                    raise MissionJournalError(
                        "event_cursor_regression",
                        "handled_through cannot move backwards",
                    )
                if cursor < 0 or cursor > latest:
                    raise MissionJournalError(
                        "invalid_event_cursor", "handled_through exceeds history",
                    )
                connection.execute(
                    "UPDATE mission_items SET state=?,requested_role=?,handled_through=?,"
                    "version=version+1,human_request_id=?,terminal_kind=?,terminal_ref=?,"
                    "updated_at=? WHERE project_id=? AND task_id=? AND version=?",
                    (
                        state, requested_role, cursor, human_request_id,
                        terminal_kind, terminal_ref, timestamp, project, task_id,
                        expected_version,
                    ),
                )
                return self._item_in(connection, task_id, project) or {}

        return self._write_through(project, write)

    @staticmethod
    def _validate(*, state: str, requested_role: str) -> None:
        if state not in STATES:
            raise MissionJournalError(
                "invalid_state", f"unsupported mission state: {state}",
            )
        if requested_role not in ROLES:
            raise MissionJournalError(
                "invalid_role", f"unsupported requested role: {requested_role}",
            )

    @staticmethod
    def _validate_event(
        *,
        event_type: str,
        source_plane: str,
        pr_number: int | None,
        head_sha: str | None,
        generation: int | None,
        execution_id: str | None,
        external_ref: str,
        payload: Mapping[str, Any] | None,
    ) -> None:
        if event_type not in EVENT_TYPES:
            raise MissionJournalError(
                "invalid_event_type", f"unsupported mission event: {event_type}",
            )
        expected_plane = EVENT_SOURCE_PLANES[event_type]
        if source_plane != expected_plane:
            raise MissionJournalError(
                "invalid_source_plane",
                f"{event_type} must originate in the {expected_plane} plane",
            )
        detail = dict(payload or {})
        unknown = sorted(set(detail) - EVENT_PAYLOAD_KEYS[event_type])
        if unknown:
            raise MissionJournalError(
                "invalid_event_payload",
                f"{event_type} payload contains unsupported fields: "
                + ", ".join(unknown),
            )
        if pr_number is not None and (
            isinstance(pr_number, bool)
            or not isinstance(pr_number, int)
            or pr_number <= 0
        ):
            raise MissionJournalError(
                "invalid_pr_number", "pr_number must be a positive integer",
            )
        for field, value in (
            ("head_sha", head_sha),
            ("execution_id", execution_id),
            ("external_ref", external_ref),
        ):
            if value is not None and value != "" and not (
                isinstance(value, str) and value.strip()
            ):
                raise MissionJournalError(
                    "invalid_event_identity", f"{field} must be a nonempty string",
                )

        def nonempty_string(value: Any) -> bool:
            return isinstance(value, str) and bool(value.strip())

        def positive_int(value: Any) -> bool:
            return (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
            )

        def nonnegative_int(value: Any) -> bool:
            return (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            )

        def positive_number(value: Any) -> bool:
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value > 0
            )

        def string_list(value: Any) -> bool:
            return (
                isinstance(value, list)
                and all(nonempty_string(item) for item in value)
            )

        if event_type == "mission_started":
            for field in ("scope_id", "start_ref"):
                if field in detail and not nonempty_string(detail[field]):
                    raise MissionJournalError(
                        "invalid_start_reference", f"{field} must be a nonempty string",
                    )
            for field in ("scope_generation", "scope_fence"):
                if field in detail and not positive_int(detail[field]):
                    raise MissionJournalError(
                        "invalid_scope_identity", f"{field} must be a positive integer",
                    )
        if event_type == "task_changed":
            for field in ("change_ref", "command_ref"):
                if field in detail and not nonempty_string(detail[field]):
                    raise MissionJournalError(
                        "invalid_task_change", f"{field} must be a nonempty string",
                    )
            for field in ("changed_fields", "dependency_ids"):
                if field in detail and not string_list(detail[field]):
                    raise MissionJournalError(
                        "invalid_task_change", f"{field} must be a list of strings",
                    )
        if event_type == "github_changed":
            for field, value in detail.items():
                if isinstance(value, bool) or not isinstance(value, (str, int)):
                    raise MissionJournalError(
                        "invalid_github_evidence",
                        f"{field} must be a provider string or integer identity",
                    )
                if isinstance(value, str) and not value.strip():
                    raise MissionJournalError(
                        "invalid_github_evidence",
                        f"{field} must not be empty",
                    )
        if event_type == "task_changed" and not (
            external_ref or detail.get("change_ref") or detail.get("command_ref")
        ):
            raise MissionJournalError(
                "task_change_reference_required",
                "task_changed requires a durable change reference",
            )
        if event_type == "github_changed" and not (
            pr_number
            or head_sha
            or external_ref
            or detail.get("material_fingerprint")
            or detail.get("policy_ref")
        ):
            raise MissionJournalError(
                "github_identity_required",
                "github_changed requires provider object or delivery identity",
            )
        if event_type in {"runner_ended", "agent_yielded"}:
            if not nonempty_string(execution_id) or not positive_int(generation):
                raise MissionJournalError(
                    "exact_execution_identity_required",
                    f"{event_type} requires execution_id and positive generation",
                )
        if event_type == "runner_ended":
            if not nonempty_string(detail.get("runner_session_id")):
                raise MissionJournalError(
                    "runner_session_required",
                    "runner_ended requires a nonempty runner session",
                )
            if detail.get("terminal_status") not in TERMINAL_EXECUTION_STATES:
                raise MissionJournalError(
                    "invalid_runner_terminal_status",
                    "runner_ended requires a recognized terminal status",
                )
            for field in ("reason_code", "receipt_ref"):
                if field in detail and not nonempty_string(detail[field]):
                    raise MissionJournalError(
                        "invalid_runner_receipt", f"{field} must be a nonempty string",
                    )
        if event_type == "agent_yielded":
            if detail.get("outcome") not in {"continue", "waiting"}:
                raise MissionJournalError(
                    "invalid_yield_outcome", "agent yield outcome is invalid",
                )
            if detail.get("requested_role") not in ROLES:
                raise MissionJournalError(
                    "invalid_role", "agent yield requested_role is invalid",
                )
            if not nonnegative_int(detail.get("observed_through")):
                raise MissionJournalError(
                    "invalid_event_cursor", "observed_through must be nonnegative",
                )
            if (
                "latest_sequence_at_yield" in detail
                and not nonnegative_int(detail["latest_sequence_at_yield"])
            ):
                raise MissionJournalError(
                    "invalid_event_cursor",
                    "latest_sequence_at_yield must be nonnegative",
                )
            if "cursor_current" in detail and not isinstance(
                detail["cursor_current"], bool,
            ):
                raise MissionJournalError(
                    "invalid_event_cursor", "cursor_current must be boolean",
                )
        if event_type == "human_answered" and not (
            nonempty_string(detail.get("human_request_id"))
            and nonempty_string(detail.get("answer_ref"))
        ):
            raise MissionJournalError(
                "human_answer_reference_required",
                "human_answered requires request and answer references",
            )
        if event_type == "observation_due":
            waited = detail.get("wait_started_at")
            due_at = detail.get("due_at")
            if not positive_number(waited):
                raise MissionJournalError(
                    "wait_reference_required",
                    "observation_due requires a positive persisted wait timestamp",
                )
            if due_at is not None and (
                not positive_number(due_at) or due_at < waited
            ):
                raise MissionJournalError(
                    "invalid_observation_due",
                    "due_at must be at or after the persisted wait timestamp",
                )
        if event_type == "terminal_provenance_persisted":
            if (
                detail.get("terminal_kind") not in TERMINAL_KINDS
                or not nonempty_string(detail.get("terminal_ref"))
            ):
                raise MissionJournalError(
                    "terminal_provenance_required",
                    "terminal event requires typed terminal provenance",
                )


default_mission_journal_repository = MissionJournalRepository()
