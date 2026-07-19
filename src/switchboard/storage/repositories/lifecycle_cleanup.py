"""Lifecycle cleanup candidates/apply repository (ARCH-MS-62).

Owns cleanup_candidates / apply_cleanup and helper builders previously living in
``repositories/shell.py``. Multi-table lifecycle SQL stays here — do not fold into
claims/deliverables. ``store.py`` / ``shell.py`` re-export these symbols; root
``lifecycle_cleanup_store.py`` is a compatibility shim.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from constants import *  # noqa: F401,F403
from db.connection import _conn
from db.core import *  # noqa: F401,F403
from switchboard.domain.coordination.terminal import (
    TERMINAL_RUNNER_STATUSES,
    TERMINAL_WAKE_STATUSES,
)
from switchboard.storage.repositories.access import has_project
from switchboard.storage.repositories.coordination import (
    _host_row,
    _monitor_row,
    _presence_row,
    _wake_row,
)
from switchboard.storage.repositories.runner import (
    _clear_active_runner_pointer_in,
    _runner_session_row,
)
from switchboard.storage.repositories.tasks import (
    _active_task_state_in,
    _delete_task_related_in,
    _insert_archive_in,
    _is_cleanup_proof_task,
    _task_row,
    _task_snapshot_in,
)


def _cleanup_age_seconds(now: float, timestamp: Optional[float]) -> Optional[float]:
    if timestamp in (None, ""):
        return None
    try:
        return max(0.0, now - float(timestamp))
    except (TypeError, ValueError):
        return None


def _cleanup_candidate(kind: str, target_id: str, action: str, reason: str,
                       now: float, task_id: str = "",
                       timestamp: Optional[float] = None,
                       severity: str = "low",
                       snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "id": f"{kind}:{target_id}",
        "kind": kind,
        "target_id": target_id,
        "task_id": task_id or None,
        "action": action,
        "reason": reason,
        "severity": severity,
        "age_seconds": _cleanup_age_seconds(now, timestamp),
        "safe_to_apply": True,
        "snapshot": snapshot or {},
    }


def _cleanup_summary(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_kind: Dict[str, int] = {}
    by_action: Dict[str, int] = {}
    for item in candidates:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
        by_action[item["action"]] = by_action.get(item["action"], 0) + 1
    return {"total": len(candidates), "by_kind": by_kind, "by_action": by_action}


def cleanup_candidates(project: str = DEFAULT_PROJECT,
                       now: Optional[float] = None,
                       proof_task_age_days: float = 14,
                       include_kinds: Optional[List[str]] = None) -> Dict[str, Any]:
    """Return read-only lifecycle cleanup candidates.

    Candidates are intentionally conservative: only expired/stale rows or old terminal
    proof/sentinel tasks with no active claims/leases are returned. Applying a cleanup
    writes `cleanup.*` activity before changing live rows.
    """
    if not has_project(project):
        return {"error": f"unknown project: {project}", "project": project}
    now = time.time() if now is None else float(now)
    wanted = {k.strip() for k in (include_kinds or []) if k.strip()}
    min_proof_age = max(0.0, float(proof_task_age_days or 0)) * 86400.0
    out: List[Dict[str, Any]] = []

    def accept(kind: str) -> bool:
        return not wanted or kind in wanted

    with _conn(project) as c:
        task_ids = {r["task_id"] for r in c.execute("SELECT task_id FROM tasks").fetchall()}

        if accept("agent_host"):
            for row in c.execute("SELECT * FROM agent_hosts ORDER BY heartbeat_at").fetchall():
                host = _host_row(row, now=now)
                heartbeat_at = float(host.get("heartbeat_at") or 0)
                if heartbeat_at > now - 3600:
                    continue
                enrolled = c.execute(
                    "SELECT 1 FROM agent_host_enrollments "
                    "WHERE host_id=? AND status='active' LIMIT 1",
                    (host.get("host_id"),),
                ).fetchone()
                if enrolled:
                    continue
                out.append(_cleanup_candidate(
                    "agent_host", host["host_id"], "remove_stale_host",
                    "host heartbeat has been absent for more than one hour",
                    now, timestamp=heartbeat_at, snapshot=host,
                ))

        if accept("agent_presence"):
            for row in c.execute("SELECT * FROM agent_presence ORDER BY heartbeat_at").fetchall():
                presence = _presence_row(row, now=now)
                if not presence.get("stale"):
                    continue
                out.append(_cleanup_candidate(
                    "agent_presence", presence["agent_id"], "remove_stale_presence",
                    "agent heartbeat expired", now,
                    task_id=presence.get("task_id") or "",
                    timestamp=presence.get("expires_at"),
                    snapshot=presence,
                ))

        if accept("runner_session"):
            for row in c.execute("SELECT * FROM runner_sessions ORDER BY heartbeat_at").fetchall():
                session = _runner_session_row(row, now=now, include_claim=True, c=c)
                if not session.get("stale"):
                    continue
                status = str(session.get("status") or "").lower()
                if status in TERMINAL_RUNNER_STATUSES:
                    continue
                out.append(_cleanup_candidate(
                    "runner_session", session["runner_session_id"], "expire_runner_session",
                    "runner heartbeat expired", now,
                    task_id=session.get("task_id") or "",
                    timestamp=session.get("expires_at"),
                    snapshot=session,
                ))

        if accept("task_claim"):
            rows = c.execute(
                "SELECT * FROM task_claims WHERE status='active' "
                "AND (expires_at<=? OR task_id NOT IN (SELECT task_id FROM tasks)) "
                "ORDER BY expires_at, id",
                (now,),
            ).fetchall()
            for row in rows:
                claim = dict(row)
                orphaned = claim["task_id"] not in task_ids
                reason = "claim task is missing" if orphaned else "claim lease expired"
                out.append(_cleanup_candidate(
                    "task_claim", claim["id"], "abandon_expired_claim", reason, now,
                    task_id=claim.get("task_id") or "",
                    timestamp=claim.get("expires_at"),
                    severity="medium",
                    snapshot=claim,
                ))

        if accept("file_lease"):
            for row in c.execute("SELECT * FROM file_leases WHERE released_at IS NULL "
                                 "ORDER BY claimed_at").fetchall():
                lease = dict(row)
                expires_at = float(lease.get("claimed_at") or 0) + int(lease.get("ttl_minutes") or 0) * 60
                if expires_at > now:
                    continue
                lease["expires_at"] = expires_at
                out.append(_cleanup_candidate(
                    "file_lease", str(lease["id"]), "release_expired_lease",
                    "file lease expired", now,
                    task_id=lease.get("task_id") or "",
                    timestamp=expires_at,
                    severity="medium",
                    snapshot=lease,
                ))

        if accept("resource_lease"):
            for row in c.execute("SELECT * FROM resource_leases WHERE released_at IS NULL "
                                 "ORDER BY claimed_at").fetchall():
                lease = dict(row)
                expires_at = float(lease.get("claimed_at") or 0) + int(lease.get("ttl_seconds") or 0)
                if expires_at > now:
                    continue
                lease["expires_at"] = expires_at
                out.append(_cleanup_candidate(
                    "resource_lease", lease["id"], "release_expired_lease",
                    f"{lease.get('resource_type') or 'resource'} lease expired", now,
                    task_id=lease.get("task_id") or "",
                    timestamp=expires_at,
                    severity="medium",
                    snapshot=lease,
                ))

        if accept("wake_intent"):
            for row in c.execute("SELECT * FROM wake_intents ORDER BY requested_at").fetchall():
                wake = _wake_row(row)
                status = wake.get("status")
                if status in TERMINAL_WAKE_STATUSES:
                    continue
                deadline = wake.get("deadline")
                old_without_deadline = (
                    deadline is None and
                    _cleanup_age_seconds(now, wake.get("requested_at") or 0) is not None and
                    _cleanup_age_seconds(now, wake.get("requested_at") or 0) >= 86400
                )
                if deadline is None and not old_without_deadline:
                    continue
                if deadline is not None and float(deadline) > now:
                    continue
                out.append(_cleanup_candidate(
                    "wake_intent", wake["wake_id"], "cancel_old_wake",
                    "wake intent deadline expired" if deadline else "wake intent is older than 24h",
                    now,
                    task_id=wake.get("task_id") or "",
                    timestamp=deadline or wake.get("requested_at"),
                    snapshot=wake,
                ))

        if accept("monitor"):
            for row in c.execute("SELECT * FROM coordination_monitors ORDER BY created_at").fetchall():
                mon = _monitor_row(row) or {}
                action = ""
                reason = ""
                if mon.get("status") == "fired":
                    action = "resolve_fired_monitor"
                    reason = "monitor already fired and needs operator resolution"
                elif mon.get("status") == "pending" and mon.get("target_type") == "agent_message":
                    msg = c.execute("SELECT 1 FROM agent_messages WHERE id=?",
                                    (int(mon.get("target_id") or 0),)).fetchone()
                    if not msg:
                        action = "cancel_orphan_monitor"
                        reason = "monitor target message is missing"
                if not action:
                    continue
                out.append(_cleanup_candidate(
                    "monitor", mon["id"], action, reason, now,
                    task_id=mon.get("task_id") or "",
                    timestamp=mon.get("fired_at") or mon.get("deadline") or mon.get("created_at"),
                    snapshot=mon,
                ))

        if accept("proof_task"):
            rows = c.execute(
                "SELECT * FROM tasks WHERE status IN ('Done','Cancelled','Canceled') "
                "ORDER BY updated_at, task_id"
            ).fetchall()
            for row in rows:
                task = _task_row(row)
                if not _is_cleanup_proof_task(task):
                    continue
                age = _cleanup_age_seconds(now, task.get("updated_at"))
                if age is None or age < min_proof_age:
                    continue
                active = _active_task_state_in(c, task["task_id"], now)
                if active["claims"] or active["resource_leases"] or active["file_leases"]:
                    continue
                out.append(_cleanup_candidate(
                    "proof_task", task["task_id"], "archive_terminal_proof_task",
                    "old terminal proof/sentinel task", now,
                    task_id=task["task_id"],
                    timestamp=task.get("updated_at"),
                    snapshot=task,
                ))

    return {"project": project, "generated_at": now, "candidates": out,
            "summary": _cleanup_summary(out)}


def _cleanup_candidate_ids(candidates: List[Dict[str, Any]]) -> set:
    return {c["id"] for c in candidates}


def apply_cleanup(project: str = DEFAULT_PROJECT,
                  candidate_ids: Optional[List[str]] = None,
                  dry_run: bool = True,
                  actor: str = "switchboard/operator",
                  reason: str = "",
                  now: Optional[float] = None,
                  proof_task_age_days: float = 14,
                  include_kinds: Optional[List[str]] = None) -> Dict[str, Any]:
    """Apply selected lifecycle cleanups, or return the dry-run plan.

    The function recomputes candidates inside the request and only applies current candidate ids.
    Every mutation writes a `cleanup.*` activity row with the candidate snapshot.
    """
    now = time.time() if now is None else float(now)
    reason = (reason or "lifecycle cleanup").strip()
    plan = cleanup_candidates(project=project, now=now,
                              proof_task_age_days=proof_task_age_days,
                              include_kinds=include_kinds)
    if plan.get("error"):
        return plan
    candidates = plan["candidates"]
    requested = {cid.strip() for cid in (candidate_ids or []) if cid.strip()}
    if requested:
        candidates = [c for c in candidates if c["id"] in requested]
    if dry_run:
        return {"project": project, "dry_run": True, "generated_at": now,
                "candidates": candidates, "summary": _cleanup_summary(candidates)}

    results: List[Dict[str, Any]] = []
    available = _cleanup_candidate_ids(candidates)
    missing = sorted(requested - available) if requested else []

    with _conn(project) as c:
        for candidate in candidates:
            kind = candidate["kind"]
            target_id = candidate["target_id"]
            payload = {"candidate": candidate, "reason": reason}
            try:
                if kind == "agent_host":
                    changed = c.execute(
                        "DELETE FROM agent_hosts WHERE host_id=? "
                        "AND heartbeat_at<=? AND NOT EXISTS ("
                        "SELECT 1 FROM agent_host_enrollments "
                        "WHERE host_id=? AND status='active')",
                        (target_id, now - 3600, target_id),
                    )
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (None, actor, "cleanup.agent_host_removed",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"],
                                    "applied": changed.rowcount == 1,
                                    "action": candidate["action"]})
                elif kind == "agent_presence":
                    c.execute("DELETE FROM agent_presence WHERE agent_id=?", (target_id,))
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (candidate.get("task_id"), actor,
                               "cleanup.agent_presence_resolved",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"]})
                elif kind == "runner_session":
                    c.execute("UPDATE runner_sessions SET status='expired', updated_at=? "
                              "WHERE runner_session_id=?", (now, target_id))
                    _clear_active_runner_pointer_in(
                        c, str(candidate.get("task_id") or ""), target_id, now)
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (candidate.get("task_id"), actor,
                               "cleanup.runner_session_expired",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"]})
                elif kind == "task_claim":
                    claim = candidate.get("snapshot") or {}
                    c.execute("UPDATE task_claims SET status='abandoned', completed_at=?, "
                              "abandon_reason=? WHERE id=? AND status='active'",
                              (now, f"cleanup: {reason}", target_id))
                    c.execute("UPDATE resource_leases SET released_at=? WHERE resource_type='task' "
                              "AND task_id=? AND agent_id=? AND released_at IS NULL",
                              (now, claim.get("task_id"), claim.get("agent_id")))
                    c.execute("UPDATE tasks SET status='Not Started', "
                              "assignee=CASE WHEN assignee=? THEN NULL ELSE assignee END, "
                              "updated_at=? WHERE task_id=? AND status='In Progress'",
                              (claim.get("agent_id"), now, claim.get("task_id")))
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (claim.get("task_id"), actor,
                               "cleanup.task_claim_abandoned",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"]})
                elif kind == "file_lease":
                    c.execute("UPDATE file_leases SET released_at=? WHERE id=?",
                              (now, target_id))
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (candidate.get("task_id"), actor,
                               "cleanup.lease_released",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"]})
                elif kind == "resource_lease":
                    c.execute("UPDATE resource_leases SET released_at=? WHERE id=?",
                              (now, target_id))
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (candidate.get("task_id"), actor,
                               "cleanup.lease_released",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"]})
                elif kind == "wake_intent":
                    wake = candidate.get("snapshot") or {}
                    result = dict(wake.get("result") or {})
                    result.update({"reason": reason, "cancelled_by": actor,
                                   "cleanup_candidate_id": candidate["id"]})
                    c.execute("UPDATE wake_intents SET status='cancelled', completed_at=?, "
                              "result_json=? WHERE wake_id=?",
                              (now, json.dumps(result, sort_keys=True), target_id))
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (candidate.get("task_id"), actor, "cleanup.wake_cancelled",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"]})
                elif kind == "monitor":
                    mon = candidate.get("snapshot") or {}
                    status = "resolved" if candidate["action"] == "resolve_fired_monitor" else "cancelled"
                    result = dict(mon.get("result") or {})
                    result.update({"reason": reason, "resolved_by": actor,
                                   "cleanup_candidate_id": candidate["id"]})
                    c.execute("UPDATE coordination_monitors SET status=?, resolved_at=?, "
                              "updated_at=?, last_checked_at=?, result_json=? WHERE id=?",
                              (status, now, now, now, json.dumps(result, sort_keys=True),
                               target_id))
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (candidate.get("task_id"), actor,
                               "cleanup.monitor_resolved" if status == "resolved"
                               else "cleanup.monitor_cancelled",
                               json.dumps(payload, sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"]})
                elif kind == "proof_task":
                    snapshot = _task_snapshot_in(c, target_id)
                    if not snapshot:
                        results.append({"id": candidate["id"], "applied": False,
                                        "error": "task not found"})
                        continue
                    active = _active_task_state_in(c, target_id, now)
                    if active["claims"] or active["resource_leases"] or active["file_leases"]:
                        results.append({"id": candidate["id"], "applied": False,
                                        "error": "task has active claims or leases",
                                        "active": active})
                        continue
                    archive_id = _insert_archive_in(c, target_id, "cleanup_archive",
                                                    actor, reason, project, "",
                                                    snapshot, now)
                    _delete_task_related_in(c, target_id, snapshot)
                    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                              "VALUES (?,?,?,?,?)",
                              (target_id, actor, "cleanup.task_archived",
                               json.dumps(payload | {"archive_id": archive_id},
                                          sort_keys=True), now))
                    results.append({"id": candidate["id"], "applied": True,
                                    "action": candidate["action"],
                                    "archive_id": archive_id})
            except Exception as exc:
                results.append({"id": candidate["id"], "applied": False,
                                "error": type(exc).__name__, "message": str(exc)})

    applied = [r for r in results if r.get("applied")]
    return {"project": project, "dry_run": False, "generated_at": now,
            "requested_ids": sorted(requested), "missing_ids": missing,
            "results": results, "applied_count": len(applied),
            "summary": _cleanup_summary(candidates)}


class StoreLifecycleCleanupRepository:
    """Thin repository wrapper over module-level lifecycle cleanup helpers."""

    def cleanup_candidates(self, *args, **kwargs):
        return cleanup_candidates(*args, **kwargs)

    def apply_cleanup(self, *args, **kwargs):
        return apply_cleanup(*args, **kwargs)


def default_lifecycle_cleanup_repository() -> StoreLifecycleCleanupRepository:
    return StoreLifecycleCleanupRepository()


__all__ = [
    "StoreLifecycleCleanupRepository",
    "default_lifecycle_cleanup_repository",
    "_cleanup_age_seconds",
    "_cleanup_candidate",
    "_cleanup_summary",
    "_cleanup_candidate_ids",
    "cleanup_candidates",
    "apply_cleanup",
]
