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

    def waiting_items_due(
        self, *, project: str, due_before: float, task_id: str = "",
    ) -> list[dict[str, Any]]:
        where = "project_id=? AND state='WAITING' AND updated_at<=?"
        params: list[Any] = [project, due_before]
        if task_id:
            where += " AND task_id=?"
            params.append(task_id)
        with self._connection(project) as c:
            rows = c.execute(
                "SELECT task_id,updated_at FROM mission_items "
                f"WHERE {where}",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_events(
        self, task_id: str, *, project: str, after_sequence: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return one bounded, forward-only page of mission history."""
        cursor = max(0, int(after_sequence))
        page_size = max(1, min(int(limit), 200))
        with self._connection(project) as c:
            rows = c.execute(
                "SELECT * FROM mission_events WHERE project_id=? AND task_id=? "
                "AND sequence>? ORDER BY sequence ASC LIMIT ?",
                (project, task_id, cursor, page_size),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event.pop("payload_json") or "{}")
            events.append(event)
        return events

    def yield_execution(
        self, task_id: str, *, project: str, execution_id: str,
        generation: int, observed_through: int, outcome: str,
        requested_role: str, actor: str, head_sha: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Record an authenticated exact-execution yield atomically.

        Capacity terminalization performs the eventual WAITING transition.  A
        stale cursor remains ACTIVE so a newly-arrived event cannot be hidden.
        """
        self._validate(state="ACTIVE", requested_role=requested_role)
        normalized_outcome = str(outcome or "").strip().lower()
        if normalized_outcome not in {"continue", "waiting"}:
            raise MissionJournalError("invalid_outcome", "outcome must be continue or waiting")
        timestamp = time.time() if now is None else now
        with self._connection(project) as c:
            c.execute("BEGIN IMMEDIATE")
            item = c.execute(
                "SELECT * FROM mission_items WHERE project_id=? AND task_id=?",
                (project, task_id),
            ).fetchone()
            if item is None:
                raise MissionJournalError("mission_not_found", "mission item does not exist")
            runner = c.execute(
                "SELECT * FROM runner_sessions WHERE task_id=? "
                "AND status IN ('running','stopping') ORDER BY started_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if runner is None:
                raise MissionJournalError(
                    "current_execution_required", "no current execution is authenticated"
                )
            metadata = json.loads(runner["metadata_json"] or "{}")
            live_execution_id = str(
                metadata.get("execution_id")
                or (metadata.get("execution") or {}).get("execution_id")
                or ""
            )
            live_generation = int(
                metadata.get("execution_generation")
                or (metadata.get("execution") or {}).get("generation")
                or 0
            )
            live_head = str(
                metadata.get("execution_head_sha")
                or (metadata.get("execution") or {}).get("head_sha")
                or ""
            )
            execution_role = str(
                metadata.get("execution_role") or metadata.get("role") or ""
            ).strip().lower()
            if (execution_id != live_execution_id or int(generation) != live_generation
                    or str(actor or "") != str(runner["agent_id"] or "")):
                raise MissionJournalError(
                    "stale_execution", "yield does not match the authenticated current execution"
                )
            if live_head and str(head_sha or "") != live_head:
                raise MissionJournalError("stale_head", "yield head does not match assignment head")
            lease = c.execute(
                "SELECT fence_epoch FROM resource_leases "
                "WHERE id=? AND resource_type='execution'",
                (execution_id,),
            ).fetchone()
            if lease is None:
                raise MissionJournalError(
                    "execution_lease_required", "current execution lease is unavailable"
                )
            execution_identity = {
                "runner_session_id": str(runner["runner_session_id"] or ""),
                "execution_id": execution_id,
                "execution_connection_id": str(
                    metadata.get("execution_connection_id") or ""
                ),
                "generation": live_generation,
                "fence_epoch": int(lease["fence_epoch"] or 0),
                "role": execution_role,
                "head_sha": live_head,
            }
            latest = int(c.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM mission_events "
                "WHERE project_id=? AND task_id=?",
                (project, task_id),
            ).fetchone()[0])
            current_cursor = int(observed_through)
            idem_key = (
                f"yield:{execution_id}:{generation}:{current_cursor}:{normalized_outcome}"
            )
            duplicate = c.execute(
                "SELECT * FROM mission_events WHERE project_id=? AND idempotency_key=?",
                (project, idem_key),
            ).fetchone()
            if duplicate is not None:
                payload = json.loads(duplicate["payload_json"] or "{}")
                return {
                    "schema": "switchboard.mission_yield.v4",
                    "task_id": task_id,
                    "execution_id": execution_id,
                    "generation": generation,
                    "outcome": normalized_outcome,
                    "observed_through": current_cursor,
                    "latest_sequence": int(payload.get("latest_sequence_at_yield") or 0),
                    "cursor_current": bool(payload.get("cursor_current")),
                    "state": "ACTIVE",
                    "pending_state": (
                        "WAITING"
                        if normalized_outcome == "waiting"
                        and payload.get("cursor_current") else None
                    ),
                    "surrender_requested": True,
                    "event_id": duplicate["event_id"],
                    "created": False,
                    "execution_identity": execution_identity,
                }
            sequence = latest + 1
            event_id = f"missionevent-{uuid.uuid4().hex}"
            current = current_cursor == latest
            payload = {
                "outcome": normalized_outcome,
                "requested_role": requested_role,
                "observed_through": current_cursor,
                "latest_sequence_at_yield": latest,
                "cursor_current": current,
            }
            c.execute(
                "INSERT INTO mission_events(event_id,project_id,task_id,sequence,event_type,"
                "source_plane,occurred_at,head_sha,generation,execution_id,payload_json,"
                "idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, project, task_id, sequence, "agent_yielded", "coordination",
                 timestamp, head_sha or None, generation, execution_id,
                 json.dumps(payload, sort_keys=True, separators=(",", ":")),
                 idem_key),
            )
            # The yield event itself is an audit of the already-observed
            # decision, not a new wake edge.
            handled = sequence if current else int(item["handled_through"] or 0)
            c.execute(
                "UPDATE mission_items SET state='ACTIVE',requested_role=?,handled_through=?,"
                "version=version+1,updated_at=? WHERE project_id=? AND task_id=?",
                (requested_role, handled, timestamp, project, task_id),
            )
        return {
            "schema": "switchboard.mission_yield.v4",
            "task_id": task_id,
            "execution_id": execution_id,
            "generation": generation,
            "outcome": normalized_outcome,
            "observed_through": current_cursor,
            "latest_sequence": latest,
            "cursor_current": current,
            "state": "ACTIVE",
            "pending_state": "WAITING" if normalized_outcome == "waiting" and current else None,
            "surrender_requested": True,
            "event_id": event_id,
            "created": True,
            "execution_identity": execution_identity,
        }

    def record_runner_terminal(
        self, task_id: str, *, project: str, runner_session_id: str,
        execution_id: str, generation: int, status: str, head_sha: str = "",
        accepted_role: str = "", now: float | None = None,
    ) -> dict[str, Any]:
        """Project one exact Capacity terminal receipt into the v4 inbox.

        The receipt never chooses a role.  It either finalizes an already
        authenticated yield/C3 handoff or keeps the mission's current role
        eligible.  Replayed terminal heartbeats are idempotent.
        """
        task_id = str(task_id or "").strip().upper()
        runner_session_id = str(runner_session_id or "").strip()
        execution_id = str(execution_id or "").strip()
        generation = int(generation or 0)
        if not task_id or not runner_session_id or not execution_id or generation <= 0:
            return {
                "created": False,
                "skipped": True,
                "reason": "exact_execution_identity_required",
            }
        accepted_role = str(accepted_role or "").strip().lower()
        if accepted_role and accepted_role not in ROLES:
            raise MissionJournalError(
                "invalid_role", f"unsupported requested role: {accepted_role}"
            )
        timestamp = time.time() if now is None else now
        idem_key = f"runner_ended:{runner_session_id}"
        with self._connection(project) as c:
            c.execute("BEGIN IMMEDIATE")
            item = c.execute(
                "SELECT * FROM mission_items WHERE project_id=? AND task_id=?",
                (project, task_id),
            ).fetchone()
            if item is None:
                return {
                    "created": False,
                    "skipped": True,
                    "reason": "mission_not_found",
                    "task_id": task_id,
                }
            duplicate = c.execute(
                "SELECT * FROM mission_events WHERE project_id=? AND idempotency_key=?",
                (project, idem_key),
            ).fetchone()
            if duplicate is not None:
                return {
                    **dict(duplicate),
                    "payload": json.loads(duplicate["payload_json"] or "{}"),
                    "created": False,
                }

            yielded = c.execute(
                "SELECT * FROM mission_events WHERE project_id=? AND task_id=? "
                "AND event_type='agent_yielded' AND execution_id=? AND generation=? "
                "ORDER BY sequence DESC LIMIT 1",
                (project, task_id, execution_id, generation),
            ).fetchone()
            yielded_payload = (
                json.loads(yielded["payload_json"] or "{}") if yielded else {}
            )
            latest = int(c.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM mission_events "
                "WHERE project_id=? AND task_id=?",
                (project, task_id),
            ).fetchone()[0])
            current_yield = bool(
                yielded
                and yielded_payload.get("cursor_current") is True
                and str(yielded_payload.get("requested_role") or "") in ROLES
                and int(yielded["sequence"]) == latest
            )
            handoff_kind = "none"
            next_role = str(item["requested_role"])
            outcome = "continue"
            if accepted_role:
                handoff_kind = "c3_completion"
                next_role = accepted_role
            elif current_yield:
                handoff_kind = "agent_yield"
                next_role = str(yielded_payload["requested_role"])
                outcome = str(yielded_payload.get("outcome") or "continue")

            sequence = latest + 1
            event_id = f"missionevent-{uuid.uuid4().hex}"
            payload = {
                "runner_session_id": runner_session_id,
                "status": str(status or "").strip().lower(),
                "handoff_kind": handoff_kind,
                "requested_role": next_role,
                "outcome": outcome,
                "yield_event_id": yielded["event_id"] if current_yield else None,
            }
            c.execute(
                "INSERT INTO mission_events(event_id,project_id,task_id,sequence,event_type,"
                "source_plane,occurred_at,head_sha,generation,execution_id,payload_json,"
                "idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id, project, task_id, sequence, "runner_ended", "capacity",
                    timestamp, head_sha or None, generation, execution_id,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    idem_key,
                ),
            )

            current_state = str(item["state"])
            if current_state in {"DONE", "HUMAN"}:
                next_state = current_state
                handled_through = sequence
            elif current_yield and not accepted_role and outcome == "waiting":
                next_state = "WAITING"
                handled_through = sequence
            else:
                # The terminal receipt is the wake edge.  Leaving it unhandled
                # lets the fenced worker copy the already-persisted role into
                # exactly one fresh start_task call.
                next_state = "ACTIVE"
                handled_through = int(item["handled_through"] or 0)
            c.execute(
                "UPDATE mission_items SET state=?,requested_role=?,handled_through=?,"
                "version=version+1,updated_at=? WHERE project_id=? AND task_id=?",
                (
                    next_state, next_role, handled_through, timestamp, project, task_id,
                ),
            )
        return {
            "event_id": event_id,
            "project_id": project,
            "task_id": task_id,
            "sequence": sequence,
            "event_type": "runner_ended",
            "source_plane": "capacity",
            "execution_id": execution_id,
            "generation": generation,
            "payload": payload,
            "idempotency_key": idem_key,
            "created": True,
            "state": next_state,
            "requested_role": next_role,
            "handled_through": handled_through,
        }

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
