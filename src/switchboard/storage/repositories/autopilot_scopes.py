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
LIVE_SCOPE_STATUSES = frozenset({"active", "paused"})
SCOPE_TYPES = frozenset({"deliverable", "task"})
SUPPORTED_RUNTIMES = frozenset({
    "claude-code", "codex", "cursor", "langgraph", "openai-loop",
})


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
    for link in deliverable.get("task_links") or []:
        if (str(link.get("task_id") or "").upper() == task_id
                and str(link.get("project_id") or project) == task_project):
            return None
    return {"error": "task is not linked to deliverable", "deliverable_id": deliverable_id,
            "task_project": task_project, "task_id": task_id}


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
    invalid = _validate_target(project, deliverable_id, kind, task_project, task_id)
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
                c.execute("UPDATE autopilot_scopes SET status='active', updated_at=? WHERE scope_id=?",
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
            "last_result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scope_id, profile_id, kind, deliverable_id, task_project, task_id,
             runtime, "active", actor, 1, now, now, "{}"),
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
        c.execute("UPDATE autopilot_scopes SET status=?, updated_at=? WHERE scope_id=?",
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


def default_autopilot_scope_repository() -> StoreAutopilotScopeRepository:
    return StoreAutopilotScopeRepository()


__all__ = [
    "AUTOPILOT_SCOPE_SCHEMA", "LIVE_SCOPE_STATUSES", "SCOPE_TYPES", "SUPPORTED_RUNTIMES",
    "StoreAutopilotScopeRepository", "default_autopilot_scope_repository",
    "list_autopilot_scopes", "get_autopilot_scope", "start_autopilot_scope",
    "control_autopilot_scope", "update_autopilot_scope",
]
