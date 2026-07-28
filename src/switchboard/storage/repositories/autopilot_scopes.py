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
                          task_project: str = "", task_id: str = "",
                          limit: int = 500) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM autopilot_scopes WHERE profile_id=?"
    params: List[Any] = [profile_id]
    if deliverable_id:
        sql += " AND deliverable_id=?"
        params.append(deliverable_id)
    if task_project:
        sql += " AND task_project=?"
        params.append(str(task_project).strip())
    if task_id:
        sql += " AND task_id=?"
        params.append(str(task_id).strip().upper())
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


def scope_liveness(scope: Dict[str, Any], *, now: Optional[float] = None) -> str:
    """One honest verdict for a scope row: armed | live | stale | paused.

    ``status`` alone lies: on 2026-07-26 a deploy restart killed a scope's
    holder and the row kept ``status="active"`` — every read surface reported a
    dead autopilot as running for 45+ minutes. Liveness is derived from the
    holder lease, which is the thing that actually ticks:

      * ``paused``  — operator paused it;
      * ``armed``   — active with no holder yet (started, awaiting pickup by a
        coordinator; the wake substrate queues until a capable host is online);
      * ``live``    — a holder's lease is current;
      * ``stale``   — a holder existed and its lease expired without release:
        the restart-killed shape. The scope is NOT running, whatever status
        says.
    """
    at = time.time() if now is None else float(now)
    status = str(scope.get("status") or "").strip().lower()
    if status == "paused":
        return "paused"
    holder = str(scope.get("holder_agent_id") or "").strip()
    expires = scope.get("expires_at")
    if not holder and expires in (None, "", 0):
        return "armed"
    if expires not in (None, "", 0) and float(expires) > at:
        return "live"
    return "stale"


#: Preference order when several scopes could answer for one task: a running
#: scope beats an armed one beats paused beats dead-but-visible.
_LIVENESS_RANK = {"live": 0, "armed": 1, "paused": 2, "stale": 3}


def autopilot_coverage_for_tasks(
        task_ids: Any, *, project: str = DEFAULT_PROJECT,
        task_project: str = "",
        profile_id: str = "autopilot-default") -> Dict[str, Dict[str, Any]]:
    """Which live scope covers each task, with an honest liveness verdict.

    UI-66, for the Fleet dock: the dock is the exception surface, but nothing
    could answer "is autopilot driving this?" per task. Coverage comes from a
    standalone task scope, or from a deliverable scope over any deliverable the
    task is linked to. A task linked to no deliverable (the COORD-76 shape) is
    only ever covered by a task scope — which is exactly what the dock needs to
    know to offer the right arm action. Batched: one call for every row on the
    dock, not one request per task per poll.
    """
    from switchboard.storage.repositories import deliverables as deliverables_repo

    now = time.time()
    wanted = [str(item or "").strip().upper() for item in (task_ids or [])]
    wanted = [item for item in dict.fromkeys(wanted) if item]
    out: Dict[str, Dict[str, Any]] = {}
    if not wanted:
        return out
    with _conn(project) as c:
        for task_id in wanted:
            candidates: List[Dict[str, Any]] = []
            rows = c.execute(
                "SELECT * FROM autopilot_scopes WHERE profile_id=? AND "
                "scope_type='task' AND task_id=? AND status IN ('active','paused') "
                "ORDER BY updated_at DESC",
                (profile_id, task_id),
            ).fetchall()
            for row in rows:
                scope = _row(row)
                candidates.append({"coverage": "task", "scope": scope})
            try:
                links = deliverables_repo.list_task_deliverable_links(
                    task_id, project=task_project or project)
            except Exception:
                links = []
            for link in links or []:
                deliverable_id = str(link.get("deliverable_id") or "").strip()
                if not deliverable_id:
                    continue
                for row in c.execute(
                        "SELECT * FROM autopilot_scopes WHERE profile_id=? AND "
                        "scope_type='deliverable' AND deliverable_id=? AND "
                        "status IN ('active','paused') ORDER BY updated_at DESC",
                        (profile_id, deliverable_id)).fetchall():
                    scope = _row(row)
                    candidates.append(
                        {"coverage": "deliverable", "scope": scope})
            if not candidates:
                out[task_id] = {
                    "task_id": task_id, "covered": False, "coverage": "none",
                    "liveness": "none", "scope_id": None,
                    "deliverable_id": None, "scope_status": None,
                }
                continue
            for candidate in candidates:
                candidate["liveness"] = scope_liveness(
                    candidate["scope"], now=now)
            best = min(candidates, key=lambda item: (
                _LIVENESS_RANK.get(item["liveness"], 9),
                0 if item["coverage"] == "deliverable" else 1,
            ))
            scope = best["scope"]
            out[task_id] = {
                "task_id": task_id,
                "covered": True,
                "coverage": best["coverage"],
                "liveness": best["liveness"],
                "scope_id": scope.get("scope_id"),
                "deliverable_id": scope.get("deliverable_id") or None,
                "scope_status": scope.get("status"),
                "heartbeat_at": scope.get("heartbeat_at"),
                "expires_at": scope.get("expires_at"),
                "runtime": scope.get("runtime"),
            }
    return out


