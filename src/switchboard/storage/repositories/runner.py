"""Runner session and control-request persistence (ARCH-MS-29).

Canonical home under ``switchboard.storage.repositories``. ``runner_store.py`` at
repo root remains a backward-compatible shim while callers migrate.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from constants import DEFAULT_PROJECT
from db.connection import _conn
from db.core import _json_obj, _text_tail

RUNNER_CONTROL_ACTIONS = {"snapshot", "kill", "restart", "health", "logs", "open", "inject"}
# COORD-34 / M4.6: operator Watch/Chat may open only when this bind is complete.
# wake_id and work_session_id live in metadata_json; runner_sessions is SoT
# (never add permanent EC2 instance_id columns on the task row).
# CO-13: inject additionally requires matching task_id on the control request.
RUNNER_BIND_FIELDS = ("task_id", "claim_id", "host_id", "wake_id", "work_session_id")
RUNNER_WATCHABLE_STATUSES = frozenset({"ready", "running"})
RUNNER_BIND_ERROR = "runner_bind_incomplete"
RUNNER_INJECT_ERROR = "wrong_session"

__all__ = [
    "RUNNER_BIND_FIELDS",
    "RUNNER_BIND_ERROR",
    "RUNNER_INJECT_ERROR",
    "RUNNER_WATCHABLE_STATUSES",
    "_runner_session_row",
    "_upsert_runner_session_in",
    "upsert_runner_session",
    "list_runner_sessions",
    "get_runner_session",
    "runner_bind_tuple",
    "missing_runner_bind_fields",
    "runner_bind_incomplete",
    "is_preclaim_runner",
    "requires_full_runner_bind",
    "assert_runner_watchable",
    "resolve_task_active_runner",
    "resolve_runner_watch",
    "_clear_active_runner_pointer_in",
    "request_runner_control",
    "list_runner_control_requests",
    "claim_runner_control_request",
    "complete_runner_control_request",
]


def _store_facade():
    """Resolve transitional side-effect-ledger hooks after store.py is initialized."""
    import store

    return store


def _normalize_runner_control(control: Dict[str, Any], host_id: str) -> Dict[str, Any]:
    """Fail closed on T3 claims.

    A session may advertise runner_kill only when it is both host-owned and explicitly
    managed by a supervisor/process handle. Unmanaged sessions can still be listed, but they
    cannot make the UI/API show a kill button.
    """
    raw = dict(control or {})
    managed = bool(
        raw.get("managed_process")
        or raw.get("managed")
        or raw.get("supervised")
        or str(raw.get("tier") or "").upper() == "T3"
    )
    runner_kill = bool(raw.get("runner_kill")) and managed and bool(host_id)
    runner_restart = False  # fail closed until supervisor restart is implemented end-to-end
    raw["managed_process"] = managed
    raw["runner_kill"] = runner_kill
    raw["runner_restart"] = runner_restart
    if runner_kill:
        raw.setdefault("tier", "T3")
    return raw


def _runner_available_actions(session: Dict[str, Any]) -> List[str]:
    control = session.get("control") or {}
    metadata = session.get("metadata") or {}
    status = str(session.get("status") or "").lower()
    if session.get("stale") or status in {"exited", "killed", "failed", "completed"}:
        return []
    actions: List[str] = []
    has_host = bool(session.get("host_id"))
    if control.get("managed_process") and session.get("host_id"):
        actions.extend(["health", "snapshot"])
    if has_host and (metadata.get("log_path") or control.get("runner_logs")):
        actions.append("logs")
    if has_host and control.get("runner_open"):
        actions.append("open")
    if has_host and control.get("runner_inject"):
        actions.append("inject")
    if control.get("runner_kill"):
        actions.append("kill")
    if control.get("runner_restart"):
        actions.append("restart")
    return sorted(dict.fromkeys(actions))


def _runner_control_capabilities(session: Dict[str, Any]) -> Dict[str, str]:
    available = set(session.get("available_actions") or [])
    return {action: ("supported" if action in available else "not_supported")
            for action in sorted(RUNNER_CONTROL_ACTIONS)}


def _runner_environment(session: Dict[str, Any], now: float) -> Dict[str, Any]:
    metadata = session.get("metadata") or {}
    snapshot = session.get("last_snapshot") or {}
    status = "stale" if session.get("stale") else (session.get("status") or "unknown")
    started_at = session.get("started_at")
    uptime = None
    if started_at:
        try:
            uptime = max(0.0, now - float(started_at))
        except (TypeError, ValueError):
            uptime = None
    last_result = (
        metadata.get("last_result")
        or snapshot.get("last_result")
        or snapshot.get("result")
        or {}
    )
    failure_reason = (
        metadata.get("failure_reason")
        or metadata.get("last_error")
        or snapshot.get("failure_reason")
        or snapshot.get("error")
        or (last_result.get("error") if isinstance(last_result, dict) else "")
    )
    return {
        "status": status,
        "uptime_seconds": uptime,
        "failure_reason": failure_reason or None,
        "last_command": metadata.get("command") or session.get("command"),
        "last_result": last_result or None,
        "log_tail": _text_tail(snapshot.get("log_tail") or metadata.get("log_tail") or ""),
        "log_path": metadata.get("log_path"),
        "capabilities": _runner_control_capabilities(session),
    }


def _runner_session_row(row: sqlite3.Row, now: Optional[float] = None,
                        include_claim: bool = False,
                        c: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    d = dict(row)
    ttl_s = d.get("heartbeat_ttl_s") or 60
    expires_at = (d.get("heartbeat_at") or 0) + ttl_s
    d["control"] = _json_obj(d.pop("control_json", "{}"), {})
    d["metadata"] = _json_obj(d.pop("metadata_json", "{}"), {})
    d["last_snapshot"] = _json_obj(d.pop("last_snapshot_json", "{}"), {})
    d["expires_at"] = expires_at
    d["stale"] = now >= expires_at
    d["available_actions"] = _runner_available_actions(d)
    d["environment"] = _runner_environment(d, now)
    if include_claim and c is not None and d.get("claim_id"):
        claim = c.execute("SELECT * FROM task_claims WHERE id=?", (d["claim_id"],)).fetchone()
        d["claim"] = dict(claim) if claim else None
    return d


def _runner_control_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["snapshot"] = _json_obj(d.pop("snapshot_json", "{}"), {})
    d["result"] = _json_obj(d.pop("result_json", "{}"), {})
    d["options"] = _json_obj(d.pop("options_json", "{}"), {})
    return d


def _runner_snapshot_from_session(session: Dict[str, Any],
                                  reason: str = "operator_request") -> Dict[str, Any]:
    return {
        "captured_at": time.time(),
        "source": "switchboard_registry",
        "reason": reason,
        "runner_session_id": session.get("runner_session_id"),
        "host_id": session.get("host_id"),
        "agent_id": session.get("agent_id"),
        "runtime": session.get("runtime"),
        "task_id": session.get("task_id"),
        "claim_id": session.get("claim_id"),
        "pid": session.get("pid"),
        "status": session.get("status"),
        "cwd": session.get("cwd"),
        "heartbeat_at": session.get("heartbeat_at"),
        "head_sha": (session.get("last_snapshot") or {}).get("head_sha"),
    }


def runner_bind_tuple(record: Dict[str, Any]) -> Dict[str, str]:
    """Extract the COORD-34 autopilot bind fields from a runner session record."""
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    if not metadata and record.get("metadata_json"):
        metadata = _json_obj(record.get("metadata_json"), {})
    return {
        "task_id": str(record.get("task_id") or "").strip(),
        "claim_id": str(record.get("claim_id") or "").strip(),
        "host_id": str(record.get("host_id") or "").strip(),
        "wake_id": str(
            metadata.get("wake_id") or record.get("wake_id") or "").strip(),
        "work_session_id": str(
            metadata.get("work_session_id") or record.get("work_session_id") or "").strip(),
    }


def missing_runner_bind_fields(record: Dict[str, Any]) -> List[str]:
    """Return bind field names that are absent or malformed for Watch/Chat."""
    bind = runner_bind_tuple(record)
    missing = [name for name in RUNNER_BIND_FIELDS if not bind.get(name)]
    host_id = bind.get("host_id") or ""
    # Contract: host_id=host/<instance-id>. Reject blank host/ and non-host shapes.
    if host_id and not (
            host_id.startswith("host/") and len(host_id) > len("host/")
    ) and "host_id" not in missing:
        missing.append("host_id")
    return missing


def runner_bind_incomplete(missing: List[str], *,
                           runner_session_id: str = "",
                           task_id: str = "") -> Dict[str, Any]:
    """Typed refusal used by UI-17 Watch/Chat when the bind contract is incomplete."""
    return {
        "error": RUNNER_BIND_ERROR,
        "error_code": RUNNER_BIND_ERROR,
        "failure_class": "unbound_identity",
        "missing": list(missing),
        "refused": True,
        "watchable": False,
        "runner_session_id": runner_session_id or None,
        "task_id": task_id or None,
        "message": (
            "Runner session bind incomplete for Watch/Chat; "
            f"missing: {', '.join(missing) or 'bind fields'}"
        ),
    }


def is_preclaim_runner(record: Dict[str, Any]) -> bool:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    phase = str(metadata.get("credential_admission_phase") or "").strip().lower()
    status = str(record.get("status") or "").strip().lower()
    return phase in {"preclaim", "preclaim_failed"} or (
        status == "starting" and phase != "claim_bound")


def requires_full_runner_bind(record: Dict[str, Any]) -> bool:
    """True when this registration must carry the full claim/host/wake bind.

    Watch/Chat always uses ``assert_runner_watchable`` / ``resolve_runner_watch``.
    Upsert fails closed for claim-bound Agent Host / BYOA registrations; advisory
    registry rows that only publish claim_id for fleet UI remain allowed.
    """
    if record.get("require_task_bind") is False:
        return False
    if record.get("require_task_bind") is True:
        return True
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    phase = str(metadata.get("credential_admission_phase") or "").strip().lower()
    return phase == "claim_bound"


def assert_runner_watchable(session: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Fail closed: Watch/Chat may open only for a fully bound live runner."""
    if not session:
        return runner_bind_incomplete(list(RUNNER_BIND_FIELDS))
    if session.get("stale"):
        return runner_bind_incomplete(
            list(RUNNER_BIND_FIELDS),
            runner_session_id=str(session.get("runner_session_id") or ""),
            task_id=str(session.get("task_id") or ""),
        ) | {"message": "Runner session is stale; Watch/Chat refused until a live bind exists"}
    missing = missing_runner_bind_fields(session)
    if missing:
        return runner_bind_incomplete(
            missing,
            runner_session_id=str(session.get("runner_session_id") or ""),
            task_id=str(session.get("task_id") or ""),
        )
    status = str(session.get("status") or "").strip().lower()
    if status not in RUNNER_WATCHABLE_STATUSES:
        return runner_bind_incomplete(
            list(RUNNER_BIND_FIELDS),
            runner_session_id=str(session.get("runner_session_id") or ""),
            task_id=str(session.get("task_id") or ""),
        ) | {
            "message": (
                f"Runner session status {status or 'unknown'} is not watchable; "
                "need ready/running with full bind"
            ),
            "status": status or None,
        }
    bind = runner_bind_tuple(session)
    return {
        "watchable": True,
        "refused": False,
        "runner_session_id": session.get("runner_session_id"),
        "task_id": bind["task_id"],
        "bind": bind,
        "session": session,
    }


