"""Runner session and control-request persistence (ARCH-MS-29).

Canonical home under ``switchboard.storage.repositories``. ``runner_store.py`` at
repo root remains a backward-compatible shim while callers migrate.
"""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import time
import urllib.parse
import uuid
from typing import Any, Callable, Dict, List, Optional

from constants import DEFAULT_PROJECT, MCP_OPERATOR_SCOPES
from db.connection import _conn
from db.core import _json_obj, _text_tail, hash_token

RUNNER_CONTROL_ACTIONS = {"snapshot", "kill", "restart", "health", "logs", "open", "inject"}
# COORD-34 / M4.6: operator Watch/Chat may open only when this bind is complete.
# wake_id and work_session_id live in metadata_json; runner_sessions is SoT
# (never add permanent EC2 instance_id columns on the task row).
# CO-13: inject additionally requires matching task_id on the control request.
RUNNER_BIND_FIELDS = ("task_id", "claim_id", "host_id", "wake_id", "work_session_id")
RUNNER_WATCHABLE_STATUSES = frozenset({"ready", "running"})
RUNNER_TERMINAL_STATUSES = frozenset({
    "completed", "failed", "cancelled", "expired", "lost", "killed", "exited",
})
RUNNER_FAILURE_TERMINAL_STATUSES = frozenset(
    RUNNER_TERMINAL_STATUSES - {"completed"}
)
RUNNER_BIND_ERROR = "runner_bind_incomplete"
RUNNER_INJECT_ERROR = "wrong_session"
DIRECT_SESSION_TOKEN_TTL_S = 4 * 60 * 60
SERVER_RELAY_FAILURE_SCHEMA = "switchboard.server_relay_failure.v1"
SERVER_RELAY_FAILURE_KIND = "runner.server_relay_unavailable"
SERVER_RELAY_FAILURE_DEDUPE_S = 300

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
    "terminal_task_cleanup_for_host_in",
    "runner_bind_tuple",
    "missing_runner_bind_fields",
    "runner_bind_incomplete",
    "is_credential_preclaim_runner",
    "requires_full_runner_bind",
    "is_native_assignment_runner",
    "issue_direct_session_mcp_token",
    "get_direct_session_principal_by_token_any_project",
    "check_agent_host_bootstrap_authority",
    "check_direct_task_completion_authority",
    "assert_runner_watchable",
    "latest_dispatch_outcome",
    "TERMINAL_RUNNER_STATES",
    "terminalize_wake_runners_in",
    "resolve_task_active_runner",
    "resolve_runner_watch",
    "record_server_relay_failure",
    "_clear_active_runner_pointer_in",
    "request_runner_control",
    "list_runner_control_requests",
    "claim_runner_control_request",
    "complete_runner_control_request",
]


def _server_relay_failure_event(session: Dict[str, Any],
                                failure: Dict[str, Any]) -> Dict[str, Any]:
    """Build the non-secret audit payload for a failed relay-ticket mint."""
    return {
        "schema": SERVER_RELAY_FAILURE_SCHEMA,
        "runner_session_id": str(session.get("runner_session_id") or ""),
        "task_id": str(session.get("task_id") or ""),
        "host_id": str(session.get("host_id") or ""),
        "error": str(failure.get("error") or "server_relay_unavailable"),
        "missing": sorted({
            str(field) for field in (failure.get("missing") or []) if str(field)
        }),
        "failure_class": "hidden_fallback",
    }


def _record_server_relay_failure_in(c: sqlite3.Connection,
                                    session: Dict[str, Any],
                                    failure: Dict[str, Any], *, actor: str,
                                    now: Optional[float] = None) -> Dict[str, Any]:
    """Persist a bounded activity signal without flooding every heartbeat."""
    now = time.time() if now is None else float(now)
    event = _server_relay_failure_event(session, failure)
    recent = c.execute(
        "SELECT payload FROM activity WHERE kind=? AND task_id IS ? AND created_at>=? "
        "ORDER BY id DESC LIMIT 25",
        (SERVER_RELAY_FAILURE_KIND, event.get("task_id") or None,
         now - SERVER_RELAY_FAILURE_DEDUPE_S),
    ).fetchall()
    signature = (
        event.get("runner_session_id"), event.get("error"),
        tuple(event.get("missing") or []),
    )
    for row in recent:
        prior = _json_obj(row["payload"], {})
        if (prior.get("runner_session_id"), prior.get("error"),
                tuple(prior.get("missing") or [])) == signature:
            return {**event, "recorded": False, "reason": "dedupe_window"}
    c.execute(
        "INSERT INTO activity(task_id,actor,kind,payload,created_at) VALUES (?,?,?,?,?)",
        (event.get("task_id") or None, actor, SERVER_RELAY_FAILURE_KIND,
         json.dumps(event, sort_keys=True), now),
    )
    return {**event, "recorded": True}