def _validate_target(project: str, deliverable_id: str, scope_type: str,
                     task_project: str, task_id: str) -> Optional[Dict[str, Any]]:
    if scope_type == "task" and not deliverable_id:
        # A standalone task scope: "Start this one task and carry it to Done."
        # Deliverables group outcomes; requiring one to drive a single task would
        # force bookkeeping onto every ad-hoc start (ADR-0006 subtraction). The
        # task itself is the target, so validate the task and nothing else.
        from switchboard.storage.repositories import tasks as tasks_repository
        task = tasks_repository.get_task(task_id, project=task_project or project)
        if not task:
            return {"error": "unknown task", "task_project": task_project,
                    "task_id": task_id}
        return None
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
                              deliverable_id: str = "", scope_type: str = "deliverable",
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
                          deliverable_id: str = "", scope_type: str = "deliverable",
                          task_project: str = "", task_id: str = "",
                          runtime: str = "codex", actor: str = "user") -> Dict[str, Any]:
    kind = str(scope_type or "deliverable").strip().lower()
    if kind not in SCOPE_TYPES:
        return {"error": "scope_type must be deliverable or task"}
    deliverable_id = str(deliverable_id or "").strip()
    task_project = str(task_project or project).strip() if kind == "task" else ""
    task_id = str(task_id or "").strip().upper() if kind == "task" else ""
    if kind == "task":
        if not task_id:
            return {"error": "task_id is required for a task scope"}
    elif not deliverable_id:
        return {"error": "deliverable_id is required for a deliverable scope"}
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
        if kind == "task" and deliverable_id:
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


def start_task_scope_in(
        connection: Any, *, project: str, task_id: str, runtime: str,
        actor: str, execution_lease: Dict[str, Any],
        wake_deadline: float, now: float,
        profile_id: str = "autopilot-default") -> Dict[str, Any]:
    """Establish operator Start authority inside the capacity transaction.

    ``start_task`` used to request capacity and only then open a second
    transaction to arm the task scope.  A crash between those commits produced
    a runner with no coordination authority.  This helper deliberately accepts
    the caller's connection so scope provenance, execution generation, and the
    reserved wake share one commit.
    """
    canonical_task = str(task_id or "").strip().upper()
    if not canonical_task:
        raise ValueError("task_id is required for a task scope")
    runtime = str(runtime or "codex").strip().lower()
    if runtime not in SUPPORTED_RUNTIMES:
        raise ValueError(f"unsupported autopilot runtime: {runtime}")
    existing = connection.execute(
        "SELECT * FROM autopilot_scopes WHERE profile_id=? AND scope_type='task' "
        "AND deliverable_id='' AND task_project=? AND task_id=? "
        "AND status IN ('active','paused') ORDER BY updated_at DESC LIMIT 1",
        (profile_id, project, canonical_task),
    ).fetchone()
    provenance = {
        "schema": "switchboard.scoped_start_provenance.v1",
        "actor": actor,
        "started_at": now,
        "execution_id": execution_lease["id"],
        "execution_generation": execution_lease["execution_generation"],
        "execution_fence_epoch": execution_lease["fence_epoch"],
        "wake_id": execution_lease["wake_id"],
        "wake_deadline": wake_deadline,
    }
    if existing:
        scope_id = existing["scope_id"]
        result = json.loads(existing["last_result_json"] or "{}")
        result = result if isinstance(result, dict) else {}
        result["latest_start"] = provenance
        connection.execute(
            "UPDATE autopilot_scopes SET status='active',runtime=?,updated_at=?,"
            "last_result_json=? WHERE scope_id=?",
            (runtime, now, json.dumps(result, sort_keys=True), scope_id),
        )
        row = connection.execute(
            "SELECT * FROM autopilot_scopes WHERE scope_id=?", (scope_id,),
        ).fetchone()
        item = _row(row)
        item["already_started"] = True
        return item
    scope_id = "autopilot-" + uuid.uuid4().hex[:16]
    connection.execute(
        "INSERT INTO autopilot_scopes(scope_id,profile_id,scope_type,deliverable_id,"
        "task_project,task_id,runtime,status,requested_by,generation,fence_epoch,"
        "created_at,updated_at,last_result_json,started_by,started_at) "
        "VALUES (?,?,?,'',?,?,?,?,?,1,1,?,?,?,?,?)",
        (scope_id, profile_id, "task", project, canonical_task, runtime,
         "active", actor, now, now,
         json.dumps({"latest_start": provenance}, sort_keys=True), actor, now),
    )
    connection.execute(
        "INSERT INTO activity(task_id,actor,kind,payload,created_at) "
        "VALUES (?,?,?,?,?)",
        (canonical_task, actor, "autopilot.scope_started",
         json.dumps({
             "scope_id": scope_id, "scope_type": "task",
             "task_project": project, "task_id": canonical_task,
             "runtime": runtime, "start_provenance": provenance,
         }, sort_keys=True), now),
    )
    return _row(connection.execute(
        "SELECT * FROM autopilot_scopes WHERE scope_id=?", (scope_id,),
    ).fetchone())


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
        target_linked = True
        if row and row["scope_type"] == "deliverable" and task_id:
            target_linked = bool(c.execute(
                "SELECT 1 FROM deliverable_task_links "
                "WHERE deliverable_id=? AND project_id=? AND task_id=? LIMIT 1",
                (row["deliverable_id"], task_project or project,
                 str(task_id).upper()),
            ).fetchone())
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
        "target_membership": target_linked,
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
        closing = row["status"] == "active" and next_status != "active"
        if closing:
            c.execute(
                "UPDATE autopilot_scopes SET status=?,generation=generation+1,"
                "fence_epoch=fence_epoch+1,lease_id='',holder_agent_id='',"
                "heartbeat_at=NULL,expires_at=NULL,updated_at=?,last_tick_at=?,"
                "last_result_json=? WHERE scope_id=?",
                (next_status, now, now, json.dumps(last_result or {}, sort_keys=True),
                 scope_id),
            )
        else:
            c.execute(
                "UPDATE autopilot_scopes SET status=?, updated_at=?, last_tick_at=?, "
                "last_result_json=? WHERE scope_id=?",
                (next_status, now, now, json.dumps(last_result or {}, sort_keys=True),
                 scope_id),
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
