"""Durable operator-started deliverable/task Autopilot scopes (UI-27)."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional
import uuid

from constants import DEFAULT_PROJECT
from db.connection import _conn
from switchboard.storage.repositories import deliverables as deliverables_repository


AUTOPILOT_SCOPE_SCHEMA = "switchboard.autopilot_scope.v1"
AUTOPILOT_SCOPE_AUTHORITY_SCHEMA = "switchboard.autopilot_scope_authority.v1"
LIVE_SCOPE_STATUSES = frozenset({"active", "paused"})
SCOPE_TYPES = frozenset({"deliverable", "task"})
SUPPORTED_RUNTIMES = frozenset({
    "claude-code", "codex", "cursor", "langgraph", "openai-loop",
})


def _scope_result_with_transition(row: Any, transition: Dict[str, Any]) -> str:
    """Preserve the decision stream while appending one bounded scope handoff audit."""
    try:
        result = json.loads(row["last_result_json"] or "{}")
    except (TypeError, ValueError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    history = result.get("scope_transitions")
    if not isinstance(history, list):
        history = []
    result["scope_transition"] = transition
    result["scope_transitions"] = [*history, transition][-20:]
    return json.dumps(result, sort_keys=True)


def transition_deliverable_scopes_in(
        connection: Any, *, source_deliverable_id: str,
        replacement_deliverable_id: str = "", actor: str,
        reason: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    """Atomically transfer or explicitly stop every live scope for a deliverable.

    This accepts the caller's existing connection so the deliverable mutation and
    scope mutation are one SQLite transaction. A replacement retains each scope_id
    and its prior result/decision stream. Without a replacement, stopping also
    writes the audit event and operator-attention message before commit.
    """
    source = str(source_deliverable_id or "").strip()
    replacement = str(replacement_deliverable_id or "").strip()
    at = time.time() if now is None else float(now)
    if not source:
        return {"error": "source_deliverable_id required"}
    if replacement == source:
        return {"error": "replacement deliverable must differ from source",
                "deliverable_id": source}

    rows = connection.execute(
        "SELECT * FROM autopilot_scopes WHERE deliverable_id=? "
        "AND status IN ('active','paused') ORDER BY created_at, scope_id",
        (source,),
    ).fetchall()
    if replacement:
        target = connection.execute(
            "SELECT 1 FROM deliverables WHERE id=?", (replacement,)).fetchone()
        if not target:
            return {"error": "unknown replacement deliverable",
                    "replacement_deliverable_id": replacement}
        if not rows:
            return {
                "action": "no_live_scope",
                "deliverable_id": source,
                "replacement_deliverable_id": replacement,
                "scope_ids": [],
                "scope_count": 0,
                "operator_message_id": None,
                "reason": str(reason or "").strip() or
                          f"deliverable replaced by {replacement}",
            }
        conflicts = connection.execute(
            "SELECT scope_id,profile_id,scope_type,task_project,task_id "
            "FROM autopilot_scopes WHERE deliverable_id=? "
            "AND status IN ('active','paused') ORDER BY scope_id",
            (replacement,),
        ).fetchall()
        if conflicts:
            return {
                "error": "replacement deliverable already has a live autopilot scope",
                "replacement_deliverable_id": replacement,
                "conflicting_scope_ids": [row["scope_id"] for row in conflicts],
                "action": "stop the conflicting scope or omit the replacement to stop the source",
            }
        missing_task_links = []
        for row in rows:
            if row["scope_type"] != "task":
                continue
            linked = connection.execute(
                "SELECT 1 FROM deliverable_task_links WHERE deliverable_id=? "
                "AND project_id=? AND task_id=? LIMIT 1",
                (replacement, row["task_project"], row["task_id"]),
            ).fetchone()
            if not linked:
                missing_task_links.append({"scope_id": row["scope_id"],
                                           "task_project": row["task_project"],
                                           "task_id": row["task_id"]})
        if missing_task_links:
            return {
                "error": "replacement deliverable does not preserve task scope links",
                "replacement_deliverable_id": replacement,
                "missing_task_links": missing_task_links,
            }

    action = "transferred" if replacement else "stopped"
    default_reason = (
        f"deliverable replaced by {replacement}" if replacement
        else "deliverable archived without a replacement"
    )
    transition_reason = str(reason or "").strip() or default_reason
    scope_ids = []
    for row in rows:
        transition = {
            "action": action,
            "actor": actor,
            "at": at,
            "from_deliverable_id": source,
            "to_deliverable_id": replacement or None,
            "reason": transition_reason,
            "generation": int(row["generation"] or 1) + 1,
        }
        scope_ids.append(row["scope_id"])
        if replacement:
            connection.execute(
                "UPDATE autopilot_scopes SET deliverable_id=?, generation=generation+1, "
                "fence_epoch=fence_epoch+1,lease_id='',holder_agent_id='',expires_at=NULL,"
                "updated_at=?, last_result_json=? WHERE scope_id=?",
                (replacement, at, _scope_result_with_transition(row, transition),
                 row["scope_id"]),
            )
        else:
            connection.execute(
                "UPDATE autopilot_scopes SET status='stopped', generation=generation+1, "
                "fence_epoch=fence_epoch+1,lease_id='',holder_agent_id='',expires_at=NULL,"
                "updated_at=?, last_result_json=? WHERE scope_id=?",
                (at, _scope_result_with_transition(row, transition), row["scope_id"]),
            )

    payload = {
        "action": action,
        "deliverable_id": source,
        "replacement_deliverable_id": replacement or None,
        "scope_ids": scope_ids,
        "reason": transition_reason,
    }
    message_id = None
    if rows:
        if not replacement:
            message = (
                f"Autopilot stopped for deliverable {source}: {transition_reason}. "
                f"Stopped scope(s): {', '.join(scope_ids)}."
            )
            cursor = connection.execute(
                "INSERT INTO agent_messages(from_agent,to_agent,task_id,message,"
                "requires_ack,ack_deadline,sent_at) VALUES (?,?,?,?,?,?,?)",
                ("switchboard/autopilot", "switchboard/operator", None, message,
                 1, None, at),
            )
            message_id = cursor.lastrowid
            payload["operator_message_id"] = message_id
        connection.execute(
            "INSERT INTO activity(task_id,actor,kind,payload,created_at) VALUES (?,?,?,?,?)",
            (None, actor, f"autopilot.scope_{action}",
             json.dumps(payload, sort_keys=True), at),
        )
    return {
        **payload,
        "scope_count": len(scope_ids),
        "operator_message_id": message_id,
    }


def _row(row: Any) -> Dict[str, Any]:
    item = dict(row)
    try:
        result = json.loads(item.pop("last_result_json") or "{}")
    except (TypeError, ValueError):
        result = {}
    item.update({
        "schema": AUTOPILOT_SCOPE_SCHEMA,
        "last_result": result if isinstance(result, dict) else {},
    })
    return item


def list_autopilot_scopes(*, project: str = DEFAULT_PROJECT,
                          profile_id: str = "autopilot-default",
                          deliverable_id: str = "", status: str = "",
                          limit: int = 500) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM autopilot_scopes WHERE profile_id=?"
    params: List[Any] = [profile_id]
    if deliverable_id:
        sql += " AND deliverable_id=?"
        params.append(deliverable_id)
    if status:
        values = [part.strip() for part in status.split(",") if part.strip()]
        if values:
            sql += " AND status IN (" + ",".join("?" for _ in values) + ")"
            params.extend(values)
    sql += " ORDER BY updated_at, scope_id LIMIT ?"
    params.append(max(1, min(int(limit or 500), 2000)))
    with _conn(project) as c:
        return [_row(row) for row in c.execute(sql, params).fetchall()]


def get_autopilot_scope(scope_id: str, *, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        row = c.execute("SELECT * FROM autopilot_scopes WHERE scope_id=?", (scope_id,)).fetchone()
        return _row(row) if row else None


def _validate_target(project: str, deliverable_id: str, scope_type: str,
                     task_project: str, task_id: str) -> Optional[Dict[str, Any]]:
    deliverable = deliverables_repository.get_deliverable(
        deliverable_id, project=project, include_task_snapshots=False)
    if not deliverable:
        return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
    if scope_type == "deliverable":
        return None
    milestone_statuses = {
        str(row.get("id") or ""): str(row.get("status") or "").strip().lower()
        for row in (deliverable.get("milestones") or [])
    }
    for link in deliverable.get("task_links") or []:
        if (str(link.get("task_id") or "").upper() == task_id
                and str(link.get("project_id") or project) == task_project):
            reason = deliverables_repository._link_automatic_dispatch_reason(
                link, milestone_statuses.get(str(link.get("milestone_id") or ""), ""))
            if reason != "automatic_flow":
                return {
                    "error": "task link is structurally ineligible for dispatch",
                    "deliverable_id": deliverable_id,
                    "task_project": task_project,
                    "task_id": task_id,
                    "blocker": {"reason": reason, "role": link.get("role"),
                                "milestone_id": link.get("milestone_id")},
                }
            return None
    return {"error": "task is not linked to deliverable", "deliverable_id": deliverable_id,
            "task_project": task_project, "task_id": task_id}


def validate_autopilot_target(*, project: str = DEFAULT_PROJECT,
                              deliverable_id: str, scope_type: str = "deliverable",
                              task_project: str = "", task_id: str = "",
                              runtime: str = "codex") -> Optional[Dict[str, Any]]:
    """Validate a scope target without creating it.

    Task Start uses this public read boundary before dispatch so a structurally
    invalid link cannot launch work or leave behind an active scope.
    """
    runtime = str(runtime or "codex").strip().lower()
    if runtime not in SUPPORTED_RUNTIMES:
        return {"error": "unsupported autopilot runtime", "runtime": runtime,
                "supported_runtimes": sorted(SUPPORTED_RUNTIMES)}
    kind = str(scope_type or "deliverable").strip().lower()
    normalized_project = str(task_project or project).strip() if kind == "task" else ""
    normalized_task = str(task_id or "").strip().upper() if kind == "task" else ""
    return _validate_target(project, str(deliverable_id or "").strip(), kind,
                            normalized_project, normalized_task)


def start_autopilot_scope(*, project: str = DEFAULT_PROJECT,
                          profile_id: str = "autopilot-default",
                          deliverable_id: str, scope_type: str = "deliverable",
                          task_project: str = "", task_id: str = "",
                          runtime: str = "codex", actor: str = "user") -> Dict[str, Any]:
    kind = str(scope_type or "deliverable").strip().lower()
    if kind not in SCOPE_TYPES:
        return {"error": "scope_type must be deliverable or task"}
    deliverable_id = str(deliverable_id or "").strip()
    task_project = str(task_project or project).strip() if kind == "task" else ""
    task_id = str(task_id or "").strip().upper() if kind == "task" else ""
    if not deliverable_id or (kind == "task" and not task_id):
        return {"error": "deliverable_id and task_id are required for this scope"}
    runtime = str(runtime or "codex").strip().lower()
    if runtime not in SUPPORTED_RUNTIMES:
        return {"error": "unsupported autopilot runtime", "runtime": runtime,
                "supported_runtimes": sorted(SUPPORTED_RUNTIMES)}
    invalid = validate_autopilot_target(
        project=project, deliverable_id=deliverable_id, scope_type=kind,
        task_project=task_project, task_id=task_id, runtime=runtime)
    if invalid:
        return invalid
    now = time.time()
    with _conn(project) as c:
        # A deliverable scope already covers every eligible linked task. Clicking
        # Start on one of those tasks is an idempotent readback, not a second run.
        if kind == "task":
            covering = c.execute(
                "SELECT * FROM autopilot_scopes WHERE profile_id=? AND scope_type='deliverable' "
                "AND deliverable_id=? AND status IN ('active','paused') ORDER BY updated_at DESC LIMIT 1",
                (profile_id, deliverable_id),
            ).fetchone()
            if covering:
                item = _row(covering)
                item.update({"already_started": True, "covered": True,
                             "covered_task_id": task_id})
                return item
        existing = c.execute(
            "SELECT * FROM autopilot_scopes WHERE profile_id=? AND scope_type=? "
            "AND deliverable_id=? AND task_project=? AND task_id=? "
            "AND status IN ('active','paused') ORDER BY updated_at DESC LIMIT 1",
            (profile_id, kind, deliverable_id, task_project, task_id),
        ).fetchone()
        if existing:
            if existing["status"] == "paused":
                c.execute("UPDATE autopilot_scopes SET status='active',generation=generation+1,"
                          "fence_epoch=fence_epoch+1,lease_id='',holder_agent_id='',"
                          "expires_at=NULL,updated_at=? WHERE scope_id=?",
                          (now, existing["scope_id"]))
            row = c.execute("SELECT * FROM autopilot_scopes WHERE scope_id=?",
                            (existing["scope_id"],)).fetchone()
            item = _row(row)
            item["already_started"] = True
            return item
        scope_id = "autopilot-" + uuid.uuid4().hex[:16]
        c.execute(
            "INSERT INTO autopilot_scopes(scope_id,profile_id,scope_type,deliverable_id,"
            "task_project,task_id,runtime,status,requested_by,generation,created_at,updated_at,"
            "last_result_json,started_by,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scope_id, profile_id, kind, deliverable_id, task_project, task_id,
             runtime, "active", actor, 1, now, now, "{}", actor, now),
        )
        if kind == "deliverable":
            # Preserve the audit rows but stop narrower scopes now covered by the
            # deliverable run. This is the primary overlap-dedupe boundary.
            c.execute(
                "UPDATE autopilot_scopes SET status='superseded', updated_at=? "
                "WHERE profile_id=? AND scope_type='task' AND deliverable_id=? "
                "AND status IN ('active','paused')",
                (now, profile_id, deliverable_id),
            )
        c.execute(
            "INSERT INTO activity(task_id,actor,kind,payload,created_at) VALUES (?,?,?,?,?)",
            (task_id or None, actor, "autopilot.scope_started",
             json.dumps({"scope_id": scope_id, "scope_type": kind,
                         "deliverable_id": deliverable_id, "task_project": task_project,
                         "task_id": task_id, "runtime": runtime}, sort_keys=True), now),
        )
        row = c.execute("SELECT * FROM autopilot_scopes WHERE scope_id=?", (scope_id,)).fetchone()
        return _row(row)


def acquire_autopilot_scope_lease(
        scope_id: str, *, holder_agent_id: str,
        project: str = DEFAULT_PROJECT, ttl_seconds: int = 120,
        now: Optional[float] = None) -> Dict[str, Any]:
    """Acquire or renew the sole fenced coordinator authority for one scope."""
    at = time.time() if now is None else float(now)
    holder = str(holder_agent_id or "").strip()
    if not holder:
        return {"error": "holder_agent_id required", "scope_id": scope_id}
    ttl = max(30, min(int(ttl_seconds or 120), 3600))
    with _conn(project) as c:
        presence = c.execute(
            "SELECT heartbeat_at,ttl_s FROM agent_presence WHERE agent_id=?",
            (holder,),
        ).fetchone()
        if (not presence
                or float(presence["heartbeat_at"] or 0)
                + int(presence["ttl_s"] or 0) <= at):
            return {
                "error": "scope_holder_not_registered",
                "scope_id": scope_id,
                "holder_agent_id": holder,
            }
        row = c.execute(
            "SELECT * FROM autopilot_scopes WHERE scope_id=?", (scope_id,)).fetchone()
        if not row:
            return {"error": "autopilot scope not found", "scope_id": scope_id}
        if row["status"] != "active":
            return {"error": "autopilot scope is not active", "scope_id": scope_id,
                    "status": row["status"]}
        current_holder = str(row["holder_agent_id"] or "")
        current_expiry = float(row["expires_at"] or 0)
        if current_holder and current_holder != holder and current_expiry > at:
            return {
                "error": "scope_lease_conflict",
                "scope_id": scope_id,
                "holder_agent_id": current_holder,
                "expires_at": current_expiry,
            }
        takeover = bool(current_holder and current_holder != holder)
        lease_id = (
            str(row["lease_id"] or "")
            if current_holder == holder and current_expiry > at
            else "scopelease-" + uuid.uuid4().hex[:16]
        )
        fence_epoch = int(row["fence_epoch"] or 0) + (
            1 if takeover or not str(row["lease_id"] or "") else 0)
        generation = int(row["generation"] or 1) + (1 if takeover else 0)
        expires_at = at + ttl
        c.execute(
            "UPDATE autopilot_scopes SET lease_id=?,holder_agent_id=?,"
            "fence_epoch=?,generation=?,heartbeat_at=?,expires_at=?,updated_at=? "
            "WHERE scope_id=?",
            (lease_id, holder, fence_epoch, generation, at, expires_at, at, scope_id),
        )
        return {
            "schema": AUTOPILOT_SCOPE_AUTHORITY_SCHEMA,
            "scope_id": scope_id,
            "holder_agent_id": holder,
            "lease_id": lease_id,
            "generation": generation,
            "fence_epoch": fence_epoch,
            "expires_at": expires_at,
            "deliverable_id": row["deliverable_id"],
            "task_project": row["task_project"],
            "task_id": row["task_id"],
            "renewed": current_holder == holder and current_expiry > at,
            "takeover": takeover,
        }


def validate_autopilot_scope_authority(
        authority: Dict[str, Any], *, project: str = DEFAULT_PROJECT,
        deliverable_id: str = "", task_project: str = "", task_id: str = "",
        now: Optional[float] = None) -> Dict[str, Any]:
    """Fail closed unless the exact live lease/fence still covers the target."""
    supplied = dict(authority or {})
    scope_id = str(supplied.get("scope_id") or "")
    at = time.time() if now is None else float(now)
    if supplied.get("schema") != AUTOPILOT_SCOPE_AUTHORITY_SCHEMA or not scope_id:
        return {"allowed": False, "error": "scope_authority_required"}
    with _conn(project) as c:
        row = c.execute(
            "SELECT * FROM autopilot_scopes WHERE scope_id=?", (scope_id,)).fetchone()
    if not row:
        return {"allowed": False, "error": "autopilot_scope_not_found",
                "scope_id": scope_id}
    checks = {
        "status": row["status"] == "active",
        "lease_id": str(row["lease_id"] or "") == str(supplied.get("lease_id") or ""),
        "holder_agent_id": str(row["holder_agent_id"] or "")
        == str(supplied.get("holder_agent_id") or ""),
        "generation": int(row["generation"] or 0)
        == int(supplied.get("generation") or -1),
        "fence_epoch": int(row["fence_epoch"] or 0)
        == int(supplied.get("fence_epoch") or -1),
        "unexpired": float(row["expires_at"] or 0) > at,
        "deliverable_id": (
            not deliverable_id or str(row["deliverable_id"] or "") == deliverable_id),
        "task_project": (
            row["scope_type"] == "deliverable" or not task_project
            or str(row["task_project"] or "") == task_project),
        "task_id": (
            row["scope_type"] == "deliverable" or not task_id
            or str(row["task_id"] or "").upper() == str(task_id).upper()),
    }
    failed = sorted(key for key, passed in checks.items() if not passed)
    if failed:
        return {"allowed": False, "error": "scope_authority_denied",
                "scope_id": scope_id, "reason_codes": failed}
    return {"allowed": True, "scope": _row(row), "authority": supplied}


def control_autopilot_scope(*, project: str = DEFAULT_PROJECT,
                            profile_id: str = "autopilot-default",
                            deliverable_id: str, scope_type: str = "deliverable",
                            task_project: str = "", task_id: str = "",
                            action: str, actor: str = "user") -> Dict[str, Any]:
    action = str(action or "").strip().lower()
    target = {"pause": "paused", "resume": "active", "stop": "stopped"}.get(action)
    if not target:
        return {"error": "action must be pause, resume, or stop"}
    kind = str(scope_type or "deliverable").strip().lower()
    task_project = str(task_project or project).strip() if kind == "task" else ""
    task_id = str(task_id or "").strip().upper() if kind == "task" else ""
    now = time.time()
    with _conn(project) as c:
        row = c.execute(
            "SELECT * FROM autopilot_scopes WHERE profile_id=? AND scope_type=? "
            "AND deliverable_id=? AND task_project=? AND task_id=? "
            "AND status IN ('active','paused') ORDER BY updated_at DESC LIMIT 1",
            (profile_id, kind, deliverable_id, task_project, task_id),
        ).fetchone()
        if not row:
            return {"error": "live autopilot scope not found"}
        c.execute(
            "UPDATE autopilot_scopes SET status=?,generation=generation+1,"
            "fence_epoch=fence_epoch+1,lease_id='',holder_agent_id='',expires_at=NULL,"
            "updated_at=? WHERE scope_id=?",
            (target, now, row["scope_id"]))
        c.execute(
            "INSERT INTO activity(task_id,actor,kind,payload,created_at) VALUES (?,?,?,?,?)",
            (task_id or None, actor, f"autopilot.scope_{action}",
             json.dumps({"scope_id": row["scope_id"], "deliverable_id": deliverable_id,
                         "task_id": task_id}, sort_keys=True), now),
        )
        current = c.execute("SELECT * FROM autopilot_scopes WHERE scope_id=?",
                            (row["scope_id"],)).fetchone()
        return _row(current)


def update_autopilot_scope(scope_id: str, *, project: str = DEFAULT_PROJECT,
                           status: str = "", last_result: Optional[Dict[str, Any]] = None,
                           ticked_at: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if ticked_at is None else float(ticked_at)
    with _conn(project) as c:
        row = c.execute("SELECT * FROM autopilot_scopes WHERE scope_id=?", (scope_id,)).fetchone()
        if not row:
            return {"error": "autopilot scope not found", "scope_id": scope_id}
        next_status = status or row["status"]
        c.execute(
            "UPDATE autopilot_scopes SET status=?, updated_at=?, last_tick_at=?, "
            "last_result_json=? WHERE scope_id=?",
            (next_status, now, now, json.dumps(last_result or {}, sort_keys=True), scope_id),
        )
        current = c.execute("SELECT * FROM autopilot_scopes WHERE scope_id=?", (scope_id,)).fetchone()
        return _row(current)


class StoreAutopilotScopeRepository:
    def list_autopilot_scopes(self, **kwargs):
        return list_autopilot_scopes(**kwargs)

    def get_autopilot_scope(self, *args, **kwargs):
        return get_autopilot_scope(*args, **kwargs)

    def start_autopilot_scope(self, **kwargs):
        return start_autopilot_scope(**kwargs)

    def control_autopilot_scope(self, **kwargs):
        return control_autopilot_scope(**kwargs)

    def update_autopilot_scope(self, *args, **kwargs):
        return update_autopilot_scope(*args, **kwargs)

    def acquire_autopilot_scope_lease(self, *args, **kwargs):
        return acquire_autopilot_scope_lease(*args, **kwargs)

    def validate_autopilot_scope_authority(self, *args, **kwargs):
        return validate_autopilot_scope_authority(*args, **kwargs)


def default_autopilot_scope_repository() -> StoreAutopilotScopeRepository:
    return StoreAutopilotScopeRepository()


__all__ = [
    "AUTOPILOT_SCOPE_SCHEMA", "AUTOPILOT_SCOPE_AUTHORITY_SCHEMA",
    "LIVE_SCOPE_STATUSES", "SCOPE_TYPES", "SUPPORTED_RUNTIMES",
    "StoreAutopilotScopeRepository", "default_autopilot_scope_repository",
    "list_autopilot_scopes", "get_autopilot_scope", "validate_autopilot_target",
    "start_autopilot_scope",
    "control_autopilot_scope", "update_autopilot_scope",
    "acquire_autopilot_scope_lease", "validate_autopilot_scope_authority",
    "transition_deliverable_scopes_in",
]