def record_server_relay_failure(session: Dict[str, Any], failure: Dict[str, Any],
                                *, actor: str = "system",
                                project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Record a structured server-side signal when no relay URL was minted."""
    with _conn(project) as c:
        return _record_server_relay_failure_in(
            c, session, failure, actor=actor)


def terminal_task_cleanup_for_host_in(
        c: sqlite3.Connection, host_id: str, actor: str,
        now: Optional[float] = None) -> Dict[str, Any]:
    """Project terminal task truth into its live host-owned execution rows.

    The host consumes these directives and kills the supervised process. Until
    it acknowledges that kill, the central runner stays non-terminal so a lost
    heartbeat response is retried. Work Sessions and leases close immediately
    because the task is already the stronger lifecycle authority.
    """
    now = time.time() if now is None else float(now)
    host_id = str(host_id or "").strip()
    empty = {
        "schema": "switchboard.terminal_runner_cleanup.v1",
        "host_id": host_id,
        "sessions": [],
        "session_count": 0,
        "closed_work_session_count": 0,
        "released_resource_lease_count": 0,
        "released_file_lease_count": 0,
    }
    if not host_id:
        return empty
    terminal_tasks = ("Done", "Cancelled", "Canceled")
    terminal_runners = tuple(sorted(RUNNER_TERMINAL_STATUSES))
    task_marks = ",".join("?" for _ in terminal_tasks)
    runner_marks = ",".join("?" for _ in terminal_runners)
    rows = c.execute(
        "SELECT r.runner_session_id,r.task_id,r.status,r.metadata_json,t.status AS task_status "
        "FROM runner_sessions r JOIN tasks t ON t.task_id=r.task_id "
        f"WHERE r.host_id=? AND t.status IN ({task_marks}) "
        f"AND lower(r.status) NOT IN ({runner_marks}) "
        "ORDER BY r.started_at,r.runner_session_id",
        (host_id, *terminal_tasks, *terminal_runners),
    ).fetchall()
    task_statuses = {
        str(row["task_id"] or ""): str(row["task_status"] or "")
        for row in rows if row["task_id"]
    }
    closed_work_sessions = 0
    released_resource_leases = 0
    released_file_leases = 0
    for task_id, task_status in sorted(task_statuses.items()):
        changed = c.execute(
            "UPDATE work_sessions SET status='completed', completed_at=COALESCE(completed_at,?), "
            "updated_at=?, updated_by=? WHERE task_id=? "
            "AND status IN ('active','proposed','blocked')",
            (now, now, actor, task_id),
        )
        closed_work_sessions += changed.rowcount
        released_resource_leases += c.execute(
            "UPDATE resource_leases SET released_at=? WHERE task_id=? AND released_at IS NULL",
            (now, task_id),
        ).rowcount
        released_file_leases += c.execute(
            "UPDATE file_leases SET released_at=? WHERE task_id=? AND released_at IS NULL",
            (now, task_id),
        ).rowcount
        if changed.rowcount:
            c.execute(
                "INSERT INTO activity(task_id,actor,kind,payload,created_at) VALUES (?,?,?,?,?)",
                (task_id, actor, "work_session.completed_by_terminal_task",
                 json.dumps({"closed_count": changed.rowcount,
                             "terminal_status": task_status}, sort_keys=True), now),
            )
    directives: List[Dict[str, Any]] = []
    for row in rows:
        metadata = _json_obj(row["metadata_json"], {})
        first_request = not metadata.get("terminal_cleanup_requested_at")
        metadata.update({
            "terminal_cleanup_requested_at": (
                metadata.get("terminal_cleanup_requested_at") or now),
            "terminal_cleanup_task_status": row["task_status"],
        })
        c.execute(
            "UPDATE runner_sessions SET metadata_json=?,updated_at=? WHERE runner_session_id=?",
            (json.dumps(metadata, sort_keys=True), now, row["runner_session_id"]),
        )
        directive = {
            "runner_session_id": row["runner_session_id"],
            "task_id": row["task_id"],
            "task_status": row["task_status"],
            "runner_status": row["status"],
            "action": "kill",
            "reason": f"task is terminal: {row['task_status']}",
        }
        directives.append(directive)
        if first_request:
            c.execute(
                "INSERT INTO activity(task_id,actor,kind,payload,created_at) VALUES (?,?,?,?,?)",
                (row["task_id"], actor, "runner.terminal_cleanup_requested",
                 json.dumps(directive, sort_keys=True), now),
            )
    return {
        **empty,
        "sessions": directives,
        "session_count": len(directives),
        "closed_work_session_count": closed_work_sessions,
        "released_resource_lease_count": released_resource_leases,
        "released_file_lease_count": released_file_leases,
    }


def check_direct_task_completion_authority(
        binding: Dict[str, Any], *, principal_id: str,
        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Authorize completion of one no-claim native-host wake.

    Direct task wakes use the selected host on a pending wake. Connect wakes use
    the host that atomically claimed the assignment. Both require the already-
    registered native runner; the request body is only a lookup tuple.
    """
    supplied = {key: str((binding or {}).get(key) or "").strip() for key in (
        "wake_id", "host_id", "runner_session_id", "task_id", "agent_id",
    )}
    supplied["task_id"] = supplied["task_id"].upper()
    missing = sorted(key for key, value in supplied.items() if not value)
    if missing:
        return {
            "allowed": False,
            "error_code": "direct_task_completion_binding_incomplete",
            "missing": missing,
        }

    with _conn(project) as c:
        wake = c.execute(
            "SELECT status, claimed_by_host, task_id, selector_json, policy_json "
            "FROM wake_intents WHERE wake_id=?",
            (supplied["wake_id"],),
        ).fetchone()
        runner = c.execute(
            "SELECT host_id, agent_id, runtime, task_id, status, metadata_json, "
            "principal_id FROM runner_sessions WHERE runner_session_id=?",
            (supplied["runner_session_id"],),
        ).fetchone()

    reasons: List[str] = []
    selector = _json_obj(wake["selector_json"], {}) if wake else {}
    policy = _json_obj(wake["policy_json"], {}) if wake else {}
    metadata = _json_obj(runner["metadata_json"], {}) if runner else {}
    if not wake or str(wake["status"] or "") not in {
            "pending", "claimed", "completed"}:
        reasons.append("direct_wake_not_active")
    direct = bool(
        wake and policy.get("mode") == "direct_task"
            and policy.get("execution_mode") == "direct_personal_cli"
            and policy.get("require_runner_bind") is False)
    assignment = policy.get("assignment") or {}
    connect = bool(
        wake and policy.get("mode") == "connect"
        and assignment.get("schema") == "switchboard.connect.assignment.v1")
    if wake and not (direct or connect):
        reasons.append("wake_not_direct_personal_cli")
    expected_wake = {
        "host_id": supplied["host_id"],
        "task_id": supplied["task_id"],
        "agent_id": supplied["agent_id"],
    }
    actual_wake = {
        "host_id": str(
            wake["claimed_by_host"] if connect else selector.get("host_id") or ""),
        "task_id": str(wake["task_id"] or selector.get("task_id") or "").upper()
        if wake else "",
        "agent_id": str(selector.get("agent_id") or ""),
    }
    for field, expected in expected_wake.items():
        if actual_wake[field] != expected:
            reasons.append(f"wake_{field}_mismatch")
    if not runner:
        reasons.append("direct_runner_not_found")
    else:
        expected_runner = {
            "host_id": supplied["host_id"],
            "agent_id": supplied["agent_id"],
            "runtime": str(selector.get("runtime") or ""),
            "task_id": supplied["task_id"],
            "principal_id": str(principal_id or ""),
        }
        for field, expected in expected_runner.items():
            actual = str(runner[field] or "")
            if field == "task_id":
                actual = actual.upper()
            if actual != expected:
                reasons.append(f"runner_{field}_mismatch")
        if str(runner["status"] or "").lower() not in {"ready", "running"}:
            reasons.append("direct_runner_not_live")
        assignment_matches = (
            metadata.get("direct_assignment") is True if direct else
            metadata.get("connect_assignment") is True
            and metadata.get("assignment_schema")
            == "switchboard.connect.assignment.v1"
            and str(metadata.get("assignment_id") or "")
            == str(assignment.get("assignment_id") or "")
        )
        if (str(metadata.get("wake_id") or "") != supplied["wake_id"]
                or not assignment_matches
                or (connect
                    and metadata.get("native_host_execution") is not True)):
            reasons.append("direct_runner_metadata_mismatch")
    if reasons:
        return {
            "allowed": False,
            "error_code": "direct_task_completion_binding_denied",
            "reason_codes": sorted(set(reasons)),
        }
    return {
        "allowed": True,
        "schema": "switchboard.direct_task_completion_authority.v1",
        **supplied,
    }


def issue_direct_session_mcp_token(
        wake_id: str, host_id: str, runner_session_id: str, *,
        principal_id: str, actor: str = "agent-host",
        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Issue one short-lived task agent bearer for an exact native assignment.

    Both legacy direct-task and Connect launches run under the enrolled host
    until this bearer is minted.  Keep the host token narrow, and bind the
    returned credential to the exact wake, host, runner, task, and agent.
    """
    now = time.time()
    wake_id = str(wake_id or "").strip()
    host_id = str(host_id or "").strip()
    runner_session_id = str(runner_session_id or "").strip()
    principal_id = str(principal_id or "").strip()
    expected_runner = "run_" + hashlib.sha256(
        f"{wake_id}:{host_id}".encode()).hexdigest()[:16]
    with _conn(project) as c:
        wake_row = c.execute(
            "SELECT * FROM wake_intents WHERE wake_id=?", (wake_id,)).fetchone()
        if not wake_row:
            return {"error": "direct_assignment_not_found"}
        wake = dict(wake_row)
        selector = _json_obj(wake.get("selector_json"), {})
        policy = _json_obj(wake.get("policy_json"), {})
        assignment = policy.get("assignment") or {}
        enrollment = c.execute(
            "SELECT enrollment_id FROM agent_host_enrollments "
            "WHERE host_id=? AND principal_id=? AND status='active'",
            (host_id, principal_id),
        ).fetchone()
        reasons = []
        if str(wake.get("status") or "") not in {"pending", "claimed", "completed"}:
            reasons.append("assignment_not_active")
        direct = bool(
            policy.get("mode") == "direct_task"
            and policy.get("execution_mode") == "direct_personal_cli")
        connect = bool(
            policy.get("mode") == "connect"
            and assignment.get("schema") == "switchboard.connect.assignment.v1")
        if not (direct or connect):
            reasons.append("assignment_mode_mismatch")
        selected_host = str(
            wake.get("claimed_by_host") if connect else selector.get("host_id") or "")
        if selected_host != host_id:
            reasons.append("assignment_host_mismatch")
        if direct:
            if str(assignment.get("host_id") or "") != host_id:
                reasons.append("config_host_mismatch")
            if str(assignment.get("task_id") or "") != str(wake.get("task_id") or ""):
                reasons.append("config_task_mismatch")
        else:
            work_ref = str(assignment.get("work_ref") or "")
            expected_work_ref = f"task:{project}:{wake.get('task_id') or ''}"
            if work_ref != expected_work_ref:
                reasons.append("config_task_mismatch")
        if runner_session_id != expected_runner:
            reasons.append("runner_session_mismatch")
        if not enrollment:
            reasons.append("host_enrollment_mismatch")
        if reasons:
            return {"error": "direct_assignment_token_denied",
                    "reason_codes": sorted(reasons)}

        raw_token = "dst-" + uuid.uuid4().hex
        expires_at = now + DIRECT_SESSION_TOKEN_TTL_S
        c.execute(
            "UPDATE direct_session_tokens SET revoked_at=? "
            "WHERE runner_session_id=? AND revoked_at IS NULL",
            (now, runner_session_id),
        )
        c.execute(
            "INSERT INTO direct_session_tokens(token_hash,project_id,task_id,agent_id,"
            "host_id,wake_id,runner_session_id,issued_at,expires_at,revoked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,NULL)",
            (hash_token(raw_token), project, str(wake.get("task_id") or ""),
             str(selector.get("agent_id") or ""), host_id, wake_id,
             runner_session_id, now, expires_at),
        )
        c.execute(
            "INSERT INTO activity(task_id,actor,kind,payload,created_at) VALUES (?,?,?,?,?)",
            (wake.get("task_id"), actor, "direct_session.mcp_token_issued",
             json.dumps({"wake_id": wake_id, "host_id": host_id,
                         "runner_session_id": runner_session_id,
                         "token_returned_once": True}, sort_keys=True), now),
        )
    return {
        "issued": True,
        "task_id": str(wake.get("task_id") or ""),
        "agent_id": str(selector.get("agent_id") or ""),
        "runner_session_id": runner_session_id,
        "expires_at": expires_at,
        "token": raw_token,
        "token_returned_once": True,
    }


def _sync_direct_session_token_lease_in(
        c: sqlite3.Connection, record: Dict[str, Any], metadata: Dict[str, Any],
        runner_session_id: str, now: float) -> None:
    """Keep one task-bound token aligned with its direct runner's lifetime."""
    if metadata.get("direct_assignment") is not True:
        return
    binding = (
        runner_session_id,
        str(record.get("task_id") or ""),
        str(record.get("host_id") or ""),
        str(record.get("agent_id") or ""),
    )
    if not all(binding):
        return
    status = str(record.get("status") or "").strip().lower()
    where = (
        "runner_session_id=? AND task_id=? AND host_id=? AND agent_id=? "
        "AND revoked_at IS NULL"
    )
    if status in RUNNER_WATCHABLE_STATUSES:
        c.execute(
            f"UPDATE direct_session_tokens SET expires_at=? WHERE {where}",
            (now + DIRECT_SESSION_TOKEN_TTL_S, *binding),
        )
    elif status in RUNNER_TERMINAL_STATUSES:
        c.execute(
            f"UPDATE direct_session_tokens SET revoked_at=? WHERE {where}",
            (now, *binding),
        )


def get_direct_session_principal_by_token_any_project(
        token: str) -> Optional[Dict[str, Any]]:
    """Resolve a live direct-session bearer into its operator MCP principal."""
    token_hash = hash_token(str(token or "").strip())
    if not token_hash:
        return None
    now = time.time()
    for project_id in _store_facade().project_ids():
        try:
            with _conn(project_id) as c:
                row = c.execute(
                    "SELECT * FROM direct_session_tokens WHERE token_hash=? "
                    "AND revoked_at IS NULL AND expires_at>?",
                    (token_hash, now),
                ).fetchone()
        except sqlite3.OperationalError:
            continue
        if row:
            value = dict(row)
            return {
                "id": f"direct-session/{value['runner_session_id']}",
                "kind": "direct_session",
                "display_name": value["agent_id"],
                "project": "*",
                "environment_operator": True,
                # A direct CLI is the same autonomous operator used by desktop MCP.
                # Assignment fields remain durable attribution and token-lifecycle
                # metadata; they do not reduce the operator's authority.
                "scopes": list(MCP_OPERATOR_SCOPES),
                "assignment_project": value["project_id"],
                "bound_task_id": value["task_id"],
                "bound_agent_id": value["agent_id"],
                "bound_host_id": value["host_id"],
                "bound_wake_id": value["wake_id"],
                "bound_runner_session_id": value["runner_session_id"],
                "expires_at": value["expires_at"],
            }
    return None


def check_agent_host_bootstrap_authority(
        binding: Dict[str, Any], *, principal_id: str,
        project: str = DEFAULT_PROJECT, work_session_id: str = "",
        action: str = "create_work_session") -> Dict[str, Any]:
    """Authorize one narrow host mutation from an exact claimed generic wake.

    Generic Autopilot wakes intentionally start with a preclaim runner and no task
    claim or Work Session.  A narrow Agent Host bearer may create those two records
    only for the wake it already claimed and the preclaim runner it already owns.
    Every submitted field is compared with durable server state; the binding is
    never accepted as authority on its own.
    """
    supplied = {key: str((binding or {}).get(key) or "").strip() for key in (
        "wake_id", "host_id", "runner_session_id", "task_id", "agent_id",
    )}
    supplied["task_id"] = supplied["task_id"].upper()
    missing = sorted(key for key, value in supplied.items() if not value)
    if missing:
        return {
            "allowed": False,
            "error_code": "agent_host_bootstrap_binding_incomplete",
            "missing": missing,
        }
    if action not in {
            "create_work_session", "claim_task", "expire_work_session",
            "complete_wake", "register_agent", "heartbeat_agent"}:
        return {"allowed": False, "error_code": "agent_host_bootstrap_action_denied"}

    now = time.time()
    with _conn(project) as c:
        wake = c.execute(
            "SELECT status, claimed_by_host, task_id, selector_json, policy_json "
            "FROM wake_intents WHERE wake_id=?",
            (supplied["wake_id"],),
        ).fetchone()
        runner = c.execute(
            "SELECT host_id, agent_id, runtime, task_id, claim_id, status, "
            "metadata_json, principal_id, heartbeat_at, heartbeat_ttl_s "
            "FROM runner_sessions WHERE runner_session_id=?",
            (supplied["runner_session_id"],),
        ).fetchone()
        session = None
        if work_session_id:
            session = c.execute(
                "SELECT work_session_id, task_id, agent_id, principal_id, status, "
                "claim_id FROM work_sessions WHERE work_session_id=?",
                (str(work_session_id).strip(),),
            ).fetchone()

    reasons: List[str] = []
    selector = _json_obj(wake["selector_json"], {}) if wake else {}
    policy = _json_obj(wake["policy_json"], {}) if wake else {}
    metadata = _json_obj(runner["metadata_json"], {}) if runner else {}
    wake_status = str(wake["status"] or "") if wake else ""
    allowed_wake_statuses = (
        {"claimed", "completed", "failed", "cancelled", "expired"}
        if action == "expire_work_session" else
        ({"claimed", "completed"}
         if action in {"complete_wake", "heartbeat_agent"} else {"claimed"})
    )
    if (not wake or wake_status not in allowed_wake_statuses
            or str(wake["claimed_by_host"] or "") != supplied["host_id"]):
        reasons.append("wake_not_claimed_by_host")
    if wake and str(wake["task_id"] or selector.get("task_id") or "").upper() \
            != supplied["task_id"]:
        reasons.append("wake_task_mismatch")
    if wake and str(selector.get("agent_id") or "") != supplied["agent_id"]:
        reasons.append("wake_agent_mismatch")
    if wake and policy.get("require_runner_bind") is not True:
        reasons.append("wake_runner_bind_not_required")
    if wake and (policy.get("account_binding")
                 or (policy.get("execution_binding") or {}).get("execution_connection_id")):
        reasons.append("exact_personal_wake_uses_prebound_path")
    if not runner:
        reasons.append("preclaim_runner_not_found")
    else:
        expected_runner = {
            "host_id": supplied["host_id"],
            "agent_id": supplied["agent_id"],
            "runtime": str(selector.get("runtime") or ""),
            "task_id": supplied["task_id"],
            "principal_id": str(principal_id or ""),
        }
        for field, expected in expected_runner.items():
            actual = str(runner[field] or "")
            if field == "task_id":
                actual = actual.upper()
            if actual != expected:
                reasons.append(f"runner_{field}_mismatch")
        claim_bound_heartbeat = (
            action == "heartbeat_agent"
            and bool(str(runner["claim_id"] or ""))
            and bool(str(metadata.get("work_session_id") or ""))
            and str(metadata.get("credential_admission_phase") or "") == "claim_bound"
            and str(runner["status"] or "").lower() in {"ready", "running"}
        )
        if (action not in {"expire_work_session", "complete_wake", "heartbeat_agent"}
                and str(runner["claim_id"] or "")):
            reasons.append("runner_already_claim_bound")
        runner_status = str(runner["status"] or "").lower()
        if action == "complete_wake":
            claim_bound = bool(str(runner["claim_id"] or ""))
            session_bound = bool(str(metadata.get("work_session_id") or ""))
            if claim_bound != session_bound:
                reasons.append("runner_completion_binding_partial")
            completion_phase = str(
                metadata.get("credential_admission_phase") or "")
            allowed_phases = (
                {"claim_bound"} if claim_bound
                else {"preclaim", "preclaim_failed"}
            )
            if completion_phase not in allowed_phases:
                reasons.append("runner_completion_phase_invalid")
            allowed_completion_statuses = (
                {"ready", "running", "exited", "failed", "completed",
                 "cancelled", "killed"}
                if claim_bound else {"exited", "failed"}
            )
            if runner_status not in allowed_completion_statuses:
                reasons.append("runner_completion_status_invalid")
        elif (action != "expire_work_session" and not claim_bound_heartbeat
              and runner_status != "starting"):
            reasons.append("runner_not_preclaim_starting")
        if (action != "complete_wake" and not claim_bound_heartbeat
                and str(metadata.get("credential_admission_phase") or "") != "preclaim"):
            reasons.append("runner_preclaim_metadata_mismatch")
        if str(metadata.get("wake_id") or "") != supplied["wake_id"]:
            reasons.append("runner_preclaim_metadata_mismatch")
        if action not in {"expire_work_session", "complete_wake"} and float(runner["heartbeat_at"] or 0) \
                + max(10, int(runner["heartbeat_ttl_s"] or 60)) <= now:
            reasons.append("runner_preclaim_stale")
    if action in {"claim_task", "expire_work_session"}:
        if not session:
            reasons.append("bootstrap_work_session_not_found")
        else:
            expected_session = {
                "task_id": supplied["task_id"],
                "agent_id": supplied["agent_id"],
                "principal_id": str(principal_id or ""),
                "status": "active",
            }
            for field, expected in expected_session.items():
                actual = str(session[field] or "")
                if field == "task_id":
                    actual = actual.upper()
                if actual != expected:
                    reasons.append(f"work_session_{field}_mismatch")
            if action == "claim_task" and str(session["claim_id"] or ""):
                reasons.append("work_session_already_claim_bound")
    if reasons:
        return {
            "allowed": False,
            "error_code": "agent_host_bootstrap_binding_denied",
            "reason_codes": sorted(set(reasons)),
        }
    return {
        "allowed": True,
        "schema": "switchboard.agent_host_bootstrap_authority.v1",
        "action": action,
        **supplied,
        "runtime": str(runner["runtime"] or "") if runner else None,
        "work_session_id": str(work_session_id or "") or None,
    }


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


def is_credential_preclaim_runner(record: Dict[str, Any]) -> bool:
    """Return whether credential admission has explicitly not reached a claim.

    This is an admission-state predicate, not a transport or liveness predicate.
    In particular, the presence of a claim must never change what kind of PTY a
    runner is or whether its relay can be renewed.
    """
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    phase = str(metadata.get("credential_admission_phase") or "").strip().lower()
    return phase in {"preclaim", "preclaim_failed", "pending"}


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


def is_native_assignment_runner(record: Dict[str, Any]) -> bool:
    """Classify an explicitly identified native host assignment.

    Transport identity comes only from immutable assignment metadata.  Claim and
    Work Session fields are deliberately absent from this predicate: attaching
    scheduler state to a running process must not revoke its PTY capabilities.
    """
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    if not metadata and record.get("metadata_json"):
        metadata = _json_obj(record.get("metadata_json"), {})
    bind = runner_bind_tuple(record)
    direct = (
        metadata.get("direct_assignment") is True
        and metadata.get("assignment_schema") == "switchboard.direct_cli_assignment.v1"
    )
    connect = (
        metadata.get("connect_assignment") is True
        and metadata.get("assignment_schema") == "switchboard.connect.assignment.v1"
    )
    return bool(
        (direct or connect)
        and metadata.get("native_host_execution") is True
        and bind.get("task_id")
        and bind.get("host_id", "").startswith("host/")
        and bind.get("wake_id")
    )


def is_connect_assignment_runner(record: Dict[str, Any]) -> bool:
    """True for a host-bound PTY launched by the content-blind Connect plane.

    Connect intentionally starts before task claims and Work Sessions exist.  Its
    durable identity is therefore task + host + wake + assignment, while the live
    relay proves transport readiness.  Requiring claim/work-session fields here
    makes a successfully attached Connect process invisible until an unrelated
    lifecycle catches up (BUG-130).
    """
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    if not metadata and record.get("metadata_json"):
        metadata = _json_obj(record.get("metadata_json"), {})
    bind = runner_bind_tuple(record)
    return bool(
        metadata.get("connect_assignment") is True
        and metadata.get("native_host_execution") is True
        and metadata.get("assignment_schema") == "switchboard.connect.assignment.v1"
        and bind.get("task_id")
        and bind.get("host_id", "").startswith("host/")
        and bind.get("wake_id")
    )


#: Identity fields that authorize *which* task/host/wake a Watch/Chat opens.
#: These stay required even when relay attachment proves liveness (WATCH-4):
#: attachment answers "is it live", identity answers "is it the right run".
RUNNER_AUTHZ_FIELDS = ("task_id", "host_id", "wake_id")


def _missing_runner_authz_fields(session: Dict[str, Any]) -> List[str]:
    """Identity-only bind check: task/host/wake, with the host_id shape rule.

    Unlike :func:`missing_runner_bind_fields` this never requires claim_id or
    work_session_id -- those are scheduler/code-write authority, not the identity
    a terminal watcher needs.
    """
    bind = runner_bind_tuple(session)
    missing = [name for name in RUNNER_AUTHZ_FIELDS if not bind.get(name)]
    host_id = bind.get("host_id") or ""
    if host_id and not (
            host_id.startswith("host/") and len(host_id) > len("host/")
    ) and "host_id" not in missing:
        missing.append("host_id")
    return missing


def assert_runner_watchable(session: Optional[Dict[str, Any]], *,
                            host_attached: Optional[bool] = None) -> Dict[str, Any]:
    """Fail closed: Watch/Chat may open only for a live runner.

    ``host_attached`` is the WATCH-4 liveness signal, resolved from the RelayHub's
    per-session host-tunnel state (``session_info()['host_attached']``). It is a
    tri-state:

      * ``True``  -- a host tunnel is attached: the run is watchable regardless of
        its scheduler claim-binding state (Connect runs carry no claim/work_session
        by design). Identity (task/host/wake) is still required for authorization.
      * ``False`` -- the relay has no attached tunnel: a row that otherwise looks
        live is refused ``host_not_attached`` rather than presenting a dead pipe as
        watchable.
      * ``None``  -- no attachment signal available (e.g. a caller that cannot query
        the hub): fall back to the DB-row assignment/bind-tuple inference
        (explicit native assignment metadata), so
        callers that cannot see the relay keep today's behaviour.
    """
    if not session:
        return runner_bind_incomplete(list(RUNNER_BIND_FIELDS))
    if session.get("stale"):
        return runner_bind_incomplete(
            list(RUNNER_BIND_FIELDS),
            runner_session_id=str(session.get("runner_session_id") or ""),
            task_id=str(session.get("task_id") or ""),
        ) | {"message": "Runner session is stale; Watch/Chat refused until a live bind exists"}
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    control = session.get("control") if isinstance(session.get("control"), dict) else {}
    if metadata.get("native_host_execution") is True and control.get("runner_open"):
        transport_missing = []
        if metadata.get("pty") is not True:
            transport_missing.append("pty")
        if transport_missing:
            return runner_bind_incomplete(
                transport_missing,
                runner_session_id=str(session.get("runner_session_id") or ""),
                task_id=str(session.get("task_id") or ""),
            ) | {
                "error": "runner_stream_not_ready",
                "error_code": "runner_stream_not_ready",
                "message": "Runner is bound but its live PTY relay is not ready",
            }
    # WATCH-4: when the live relay attachment state is supplied, it is the primary
    # liveness signal -- it overrides DB-row assignment inference. Bind-tuple checks
    # drop to identity-only (authorization).
    if host_attached is not None:
        authz_missing = _missing_runner_authz_fields(session)
        if authz_missing:
            return runner_bind_incomplete(
                authz_missing,
                runner_session_id=str(session.get("runner_session_id") or ""),
                task_id=str(session.get("task_id") or ""),
            )
        status = str(session.get("status") or "").strip().lower()
        if host_attached:
            if status not in RUNNER_WATCHABLE_STATUSES:
                return runner_bind_incomplete(
                    ["live_status"],
                    runner_session_id=str(session.get("runner_session_id") or ""),
                    task_id=str(session.get("task_id") or ""),
                ) | {"message": f"Runner status {status or 'unknown'} is not watchable"}
            bind = runner_bind_tuple(session)
            return {
                "watchable": True,
                "refused": False,
                "runner_session_id": session.get("runner_session_id"),
                "task_id": bind["task_id"],
                "bind": bind,
                "session": session,
                "binding_mode": "relay_attached",
                "host_attached": True,
            }
        # host_attached is False. A row that still claims a live status but has no
        # attached tunnel is the dark-runner case: name it, do not present it as
        # watchable -- this is exactly what the BUG-130 assignment-shape recognition
        # below could NOT catch (a live-looking row with a dead pipe). A not-yet-live
        # row falls through to assignment inference so a starting run is not
        # mislabelled as detached.
        if status in RUNNER_WATCHABLE_STATUSES:
            return runner_bind_incomplete(
                [],
                runner_session_id=str(session.get("runner_session_id") or ""),
                task_id=str(session.get("task_id") or ""),
            ) | {
                "error": "host_not_attached",
                "error_code": "host_not_attached",
                "failure_class": "unreachable_agent",
                "host_attached": False,
                "message": ("Runner row is live but no host tunnel is attached to "
                            "the relay; Watch/Chat refused until it reconnects"),
            }
    # No relay-attachment signal (e.g. an off-process caller): fall back to BUG-130
    # DB-row assignment recognition, then the full bind tuple.
    if is_native_assignment_runner(session):
        status = str(session.get("status") or "").strip().lower()
        if status not in RUNNER_WATCHABLE_STATUSES:
            return runner_bind_incomplete(
                ["live_status"],
                runner_session_id=str(session.get("runner_session_id") or ""),
                task_id=str(session.get("task_id") or ""),
            ) | {"message": f"Assignment runner status {status or 'unknown'} is not watchable"}
        return {
            "watchable": True,
            "refused": False,
            "runner_session_id": session.get("runner_session_id"),
            "task_id": str(session.get("task_id") or ""),
            "bind": runner_bind_tuple(session),
            "session": session,
            "binding_mode": "native_assignment",
        }
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


STALE_PENDING_WAKE_S = float(os.environ.get("PM_STALE_PENDING_WAKE_S", "1800") or 1800)


def latest_dispatch_outcome(task_id: str, *,
                            project: str = DEFAULT_PROJECT,
                            now: Optional[float] = None) -> Dict[str, Any]:
    """Why this task has no runner, in the dispatcher's own words.

    BUG-91: when Watch/Chat refuses, the operator needs the reason the run never
    started -- "capacity exhausted for co-general: cap=4" -- not a description of
    the debris that failure left behind. The dispatcher already records that on
    the wake; this surfaces it next to the refusal.

    Also reports a wake that has sat queued too long. One SEG-2 wake stayed
    pending for 30.7 hours across six dispatch attempts while silently
    accumulating runner rows; that needs to read as "needs attention", not as a
    task that simply has no runner.
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        return {}
    now = time.time() if now is None else now
    with _conn(project) as c:
        rows = c.execute(
            "SELECT wake_id, status, requested_at, claimed_at, completed_at, "
            "claimed_by_host, result_json, policy_json FROM wake_intents "
            "WHERE task_id=? ORDER BY COALESCE(requested_at,0) DESC LIMIT 20",
            (task_id,),
        ).fetchall()
    attempts = 0
    for row in rows:
        status = str(row["status"] or "")
        if status in {"pending", "claimed"}:
            waiting_s = max(0.0, now - float(row["requested_at"] or now))
            policy = _json_obj(row["policy_json"], {})
            attempts = max(attempts, int(policy.get("dispatch_attempt") or 0))
            return {
                "state": "needs_attention" if waiting_s >= STALE_PENDING_WAKE_S
                         else ("dispatching" if status == "claimed" else "queued"),
                "wake_id": str(row["wake_id"] or ""),
                "wake_status": status,
                "waiting_seconds": round(waiting_s),
                "dispatch_attempt": attempts,
                "host_id": str(row["claimed_by_host"] or ""),
                "message": (
                    f"No run has started yet — queued {round(waiting_s / 3600, 1)}h"
                    f"{f' across {attempts} dispatch attempts' if attempts else ''}."
                    if waiting_s >= STALE_PENDING_WAKE_S
                    else "A run is being dispatched for this task."
                ),
            }
        if status == "failed":
            result = _json_obj(row["result_json"], {})
            reason = str(result.get("reason") or result.get("error") or "").strip()
            policy = _json_obj(row["policy_json"], {})
            return {
                "state": str(result.get("failure_class") or "launch_failed"),
                "wake_id": str(row["wake_id"] or ""),
                "wake_status": status,
                "failure_class": str(result.get("failure_class") or ""),
                "reason": reason,
                "failed_at": result.get("failed_at") or row["completed_at"],
                "dispatch_attempt": int(policy.get("dispatch_attempt") or 0),
                "host_id": str(row["claimed_by_host"] or ""),
                "message": (f"The last dispatch failed: {reason}" if reason
                            else "The last dispatch failed before any run started."),
            }
        if status == "completed":
            return {}
    return {}


def resolve_runner_watch(task_id: str, *, include_stale: bool = False,
                         project: str = DEFAULT_PROJECT,
                         attachment: Optional[Callable[[str], Optional[bool]]] = None
                         ) -> Dict[str, Any]:
    """Pick a Watch/Chat-ready runner for a task, or return a typed refusal.

    UI-17 and Mission panel open only through this gate: listing alone is not enough
    when rows exist but the bind tuple is incomplete.

    ``attachment`` (WATCH-4) is an optional resolver mapping a runner_session_id to
    its live host-tunnel state (True/False/None); when supplied it is the primary
    liveness signal per session. It defaults to ``None`` so callers that cannot see
    the relay keep DB-row inference unchanged.
    """
    task_id = (task_id or "").strip()
    if not task_id:
        return runner_bind_incomplete(["task_id"]) | {
            "message": "task_id is required to open Watch/Chat",
        }
    sessions = list_runner_sessions(
        task_id=task_id, include_stale=include_stale, project=project)
    if not sessions:
        # BUG-91: name the dispatch failure, not the absence it caused.
        dispatch = latest_dispatch_outcome(task_id, project=project)
        return runner_bind_incomplete(list(RUNNER_BIND_FIELDS), task_id=task_id) | {
            "message": dispatch.get("message")
            or "No runner sessions are registered for this task",
            "sessions": [],
        } | ({"dispatch": dispatch} if dispatch else {})
    refusals: List[Dict[str, Any]] = []
    for session in sessions:
        host_attached = None
        if attachment is not None:
            try:
                host_attached = attachment(str(session.get("runner_session_id") or ""))
            except Exception:
                host_attached = None
        verdict = assert_runner_watchable(session, host_attached=host_attached)
        if verdict.get("watchable"):
            return {
                **verdict,
                "sessions": sessions,
                "enough_for_panel": True,
            }
        refusals.append(verdict)
    best = refusals[0] if refusals else runner_bind_incomplete(
        list(RUNNER_BIND_FIELDS), task_id=task_id)
    # Every row for this task is unwatchable. If a dispatch explains why nothing
    # is running, that reason outranks a description of the leftover row.
    dispatch = latest_dispatch_outcome(task_id, project=project)
    if dispatch.get("message"):
        best = {**best, "message": dispatch["message"]}
    return {
        **best,
        "sessions": sessions,
        "enough_for_panel": False,
        "candidates": len(sessions),
    } | ({"dispatch": dispatch} if dispatch else {})


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


TERMINAL_RUNNER_STATES = frozenset({
    "completed", "failed", "cancelled", "expired", "lost", "killed", "exited", "stopped",
})


def terminalize_wake_runners_in(c: sqlite3.Connection, wake_id: str, *,
                                reason: str = "", keep: str = "",
                                now: Optional[float] = None) -> List[str]:
    """Close out every non-terminal runner row left by one dispatch attempt.

    BUG-91: a dispatch that reports ``started: false`` (capacity exhausted,
    registration timeout, launch failure) never produced a task session, yet the
    supervised wrapper it raced had already published a ``runner_sessions`` row.
    Nothing terminalized that row, so it merely aged into ``stale`` and then
    ``expired`` while still being the newest thing the browser could find for the
    task — which is what made Watch/Chat refuse with a truthful-but-useless
    "Runner session is stale" instead of naming the real dispatch failure.

    ``keep`` preserves the runner a successful attempt actually bound, so a retry
    supersedes its predecessors without ever deleting evidence.

    Critically, this NEVER closes a claim-bound runner. Dispatch attempts against
    one wake overlap and finish out of order -- SEG-2 accumulated three runner
    rows for a single wake across two hosts and 31 hours. If attempt A has
    produced a real claim-bound session and slower attempt B then reports
    failure, B must not kill A's running work. Only a claim-bound session's own
    lifecycle (its process exiting, or its own completion) may terminalize it;
    a different attempt's receipt may only clear unbound wrapper debris.
    """
    wake_id = str(wake_id or "").strip()
    if not wake_id:
        return []
    now = time.time() if now is None else now
    keep = str(keep or "").strip()
    closed: List[str] = []
    rows = c.execute(
        "SELECT runner_session_id, task_id, status, claim_id, metadata_json "
        "FROM runner_sessions WHERE metadata_json LIKE ?",
        (f'%"{wake_id}"%',),
    ).fetchall()
    for row in rows:
        runner_session_id = str(row["runner_session_id"] or "")
        if not runner_session_id or runner_session_id == keep:
            continue
        metadata = _json_obj(row["metadata_json"], {})
        # LIKE is only a cheap prefilter; the wake id must match exactly.
        if str(metadata.get("wake_id") or "") != wake_id:
            continue
        if str(row["status"] or "").strip().lower() in TERMINAL_RUNNER_STATES:
            continue
        # A claim + Work Session means this row is a real session some attempt
        # actually established. Never collateral-damage it from another attempt.
        if (str(row["claim_id"] or "").strip()
                and str(metadata.get("work_session_id") or "").strip()):
            continue
        metadata["failure_reason"] = reason or "dispatch attempt never started"
        metadata["terminalized_by"] = "wake_failure"
        c.execute(
            "UPDATE runner_sessions SET status='failed', metadata_json=?, updated_at=? "
            "WHERE runner_session_id=?",
            (json.dumps(metadata, sort_keys=True), now, runner_session_id),
        )
        _clear_active_runner_pointer_in(c, str(row["task_id"] or ""),
                                        runner_session_id, now=now)
        closed.append(runner_session_id)
    return closed


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


def _release_terminal_runner_ownership_in(
        c: sqlite3.Connection, record: Dict[str, Any], metadata: Dict[str, Any],
        runner_session_id: str, actor: str, now: float) -> Optional[Dict[str, Any]]:
    """Release only the claim owned by one terminal managed runner.

    The runner row and Work Session remain immutable history.  The task receives a
    compact recovery handoff, then an In-Progress implementation returns to Ready
    while In-Review/Blocked workflow state is preserved.  Exact tuple checks make
    repeated terminal heartbeats idempotent and prevent an old runner from releasing
    a newer replacement's claim.
    """
    status = str(record.get("status") or "").strip().lower()
    task_id = str(record.get("task_id") or "").strip()
    claim_id = str(record.get("claim_id") or "").strip()
    agent_id = str(record.get("agent_id") or "").strip()
    if (status not in RUNNER_FAILURE_TERMINAL_STATUSES
            or metadata.get("execution_connection_id")
            or not (task_id and claim_id and agent_id)):
        return None

    claim = c.execute(
        "SELECT * FROM task_claims WHERE id=?", (claim_id,),
    ).fetchone()
    if (not claim or str(claim["status"] or "") != "active"
            or str(claim["task_id"] or "") != task_id
            or str(claim["agent_id"] or "") != agent_id):
        return None

    task = c.execute(
        "SELECT status, assignee, deliverable, agent_state FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    work_session_id = str(metadata.get("work_session_id") or "").strip()
    work_session = c.execute(
        "SELECT * FROM work_sessions WHERE work_session_id=?",
        (work_session_id,),
    ).fetchone() if work_session_id else None
    git_state = c.execute(
        "SELECT branch, head_sha, pr_number, pr_url FROM task_git_state WHERE task_id=?",
        (task_id,),
    ).fetchone()
    previous = {}
    if task:
        task_state = _json_obj(task["agent_state"] or "{}", {})
        previous = dict(task_state.get("switchboard/recovery_handoff") or {})
    else:
        task_state = {}
    attempt = max(1, int(previous.get("attempt") or 0) + 1)
    ws = dict(work_session) if work_session else {}
    gs = dict(git_state) if git_state else {}
    handoff = {
        "schema": "switchboard.runner_recovery_handoff.v1",
        "attempt": attempt,
        "project": metadata.get("project") or None,
        "task_id": task_id,
        "deliverable_id": (task["deliverable"] if task else None),
        "role": metadata.get("role") or metadata.get("lifecycle_role") or "implementation",
        "previous_runner_session_id": runner_session_id,
        "previous_claim_id": claim_id,
        "previous_work_session_id": work_session_id or None,
        "runner_status": status,
        "failure_reason": metadata.get("failure_reason") or metadata.get("last_error") or None,
        "repository": ws.get("repo") or None,
        "working_directory": ws.get("worktree_path") or ws.get("clone_path")
        or record.get("cwd") or None,
        "branch": gs.get("branch") or ws.get("branch") or None,
        "head_sha": gs.get("head_sha") or ws.get("head_sha")
        or metadata.get("source_sha") or None,
        "pr_number": gs.get("pr_number") or None,
        "pr_url": gs.get("pr_url") or None,
        "log_path": metadata.get("log_path") or None,
        "recorded_at": now,
    }
    task_state["switchboard/recovery_handoff"] = handoff

    reason = f"terminal_runner:{runner_session_id}:{status}"
    c.execute(
        "UPDATE task_claims SET status='abandoned', abandon_reason=? "
        "WHERE id=? AND status='active'",
        (reason, claim_id),
    )
    c.execute(
        "UPDATE resource_leases SET released_at=? WHERE resource_type='task' "
        "AND task_id=? AND agent_id=? AND released_at IS NULL",
        (now, task_id, agent_id),
    )
    if task:
        next_status = "Not Started" if str(task["status"] or "") == "In Progress" \
            else str(task["status"] or "")
        c.execute(
            "UPDATE tasks SET status=?, agent_state=?, "
            "assignee=CASE WHEN assignee=? THEN NULL ELSE assignee END, updated_at=? "
            "WHERE task_id=?",
            (next_status, json.dumps(task_state, sort_keys=True), agent_id, now, task_id),
        )
    c.execute(
        "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
        (task_id, actor, "task.claim.released_by_terminal_runner",
         json.dumps({"claim_id": claim_id, "runner_session_id": runner_session_id,
                     "runner_status": status, "recovery_handoff": handoff},
                    sort_keys=True), now),
    )
    return handoff


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
    direct_candidate = bool(
        metadata.get("direct_assignment") is True
        and metadata.get("assignment_schema") == "switchboard.direct_cli_assignment.v1"
        and status == "running"
        and not record.get("claim_id")
    )
    connect_candidate = bool(
        metadata.get("connect_assignment") is True
        and metadata.get("assignment_schema") == "switchboard.connect.assignment.v1"
        and status == "running"
        and not record.get("claim_id")
    )
    preclaim_candidate = bool(
        phase == "preclaim" and status == "starting" and not record.get("claim_id"))
    if (not (direct_candidate or connect_candidate or preclaim_candidate)
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
    direct = bool(
        policy.get("mode") == "direct_task"
        and policy.get("execution_mode") == "direct_personal_cli"
        and policy.get("require_runner_bind") is False
        and metadata.get("direct_assignment") is True
        and metadata.get("assignment_schema") == "switchboard.direct_cli_assignment.v1"
        and status == "running"
        and not record.get("claim_id")
    )
    if direct:
        assignment = policy.get("assignment") or {}
        selected_host = str(selector.get("host_id") or placement.get("selected_host_id") or "")
        expected = {
            "task_id": str(wake["task_id"] or selector.get("task_id") or ""),
            "agent_id": str(selector.get("agent_id") or ""),
            "runtime": str(selector.get("runtime") or ""),
            "host_id": selected_host,
        }
        actual = {
            "task_id": str(record.get("task_id") or ""),
            "agent_id": str(record.get("agent_id") or ""),
            "runtime": str(record.get("runtime") or ""),
            "host_id": host_id,
        }
        if (all(expected.values()) and actual == expected
                and str(assignment.get("task_id") or "") == expected["task_id"]
                and str(assignment.get("host_id") or "") == expected["host_id"]):
            metadata["native_host_execution"] = True
            metadata["direct_assignment"] = True
            record["metadata"] = metadata
            return True
        return False
    connect = bool(
        policy.get("mode") == "connect"
        and metadata.get("connect_assignment") is True
        and metadata.get("assignment_schema") == "switchboard.connect.assignment.v1"
        and status == "running"
        and not record.get("claim_id")
    )
    if connect:
        assignment = policy.get("assignment") or {}
        expected = {
            "task_id": str(wake["task_id"] or selector.get("task_id") or ""),
            "agent_id": str(selector.get("agent_id") or ""),
            "runtime": str(selector.get("runtime") or ""),
            "host_id": str(wake["claimed_by_host"] or ""),
            "assignment_id": str(assignment.get("assignment_id") or ""),
        }
        actual = {
            "task_id": str(record.get("task_id") or ""),
            "agent_id": str(record.get("agent_id") or ""),
            "runtime": str(record.get("runtime") or ""),
            "host_id": host_id,
            "assignment_id": str(metadata.get("assignment_id") or ""),
        }
        work_ref = str(assignment.get("work_ref") or "")
        if (all(expected.values()) and actual == expected
                and work_ref.startswith("task:")
                and work_ref.rsplit(":", 1)[-1] == expected["task_id"]):
            metadata["native_host_execution"] = True
            record["metadata"] = metadata
            return True
        return False
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


def _renew_exact_preclaim_runner_in(
        c: sqlite3.Connection, record: Dict[str, Any], principal_id: str,
        actor: str, now: float) -> Optional[Dict[str, Any]]:
    """Atomically refresh one preclaim row without permitting a bind downgrade.

    ``None`` means this is an ordinary runner upsert.  A renewal either refreshes
    the exact still-preclaim row, returns a concurrently claim-bound row unchanged,
    or fails closed.  It never feeds preclaim metadata into the generic upsert.
    """
    incoming_metadata = (
        record.get("metadata") if isinstance(record.get("metadata"), dict) else {})
    if incoming_metadata.get("preclaim_renewal") is not True:
        return None
    runner_session_id = str(
        record.get("runner_session_id") or record.get("id") or "").strip()
    existing_row = c.execute(
        "SELECT * FROM runner_sessions WHERE runner_session_id=?",
        (runner_session_id,),
    ).fetchone()
    if not existing_row:
        return {
            "error": "preclaim runner not found",
            "error_code": "preclaim_renewal_denied",
            "reason_codes": ["preclaim_runner_not_found"],
        }
    existing = dict(existing_row)
    existing_metadata = _json_obj(existing.get("metadata_json", "{}"), {})
    mismatches = sorted(
        field for field in ("host_id", "agent_id", "runtime", "task_id")
        if str(record.get(field) or "") != str(existing.get(field) or ""))
    if str(existing.get("principal_id") or "") != str(principal_id or ""):
        mismatches.append("principal_id")
    if str(incoming_metadata.get("wake_id") or "") != str(
            existing_metadata.get("wake_id") or ""):
        mismatches.append("wake_id")
    phase = str(existing_metadata.get("credential_admission_phase") or "")
    status = str(existing.get("status") or "").lower()
    claim_bound = (
        bool(str(existing.get("claim_id") or ""))
        and bool(str(existing_metadata.get("work_session_id") or ""))
        and phase == "claim_bound"
        and status in {"ready", "running"}
    )
    if not mismatches and claim_bound:
        return _runner_session_row(
            existing_row, now=now, include_claim=True, c=c)
    if (mismatches or existing.get("claim_id") or phase != "preclaim"
            or status != "starting"):
        reason_codes = [f"{field}_mismatch" for field in sorted(set(mismatches))]
        if existing.get("claim_id"):
            reason_codes.append("runner_already_claim_bound")
        if phase != "preclaim":
            reason_codes.append("runner_preclaim_metadata_mismatch")
        if status != "starting":
            reason_codes.append("runner_not_preclaim_starting")
        return {
            "error": "preclaim renewal does not match the live runner",
            "error_code": "preclaim_renewal_denied",
            "reason_codes": sorted(set(reason_codes)),
        }
    heartbeat_ttl_s = max(10, int(
        record.get("heartbeat_ttl_s") or record.get("ttl_s")
        or existing.get("heartbeat_ttl_s") or 60))
    c.execute(
        "UPDATE runner_sessions SET heartbeat_at=?, heartbeat_ttl_s=?, updated_at=? "
        "WHERE runner_session_id=? AND claim_id IS NULL AND status='starting' "
        "AND principal_id=?",
        (now, heartbeat_ttl_s, now, runner_session_id, principal_id),
    )
    row = c.execute(
        "SELECT * FROM runner_sessions WHERE runner_session_id=?",
        (runner_session_id,),
    ).fetchone()
    # SQLite serializes this transaction, but preserve the same no-downgrade
    # response if a future backend permits a concurrent bind between statements.
    if not row:
        return {
            "error": "preclaim runner disappeared during renewal",
            "error_code": "preclaim_renewal_denied",
        }
    c.execute(
        "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
        "VALUES (?,?,?,?,?)",
        (existing.get("task_id") or None, actor, "runner.preclaim_renewed",
         json.dumps({"runner_session_id": runner_session_id}, sort_keys=True), now),
    )
    return _runner_session_row(row, now=now, include_claim=True, c=c)


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
    renewal_requested = bool(
        isinstance(record.get("metadata"), dict)
        and record["metadata"].get("preclaim_renewal") is True)
    if renewal_requested:
        if not narrow_host:
            return {
                "error": "preclaim renewal requires a narrow Agent Host principal",
                "error_code": "preclaim_renewal_denied",
            }
        renewed = _renew_exact_preclaim_runner_in(
            c, record, principal_id, actor, now)
        if renewed is not None:
            return renewed
    previous_row = c.execute(
        "SELECT status, metadata_json FROM runner_sessions WHERE runner_session_id=?",
        (runner_session_id,),
    ).fetchone()
    previous_metadata = _json_obj(previous_row["metadata_json"], {}) \
        if previous_row else {}
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
    # A terminal failure is also the end of its exact Work Session attempt.  The
    # child normally expires the session after abandoning its claim, but hard kills
    # and host loss cannot execute that cleanup.  Closing it here makes the central
    # runner heartbeat the durable fallback and prevents failed attempts remaining
    # "active" forever. Successful runners stay active until checkpoint/claim
    # completion owns their stronger lifecycle transition.
    runner_status = str(record.get("status") or "").strip().lower()
    _sync_direct_session_token_lease_in(
        c, record, metadata, runner_session_id, now)
    work_session_id = str(metadata.get("work_session_id") or "").strip()
    lease_expired = metadata.get("terminalized_by") == "runner_lease_expiry"
    if (work_session_id
            and (lease_expired
                 or str(metadata.get("auth_lane") or "") == "codex_host_local")
            and runner_status in RUNNER_FAILURE_TERMINAL_STATUSES):
        work_session_status = "archived" if lease_expired else "expired"
        changed = c.execute(
            "UPDATE work_sessions SET status=?, completed_at=COALESCE(completed_at,?), "
            "updated_at=?, updated_by=? "
            "WHERE work_session_id=? AND status IN ('active','proposed','blocked')",
            (work_session_status, now, now, actor, work_session_id),
        )
        if changed.rowcount:
            c.execute(
                "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (record.get("task_id") or None, actor,
                 ("work_session.archived_by_runner_lease_expiry" if lease_expired
                  else "work_session.expired_by_terminal_runner"),
                 json.dumps({"runner_session_id": runner_session_id,
                             "work_session_id": work_session_id,
                             "runner_status": runner_status}, sort_keys=True), now),
            )
    _release_terminal_runner_ownership_in(
        c, record, metadata, runner_session_id, actor, now)
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
    if (metadata.get("terminalized_by") == "session_reaper"
            and previous_metadata.get("terminalized_by") != "session_reaper"):
        c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            (record.get("task_id") or None, actor, "runner.reaped",
             json.dumps({
                 "runner_session_id": runner_session_id,
                 "host_id": host_id or None,
                 "claim_id": record.get("claim_id") or None,
                 "reason": metadata.get("reaped_reason") or "unknown",
                 "last_output_at": metadata.get("last_output_at"),
                 "reaped_at": metadata.get("reaped_at") or now,
             }, sort_keys=True), now),
        )
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
    if action in {"open", "inject"} and not str(
            options.get("client_request_id") or "").strip():
        # Open and inject are repeatable operations. They must not inherit a
        # permanent external-effect result from an earlier open or same-text
        # chat attempt. Callers that need HTTP retry idempotency may provide
        # their own stable client_request_id; legacy callers receive a fresh id.
        options["client_request_id"] = "runnerop-" + uuid.uuid4().hex
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


def _server_relay_options(session: Dict[str, Any], *, user_id: str,
                          project: str,
                          host_attached: Optional[bool] = None) -> Dict[str, Any]:
    """Mint the two relay capabilities at the server trust boundary.

    A personal Agent Host has its own narrow bearer, not the server's PTY relay
    signing secret.  Tickets therefore cannot be minted on the Mac.  They are
    attached only to the authenticated claim response and are never persisted in
    runner_control_requests.options_json.

    ``host_attached=True`` (WATCH-4) treats a live relay-attached run as native
    transport for ticket binding: claim/work_session/exec/sha fields absent during
    admission are filled with a transport placeholder so
    a host_url is issued for a run proven live by its attached tunnel.
    """
    from switchboard.application import runner_pty_relay as relay
    from switchboard.domain import runner_pty as pty_domain

    metadata = dict(session.get("metadata") or {})
    public_base = relay.public_base_from_env()
    if not public_base or relay.is_loopback_url(public_base):
        return {
            "error": "relay_public_base_unavailable",
            "missing": ["relay_public_base"],
        }
    native_transport = bool(
        is_native_assignment_runner(session) or host_attached is True)
    # Keep the historical placeholder value stable; its prefix is serialized in
    # already-issued tickets but no longer participates in classification.
    placeholder_ref = f"direct/{session.get('runner_session_id') or 'session'}"
    bind = runner_bind_tuple(session)
    binding = {
        "tenant_id": str(metadata.get("tenant_id") or "tenant/default"),
        "user_id": str(user_id or "operator"),
        "project_id": str(project or DEFAULT_PROJECT),
        # Use the same canonical COORD-34 extraction as browser ticket minting.
        # Rebuilding this tuple ad hoc made the host ticket reject valid legacy
        # rows that the browser ticket accepted (BUG-125).
        "task_id": bind.get("task_id") or "",
        "claim_id": bind.get("claim_id") or (placeholder_ref if native_transport else ""),
        "work_session_id": str(
            bind.get("work_session_id") or (placeholder_ref if native_transport else "")),
        "runner_session_id": str(session.get("runner_session_id") or ""),
        "host_id": bind.get("host_id") or "",
        "wake_id": bind.get("wake_id") or "",
        "execution_connection_id": str(
            metadata.get("execution_connection_id")
            or (placeholder_ref if native_transport else "execconn/unspecified")),
        "source_sha": str(
            metadata.get("source_sha") or (placeholder_ref if native_transport else "unknown")),
        "permission_profile": "operator_watch",
    }
    missing = pty_domain.missing_ticket_bind_fields(binding)
    if missing:
        return {"error": RUNNER_BIND_ERROR, "missing": missing}
    ttl = 900
    host_ticket, host_payload = relay.mint_host_tunnel_ticket(binding, ttl_seconds=ttl)
    browser_ticket, browser_payload = relay.mint_capability_ticket(
        binding, ["watch", "input", "resize", "signal"], ttl_seconds=ttl)
    host_url = relay.public_host_relay_url(
        public_base, binding["runner_session_id"], host_ticket)
    host_url += "&host_id=" + urllib.parse.quote(binding["host_id"], safe="")
    return {
        "host_url": host_url,
        "browser_url": relay.public_relay_url(
            public_base, binding["runner_session_id"], browser_ticket),
        "expires_at": min(
            float(host_payload.get("exp") or 0),
            float(browser_payload.get("exp") or 0),
        ),
        "binding": binding,
    }


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
        claimed_request = _runner_control_row(row)
        if claimed_request.get("action") == "open":
            session_row = c.execute(
                "SELECT * FROM runner_sessions WHERE runner_session_id=?",
                (claimed_request.get("runner_session_id"),),
            ).fetchone()
            session = (_runner_session_row(
                session_row, now=now, include_claim=True, c=c)
                if session_row else {})
            server_relay = _server_relay_options(
                session,
                user_id=str(claimed_request.get("principal_id") or ""),
                project=project,
            )
            claimed_request["options"] = {
                **(claimed_request.get("options") or {}),
                "server_relay": server_relay,
            }
            if server_relay.get("error"):
                _record_server_relay_failure_in(
                    c, session, server_relay, actor=actor, now=now)
    return {"claimed": True, "request": claimed_request}


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
        if str(req.get("status") or "") in {
                "completed", "failed", "cancelled", "refused"}:
            return {
                **req,
                "completed": False,
                "reason": f"request is already {req.get('status')}",
            }
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
        if (req.get("action") == "kill" and final_status == "completed"
                and str(session_status or "").lower() not in RUNNER_WATCHABLE_STATUSES):
            # The launch wake records dispatch, not process lifetime.  Once its
            # process is deliberately killed it must no longer remain a reusable
            # successful generation: start_task derives the next idempotency key
            # from this failed predecessor.  Otherwise Connect returns the old
            # completed wake as "started" without asking a host to spawn anything.
            metadata = session.get("metadata") if isinstance(
                session.get("metadata"), dict) else {}
            wake_id = str(metadata.get("wake_id") or "").strip()
            if wake_id:
                wake_row = c.execute(
                    "SELECT status, runner_session_id, result_json FROM wake_intents "
                    "WHERE wake_id=?", (wake_id,),
                ).fetchone()
                if (wake_row and str(wake_row["status"] or "") == "completed"
                        and str(wake_row["runner_session_id"] or "")
                        == str(session.get("runner_session_id") or "")):
                    wake_result = _json_obj(wake_row["result_json"], {})
                    wake_result.update({
                        "started": False,
                        "failure_class": "runner_killed",
                        "reason": str(req.get("reason") or "runner killed"),
                        "failed_at": now,
                        "runner_session_id": session.get("runner_session_id"),
                    })
                    c.execute(
                        "UPDATE wake_intents SET status='failed', completed_at=?, "
                        "result_json=? WHERE wake_id=? AND status='completed'",
                        (now, json.dumps(wake_result, sort_keys=True), wake_id),
                    )
                    c.execute(
                        "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (session.get("task_id") or None, actor, "wake.failed",
                         json.dumps({"wake_id": wake_id,
                                     "runner_session_id": session.get("runner_session_id"),
                                     "failure_class": "runner_killed",
                                     "reason": wake_result["reason"]}, sort_keys=True), now),
                    )
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