def resolve_runner_watch(task_id: str, *, include_stale: bool = False,
                         project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Pick a Watch/Chat-ready runner for a task, or return a typed refusal.

    UI-17 and Mission panel open only through this gate: listing alone is not enough
    when rows exist but the bind tuple is incomplete.
    """
    task_id = (task_id or "").strip()
    if not task_id:
        return runner_bind_incomplete(["task_id"]) | {
            "message": "task_id is required to open Watch/Chat",
        }
    sessions = list_runner_sessions(
        task_id=task_id, include_stale=include_stale, project=project)
    if not sessions:
        return runner_bind_incomplete(list(RUNNER_BIND_FIELDS), task_id=task_id) | {
            "message": "No runner sessions are registered for this task",
            "sessions": [],
        }
    refusals: List[Dict[str, Any]] = []
    for session in sessions:
        verdict = assert_runner_watchable(session)
        if verdict.get("watchable"):
            return {
                **verdict,
                "sessions": sessions,
                "enough_for_panel": True,
            }
        refusals.append(verdict)
    best = refusals[0] if refusals else runner_bind_incomplete(
        list(RUNNER_BIND_FIELDS), task_id=task_id)
    return {
        **best,
        "sessions": sessions,
        "enough_for_panel": False,
        "candidates": len(sessions),
    }


def resolve_task_active_runner(task_id: str, *, agent_state: Optional[Dict[str, Any]] = None,
                               sessions: Optional[List[Dict[str, Any]]] = None,
                               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Resolve Mission's active runner pointer, falling back to authoritative rows.

    ``runner_sessions`` remains the source of truth. The task ``agent_state`` pointer is
    only a fast path and is ignored when it is stale, terminal, incomplete, or bound to
    another task.
    """
    task_id = str(task_id or "").strip()
    state = dict(agent_state or {})
    pointer = state.get("switchboard/runner")
    pointer = pointer if isinstance(pointer, dict) else {}
    pointer_id = str(
        pointer.get("active_runner_session_id")
        or state.get("active_runner_session_id")
        or ""
    ).strip()

    def usable(session: Optional[Dict[str, Any]]) -> bool:
        if not session or session.get("stale"):
            return False
        if str(session.get("task_id") or "") != task_id:
            return False
        return bool(assert_runner_watchable(session).get("watchable"))

    candidates = sessions
    if candidates is None:
        candidates = list_runner_sessions(
            task_id=task_id, include_stale=True, project=project)

    if pointer_id:
        pointed = next(
            (session for session in candidates
             if str(session.get("runner_session_id") or "") == pointer_id),
            None,
        )
        if usable(pointed):
            return {
                "schema": "switchboard.active_runner_resolution.v1",
                "task_id": task_id,
                "active": True,
                "source": "agent_state_pointer",
                "pointer_id": pointer_id,
                "session": pointed,
            }

    for session in candidates:
        if usable(session):
            return {
                "schema": "switchboard.active_runner_resolution.v1",
                "task_id": task_id,
                "active": True,
                "source": "runner_sessions_fallback",
                "pointer_id": pointer_id or None,
                "session": session,
            }
    return {
        "schema": "switchboard.active_runner_resolution.v1",
        "task_id": task_id,
        "active": False,
        "source": "none",
        "pointer_id": pointer_id or None,
        "session": None,
    }


def _merge_existing_runner_record(c: sqlite3.Connection,
                                  record: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve stronger claim/work bind fields across heartbeat / partial upserts."""
    runner_session_id = (record.get("runner_session_id") or record.get("id") or "").strip()
    if not runner_session_id:
        return record
    existing_row = c.execute(
        "SELECT * FROM runner_sessions WHERE runner_session_id=?",
        (runner_session_id,),
    ).fetchone()
    if not existing_row:
        return record
    existing = dict(existing_row)
    existing_metadata = _json_obj(existing.get("metadata_json", "{}"), {})
    merged = dict(record)
    for key in ("host_id", "agent_id", "runtime", "task_id", "claim_id", "cwd"):
        if not str(merged.get(key) or "").strip() and existing.get(key):
            merged[key] = existing.get(key)
    if merged.get("pid") is None and existing.get("pid") is not None:
        merged["pid"] = existing.get("pid")
    metadata = dict(existing_metadata)
    incoming = merged.get("metadata") if isinstance(merged.get("metadata"), dict) else {}
    metadata.update({k: v for k, v in incoming.items() if v is not None and v != ""})
    for key in ("wake_id", "work_session_id", "wake_mode", "command", "log_path", "pgid"):
        if key in merged and merged.get(key) not in (None, ""):
            metadata.setdefault(key, merged.get(key))
    for key in ("wake_id", "work_session_id"):
        if not metadata.get(key) and existing_metadata.get(key):
            metadata[key] = existing_metadata.get(key)
    merged["metadata"] = metadata
    if not merged.get("control") and existing.get("control_json"):
        merged["control"] = _json_obj(existing.get("control_json"), {})
    if merged.get("heartbeat_ttl_s") is None and existing.get("heartbeat_ttl_s"):
        merged["heartbeat_ttl_s"] = existing.get("heartbeat_ttl_s")
    return merged


def _maybe_set_active_runner_pointer(c: sqlite3.Connection, record: Dict[str, Any],
                                     now: float) -> None:
    """Optional Mission UI pointer: agent_state.active_runner_session_id."""
    task_id = str(record.get("task_id") or "").strip()
    runner_session_id = str(
        record.get("runner_session_id") or record.get("id") or "").strip()
    if not task_id or not runner_session_id:
        return
    if missing_runner_bind_fields(record):
        return
    row = c.execute("SELECT agent_state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return
    current = _json_obj(row["agent_state"] if "agent_state" in row.keys() else "{}", {})
    if not isinstance(current, dict):
        current = {}
    pointer = dict(current.get("switchboard/runner") or {})
    next_pointer = {
        "active_runner_session_id": runner_session_id,
        "host_id": record.get("host_id"),
        "claim_id": record.get("claim_id"),
        "updated_at": now,
    }
    if (pointer.get("active_runner_session_id") == runner_session_id
            and pointer.get("host_id") == record.get("host_id")
            and pointer.get("claim_id") == record.get("claim_id")
            and current.get("active_runner_session_id") == runner_session_id
            and current.get("active_runner_host_id") == record.get("host_id")):
        return
    current["switchboard/runner"] = next_pointer
    # Legacy flat key for Mission UI readers that expect the optional field name.
    current["active_runner_session_id"] = runner_session_id
    current["active_runner_host_id"] = record.get("host_id")
    c.execute("UPDATE tasks SET agent_state=?, updated_at=? WHERE task_id=?",
              (json.dumps(current, sort_keys=True), now, task_id))


def _clear_active_runner_pointer_in(c: sqlite3.Connection, task_id: str,
                                    runner_session_id: str,
                                    now: Optional[float] = None) -> bool:
    """Clear only the matching convenience pointer; never erase a newer runner."""
    task_id = str(task_id or "").strip()
    runner_session_id = str(runner_session_id or "").strip()
    if not task_id or not runner_session_id:
        return False
    row = c.execute("SELECT agent_state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return False
    current = _json_obj(row["agent_state"] if "agent_state" in row.keys() else "{}", {})
    if not isinstance(current, dict):
        return False
    nested = current.get("switchboard/runner")
    nested_id = (nested or {}).get("active_runner_session_id") if isinstance(nested, dict) else ""
    flat_id = current.get("active_runner_session_id")
    if runner_session_id not in {str(nested_id or ""), str(flat_id or "")}:
        return False
    if str(nested_id or "") == runner_session_id:
        current.pop("switchboard/runner", None)
    if str(flat_id or "") == runner_session_id:
        current.pop("active_runner_session_id", None)
        current.pop("active_runner_host_id", None)
    c.execute("UPDATE tasks SET agent_state=?, updated_at=? WHERE task_id=?",
              (json.dumps(current, sort_keys=True), now or time.time(), task_id))
    return True


def _renew_personal_claim_from_runner_in(
        c: sqlite3.Connection, record: Dict[str, Any], principal_id: str,
        now: float) -> bool:
    """Extend only the claim behind an exact, live personal-host connection.

    Native Codex runs may outlive the ordinary claim TTL. The authenticated
    runner heartbeat is the renewal signal, but it may renew only the complete
    tuple already fenced by ``personal_execution_connections`` and never revive
    an expired claim or outlive the execution deadline.
    """
    connection, binding_error = _personal_runner_connection_in(
        c, record, principal_id, now)
    if binding_error or not connection:
        return False
    status = str(record.get("status") or "").strip().lower()
    if status not in {"starting", "ready", "running"}:
        return False
    if str(connection.get("status") or "") != "active":
        return False
    expected = _personal_runner_connection_tuple(record, principal_id)
    renewed = c.execute(
        "UPDATE task_claims SET expires_at=? "
        "WHERE id=? AND task_id=? AND agent_id=? AND status='active' "
        "AND expires_at>? AND expires_at<?",
        (float(connection["expires_at"]), expected["claim_id"],
         expected["task_id"], expected["agent_id"], now,
         float(connection["expires_at"])),
    )
    return renewed.rowcount == 1


def _personal_runner_connection_tuple(
        record: Dict[str, Any], principal_id: str) -> Dict[str, str]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    return {
        "runner_session_id": str(
            record.get("runner_session_id") or record.get("id") or "").strip(),
        "host_id": str(record.get("host_id") or "").strip(),
        "host_principal_id": str(principal_id or "").strip(),
        "agent_id": str(record.get("agent_id") or "").strip(),
        "task_id": str(record.get("task_id") or "").strip(),
        "claim_id": str(record.get("claim_id") or "").strip(),
        "wake_id": str(metadata.get("wake_id") or "").strip(),
        "work_session_id": str(metadata.get("work_session_id") or "").strip(),
        "source_sha": str(metadata.get("source_sha") or "").strip(),
    }


def _personal_runner_connection_in(
        c: sqlite3.Connection, record: Dict[str, Any], principal_id: str,
        now: float) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Resolve only the exact durable personal execution tuple for this runner."""
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    connection_id = str(metadata.get("execution_connection_id") or "").strip()
    if not connection_id or not principal_id:
        return None, {
            "error": "runner session is not bound to a personal execution connection",
            "error_code": "runner_execution_binding_mismatch",
            "failure_class": "unbound_identity",
            "reason_codes": ["execution_connection_id_missing"],
        }
    connection_row = c.execute(
        "SELECT * FROM personal_execution_connections "
        "WHERE execution_connection_id=? AND expires_at>?",
        (connection_id, now),
    ).fetchone()
    if not connection_row:
        return None, {
            "error": "runner session is not bound to a live personal execution connection",
            "error_code": "runner_execution_binding_mismatch",
            "failure_class": "unbound_identity",
            "reason_codes": ["execution_connection_not_found"],
            "execution_connection_id": connection_id,
        }
    connection = dict(connection_row)
    expected = _personal_runner_connection_tuple(record, principal_id)
    mismatches = sorted(
        field for field, value in expected.items()
        if not value or str(connection.get(field) or "") != value)
    status = str(record.get("status") or "").strip().lower()
    allowed_connection_states = {
        "starting": {"reserved", "active"},
        "ready": {"active"},
        "running": {"active"},
        "completed": {"active", "completed"},
        "failed": {"reserved", "active", "failed"},
    }
    connection_status = str(connection.get("status") or "").strip().lower()
    reason_codes: list[str] = []
    if mismatches:
        reason_codes.append("execution_connection_tuple_mismatch")
    if status not in allowed_connection_states:
        reason_codes.append("runner_status_not_permitted")
    elif connection_status not in allowed_connection_states[status]:
        reason_codes.append("execution_connection_status_mismatch")
    if reason_codes:
        return None, {
            "error": "runner session is not bound to the exact personal execution connection",
            "error_code": "runner_execution_binding_mismatch",
            "failure_class": "unbound_identity",
            "reason_codes": reason_codes,
            "mismatches": mismatches,
            "runner_session_id": expected["runner_session_id"] or None,
            "execution_connection_id": connection_id,
            "runner_status": status or None,
            "execution_connection_status": connection_status or None,
        }
    return connection, None


def _native_agent_host_runner_allowed_in(
        c: sqlite3.Connection, record: Dict[str, Any],
        principal_id: str) -> bool:
    """Admit only the server-selected host-local runner for a live wake.

    Enrolled Agent Hosts are narrow principals.  BYOA/account-bound executions
    must keep their ``personal_execution_connections`` fence, but a project-wide
    native Codex host intentionally has no exported/provider connection.  Its
    authority is the live wake placement plus host principal.  Persist a marker
    derived here (never trusted from the request) so later claim-bound heartbeats
    for the same runner remain admissible after the wake becomes terminal.
    """
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    runner_session_id = str(
        record.get("runner_session_id") or record.get("id") or "").strip()
    host_id = str(record.get("host_id") or "").strip()
    existing = c.execute(
        "SELECT host_id, principal_id, metadata_json FROM runner_sessions "
        "WHERE runner_session_id=?", (runner_session_id,),
    ).fetchone() if runner_session_id else None
    if existing:
        existing_metadata = _json_obj(existing["metadata_json"], {})
        if (str(existing["host_id"] or "") == host_id
                and str(existing["principal_id"] or "") == principal_id
                and existing_metadata.get("native_host_execution") is True):
            metadata["native_host_execution"] = True
            record["metadata"] = metadata
            return True

    phase = str(metadata.get("credential_admission_phase") or "").strip().lower()
    status = str(record.get("status") or "").strip().lower()
    wake_id = str(metadata.get("wake_id") or "").strip()
    if (phase != "preclaim" or status != "starting" or record.get("claim_id")
            or not wake_id or not host_id):
        return False
    wake = c.execute(
        "SELECT status, claimed_by_host, task_id, selector_json, policy_json, "
        "placement_json FROM wake_intents WHERE wake_id=?", (wake_id,),
    ).fetchone()
    if not wake or str(wake["status"] or "") not in {"pending", "claimed"}:
        return False
    selector = _json_obj(wake["selector_json"], {})
    policy = _json_obj(wake["policy_json"], {})
    placement = _json_obj(wake["placement_json"], {})
    execution = policy.get("execution_binding") or {}
    if (policy.get("account_binding") or execution.get("execution_connection_id")
            or policy.get("require_runner_bind") is not True):
        return False
    expected = {
        "task_id": str(wake["task_id"] or selector.get("task_id") or ""),
        "agent_id": str(selector.get("agent_id") or ""),
        "runtime": str(selector.get("runtime") or ""),
    }
    if any(str(record.get(key) or "") != value
           for key, value in expected.items() if value):
        return False
    if str(wake["status"] or "") == "claimed":
        if str(wake["claimed_by_host"] or "") != host_id:
            return False
    elif str(placement.get("selected_host_id") or "") != host_id:
        return False
    metadata["native_host_execution"] = True
    record["metadata"] = metadata
    return True


def _upsert_runner_session_in(c: sqlite3.Connection, record: Dict[str, Any],
                              principal_id: str, actor: str, now: float) -> Dict[str, Any]:
    runner_session_id = (record.get("runner_session_id") or record.get("id") or "").strip()
    if not runner_session_id:
        return {"error": "runner_session_id required"}
    principal = c.execute(
        "SELECT kind, scopes FROM principals WHERE id=?", (principal_id,),
    ).fetchone() if principal_id else None
    scopes: set[str] = set()
    if principal:
        try:
            scopes = set(json.loads(principal["scopes"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            scopes = set()
    narrow_host = bool(
        principal and "write:agent_host" in scopes
        and "write:ixp" not in scopes and "admin" not in scopes)
    if narrow_host:
        submitted_host = str(record.get("host_id") or "").strip()
        enrollment = c.execute(
            "SELECT host_id FROM agent_host_enrollments "
            "WHERE principal_id=? AND host_id=? AND status='active'",
            (principal_id, submitted_host),
        ).fetchone()
        registered_host = c.execute(
            "SELECT principal_id FROM agent_hosts WHERE host_id=?",
            (submitted_host,),
        ).fetchone()
        existing = c.execute(
            "SELECT host_id, principal_id FROM runner_sessions WHERE runner_session_id=?",
            (runner_session_id,),
        ).fetchone()
        if (not enrollment or not registered_host
                or str(registered_host["principal_id"] or "") != principal_id
                or (existing and (
                    str(existing["host_id"] or "") != submitted_host
                    or str(existing["principal_id"] or "") != principal_id))):
            return {
                "error": "runner identity is owned by another host",
                "error_code": "runner_identity_mismatch",
                "failure_class": "unbound_identity",
                "runner_session_id": runner_session_id,
            }
    record = _merge_existing_runner_record(c, record)
    host_id = (record.get("host_id") or "").strip()
    control = _normalize_runner_control(record.get("control") or {}, host_id)
    metadata = dict(record.get("metadata") or {})
    for key in ("command", "log_path", "pgid", "wake_id", "wake_mode", "alive",
                "work_session_id"):
        if key in record and key not in metadata:
            metadata[key] = record.get(key)
    record = {**record, "host_id": host_id, "metadata": metadata,
              "runner_session_id": runner_session_id}
    if narrow_host and not _native_agent_host_runner_allowed_in(
            c, record, principal_id):
        _connection, binding_error = _personal_runner_connection_in(
            c, record, principal_id, now)
        if binding_error:
            return binding_error
    if requires_full_runner_bind(record):
        missing = missing_runner_bind_fields(record)
        if missing:
            return runner_bind_incomplete(
                missing,
                runner_session_id=runner_session_id,
                task_id=str(record.get("task_id") or ""),
            )
    snapshot = record.get("last_snapshot") or record.get("snapshot") or {}
    heartbeat_ttl_s = max(10, int(record.get("heartbeat_ttl_s") or record.get("ttl_s") or 60))
    started_at = record.get("started_at") or now
    heartbeat_at = record.get("heartbeat_at") or now
    c.execute(
        "INSERT INTO runner_sessions(runner_session_id, host_id, agent_id, runtime, task_id, "
        "claim_id, pid, status, cwd, control_json, metadata_json, last_snapshot_json, "
        "principal_id, started_at, heartbeat_at, heartbeat_ttl_s, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(runner_session_id) DO UPDATE SET host_id=excluded.host_id, "
        "agent_id=excluded.agent_id, runtime=excluded.runtime, task_id=excluded.task_id, "
        "claim_id=excluded.claim_id, pid=excluded.pid, status=excluded.status, cwd=excluded.cwd, "
        "control_json=excluded.control_json, metadata_json=excluded.metadata_json, "
        "last_snapshot_json=CASE WHEN excluded.last_snapshot_json!='{}' "
        "THEN excluded.last_snapshot_json ELSE runner_sessions.last_snapshot_json END, "
        "principal_id=excluded.principal_id, heartbeat_at=excluded.heartbeat_at, "
        "heartbeat_ttl_s=excluded.heartbeat_ttl_s, updated_at=excluded.updated_at",
        (
            runner_session_id,
            host_id or None,
            record.get("agent_id") or None,
            record.get("runtime") or None,
            record.get("task_id") or None,
            record.get("claim_id") or None,
            record.get("pid"),
            record.get("status") or ("running" if record.get("alive", True) else "unknown"),
            record.get("cwd") or None,
            json.dumps(control, sort_keys=True),
            json.dumps(metadata, sort_keys=True),
            json.dumps(snapshot or {}, sort_keys=True),
            principal_id or None,
            started_at,
            heartbeat_at,
            heartbeat_ttl_s,
            now,
        ),
    )
    _renew_personal_claim_from_runner_in(c, record, principal_id, now)
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (record.get("task_id") or None, actor, "runner.session_registered",
               json.dumps({"runner_session_id": runner_session_id, "host_id": host_id or None,
                           "agent_id": record.get("agent_id"),
                           "runtime": record.get("runtime"),
                           "control": control,
                           "available_actions": _runner_available_actions({
                               "control": control,
                               "host_id": host_id,
                               "status": record.get("status") or "running",
                               "stale": False,
                           })}, sort_keys=True), now))
    row = c.execute("SELECT * FROM runner_sessions WHERE runner_session_id=?",
                    (runner_session_id,)).fetchone()
    session = _runner_session_row(row, now=now, include_claim=True, c=c)
    if (not missing_runner_bind_fields(record)
            and not session.get("stale")
            and str(session.get("status") or "").lower() in RUNNER_WATCHABLE_STATUSES):
        _maybe_set_active_runner_pointer(c, record, now)
    else:
        _clear_active_runner_pointer_in(
            c, str(session.get("task_id") or record.get("task_id") or ""),
            runner_session_id, now)
    return session


def upsert_runner_session(record: Dict[str, Any], principal_id: str = "",
                          actor: str = "system",
                          project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        return _upsert_runner_session_in(c, record, principal_id, actor, now)


def list_runner_sessions(host_id: str = "", runtime: str = "", task_id: str = "",
                         status: str = "", include_stale: bool = False,
                         project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    q = "SELECT * FROM runner_sessions WHERE 1=1"
    params: List[Any] = []
    if host_id:
        q += " AND host_id=?"; params.append(host_id)
    if runtime:
        q += " AND runtime=?"; params.append(runtime)
    if task_id:
        q += " AND task_id=?"; params.append(task_id)
    if status:
        q += " AND status=?"; params.append(status)
    q += " ORDER BY heartbeat_at DESC, runner_session_id"
    now = time.time()
    with _conn(project) as c:
        rows = c.execute(q, params).fetchall()
        sessions = [_runner_session_row(r, now=now, include_claim=True, c=c) for r in rows]
    if not include_stale:
        sessions = [s for s in sessions if not s.get("stale")]
    return sessions


def get_runner_session(runner_session_id: str,
                       project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM runner_sessions WHERE runner_session_id=?",
                        (runner_session_id,)).fetchone()
        return _runner_session_row(row, now=now, include_claim=True, c=c) if row else None


def request_runner_control(runner_session_id: str, action: str, reason: str = "",
                           options: Optional[Dict[str, Any]] = None,
                           actor: str = "system", principal_id: str = "",
                           project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    action = (action or "").strip().lower()
    options = dict(options or {})
    if action not in RUNNER_CONTROL_ACTIONS:
        return {"requested": False, "error": "unsupported_action", "action": action}
    with _conn(project) as c:
        row = c.execute("SELECT * FROM runner_sessions WHERE runner_session_id=?",
                        (runner_session_id,)).fetchone()
        if not row:
            return {"requested": False, "error": "runner_session_not_found",
                    "runner_session_id": runner_session_id}
        session = _runner_session_row(row, now=now, include_claim=True, c=c)
        if action == "inject":
            caller_task = str(options.get("task_id") or "").strip()
            session_task = str(session.get("task_id") or "").strip()
            if not caller_task or not session_task or caller_task != session_task:
                return {
                    "requested": False,
                    "error": RUNNER_INJECT_ERROR,
                    "error_code": RUNNER_INJECT_ERROR,
                    "reason": "task_mismatch",
                    "message": "runner_inject requires matching runner_session_id+task_id bind",
                    "runner_session_id": runner_session_id,
                    "expected_task_id": session_task or None,
                    "provided_task_id": caller_task or None,
                }
            text = options.get("text")
            if text is None:
                text = options.get("message")
            if not isinstance(text, str) or not text:
                return {
                    "requested": False,
                    "error": "invalid_input",
                    "reason": "text_required",
                    "runner_session_id": runner_session_id,
                }
        available = set(session.get("available_actions") or [])
        effect_payload = {
            "runner_session_id": runner_session_id,
            "host_id": session.get("host_id"),
            "action": action,
            "options": options,
        }
        effect_claim = _store_facade()._claim_external_effect_in(
            c, "runner_control", session.get("host_id") or "agent_host",
            f"{runner_session_id}:{action}", effect_payload,
            task_id=session.get("task_id") or None,
            claim_id=session.get("claim_id") or "",
            agent_id=session.get("agent_id") or "",
            actor=actor, principal_id=principal_id, project=project, now=now)
        if not effect_claim.get("claimed"):
            out = {"requested": False, "reason": effect_claim.get("reason"),
                   "effect": effect_claim.get("effect"),
                   "effect_key": effect_claim.get("effect_key"),
                   "readback_required": effect_claim.get("readback_required", False)}
            if effect_claim.get("verified"):
                out["verified"] = True
                out["proof"] = effect_claim.get("proof")
            return out
        request_id = "runnerreq-" + uuid.uuid4().hex[:16]
        snapshot = _runner_snapshot_from_session(session, reason=f"before_{action}")
        req_status = "pending" if action in available else "refused"
        result = {}
        if req_status == "refused":
            result = {
                "reason": "not_supported",
                "available_actions": sorted(available),
                "capabilities": (session.get("environment") or {}).get("capabilities") or {},
                "control": session.get("control") or {},
            }
        c.execute(
            "INSERT INTO runner_control_requests(request_id, runner_session_id, host_id, "
            "action, status, reason, requested_by, principal_id, requested_at, "
            "snapshot_json, result_json, options_json, effect_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                request_id,
                runner_session_id,
                session.get("host_id") or None,
                action,
                req_status,
                reason or f"operator requested {action}",
                actor,
                principal_id or None,
                now,
                json.dumps(snapshot, sort_keys=True),
                json.dumps(result, sort_keys=True),
                json.dumps(options, sort_keys=True),
                effect_claim["effect_key"],
            ),
        )
        _store_facade()._update_external_effect_in(
            c, effect_claim["effect_key"], "issued" if req_status == "pending" else "failed",
            readback={"request_id": request_id, "status": req_status, "result": result},
            last_error="" if req_status == "pending" else "not_supported",
            actor=actor, task_id=session.get("task_id") or None, project=project, now=now)
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (session.get("task_id") or None, actor,
                   f"runner.{action}_{'requested' if req_status == 'pending' else 'refused'}",
                   json.dumps({"request_id": request_id,
                               "runner_session_id": runner_session_id,
                               "host_id": session.get("host_id"),
                               "status": req_status,
                               "reason": reason or "",
                               "effect_key": effect_claim["effect_key"],
                               "available_actions": sorted(available),
                               "snapshot": snapshot}, sort_keys=True), now))
        out = _runner_control_row(c.execute(
            "SELECT * FROM runner_control_requests WHERE request_id=?",
            (request_id,),
        ).fetchone())
    out["requested"] = req_status == "pending"
    return out


def list_runner_control_requests(status: str = "", host_id: str = "",
                                 runner_session_id: str = "",
                                 project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    q = "SELECT * FROM runner_control_requests WHERE 1=1"
    params: List[Any] = []
    if status:
        q += " AND status=?"; params.append(status)
    if host_id:
        q += " AND host_id=?"; params.append(host_id)
    if runner_session_id:
        q += " AND runner_session_id=?"; params.append(runner_session_id)
    q += " ORDER BY requested_at"
    with _conn(project) as c:
        return [_runner_control_row(r) for r in c.execute(q, params).fetchall()]


def claim_runner_control_request(host_id: str, request_id: str,
                                 actor: str = "system",
                                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM runner_control_requests WHERE request_id=?",
                        (request_id,)).fetchone()
        if not row:
            return {"claimed": False, "error": "runner_control_not_found",
                    "request_id": request_id}
        req = _runner_control_row(row)
        if req["status"] != "pending":
            return {"claimed": False, "reason": f"request is {req['status']}", "request": req}
        if req.get("host_id") and req["host_id"] != host_id:
            return {"claimed": False, "reason": "wrong_host", "host_id": host_id,
                    "request_host_id": req.get("host_id")}
        cur = c.execute(
            "UPDATE runner_control_requests SET status='claimed', claimed_at=?, "
            "claimed_by_host=? WHERE request_id=? AND status='pending'",
            (now, host_id, request_id),
        )
        if cur.rowcount == 0:
            row = c.execute("SELECT * FROM runner_control_requests WHERE request_id=?",
                            (request_id,)).fetchone()
            return {"claimed": False, "reason": "lost_race",
                    "request": _runner_control_row(row)}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "runner.control_claimed",
                   json.dumps({"request_id": request_id, "host_id": host_id}, sort_keys=True), now))
        row = c.execute("SELECT * FROM runner_control_requests WHERE request_id=?",
                        (request_id,)).fetchone()
    return {"claimed": True, "request": _runner_control_row(row)}


def complete_runner_control_request(request_id: str, result: Optional[Dict[str, Any]] = None,
                                    snapshot: Optional[Dict[str, Any]] = None,
                                    status: str = "",
                                    host_id: str = "",
                                    actor: str = "system",
                                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    result = dict(result or {})
    snapshot = dict(snapshot or {})
    with _conn(project) as c:
        row = c.execute("SELECT * FROM runner_control_requests WHERE request_id=?",
                        (request_id,)).fetchone()
        if not row:
            return {"error": "runner_control_not_found", "request_id": request_id}
        req = _runner_control_row(row)
        if host_id and (req.get("status") != "claimed"
                        or str(req.get("claimed_by_host") or "") != host_id):
            return {"error": "runner_control_host_mismatch",
                    "error_code": "runner_control_host_mismatch",
                    "request_id": request_id}
        final_status = status or ("failed" if result.get("error") else "completed")
        if final_status not in {"completed", "failed", "cancelled"}:
            final_status = "completed"
        if not snapshot:
            snapshot = req.get("snapshot") or {}
        merged_result = {**(req.get("result") or {}), **result}
        c.execute(
            "UPDATE runner_control_requests SET status=?, completed_at=?, "
            "snapshot_json=?, result_json=? WHERE request_id=?",
            (final_status, now, json.dumps(snapshot, sort_keys=True),
             json.dumps(merged_result, sort_keys=True), request_id),
        )
        session_status = None
        if req.get("action") == "kill" and final_status == "completed":
            session_status = merged_result.get("status") or "killed"
        elif req.get("action") == "snapshot" and snapshot.get("status"):
            session_status = snapshot.get("status")
        sets = ["last_snapshot_json=?", "updated_at=?"]
        vals: List[Any] = [json.dumps(snapshot, sort_keys=True), now]
        if session_status:
            sets.append("status=?")
            vals.append(session_status)
        vals.append(req["runner_session_id"])
        c.execute(f"UPDATE runner_sessions SET {', '.join(sets)} WHERE runner_session_id=?", vals)
        session_row = c.execute("SELECT * FROM runner_sessions WHERE runner_session_id=?",
                                (req["runner_session_id"],)).fetchone()
        session = _runner_session_row(session_row, now=now, include_claim=True, c=c) if session_row else {}
        if session_status and str(session_status).lower() not in RUNNER_WATCHABLE_STATUSES:
            _clear_active_runner_pointer_in(
                c, str(session.get("task_id") or ""),
                str(session.get("runner_session_id") or ""), now)
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (session.get("task_id") or None, actor, f"runner.{req['action']}_{final_status}",
                   json.dumps({"request_id": request_id,
                               "runner_session_id": req["runner_session_id"],
                               "effect_key": req.get("effect_key"),
                               "status": final_status,
                               "result": merged_result,
                               "snapshot": snapshot}, sort_keys=True), now))
        if req.get("effect_key"):
            _store_facade()._update_external_effect_in(
                c, req["effect_key"],
                "verified" if final_status == "completed" else "failed",
                readback={"request_id": request_id, "status": final_status,
                          "result": merged_result, "snapshot": snapshot},
                last_error="" if final_status == "completed" else merged_result.get("error", final_status),
                actor=actor, task_id=session.get("task_id") or None, project=project, now=now)
        row = c.execute("SELECT * FROM runner_control_requests WHERE request_id=?",
                        (request_id,)).fetchone()
    return _runner_control_row(row)
