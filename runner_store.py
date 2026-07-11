"""Runner session and control-request persistence (ARCH-MS-20).

Extracted from the legacy store.py facade. The public API remains re-exported by
store.py while callers migrate toward repository-bound storage modules.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from constants import DEFAULT_PROJECT
from db.connection import _conn
from db.core import _json_obj, _text_tail

RUNNER_CONTROL_ACTIONS = {"snapshot", "kill", "restart", "health", "logs", "open"}

__all__ = [
    "upsert_runner_session",
    "list_runner_sessions",
    "get_runner_session",
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


def _upsert_runner_session_in(c: sqlite3.Connection, record: Dict[str, Any],
                              principal_id: str, actor: str, now: float) -> Dict[str, Any]:
    runner_session_id = (record.get("runner_session_id") or record.get("id") or "").strip()
    if not runner_session_id:
        return {"error": "runner_session_id required"}
    host_id = (record.get("host_id") or "").strip()
    control = _normalize_runner_control(record.get("control") or {}, host_id)
    metadata = dict(record.get("metadata") or {})
    for key in ("command", "log_path", "pgid", "wake_id", "wake_mode", "alive"):
        if key in record and key not in metadata:
            metadata[key] = record.get(key)
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
    return _runner_session_row(row, now=now, include_claim=True, c=c)


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
    if action not in RUNNER_CONTROL_ACTIONS:
        return {"requested": False, "error": "unsupported_action", "action": action}
    with _conn(project) as c:
        row = c.execute("SELECT * FROM runner_sessions WHERE runner_session_id=?",
                        (runner_session_id,)).fetchone()
        if not row:
            return {"requested": False, "error": "runner_session_not_found",
                    "runner_session_id": runner_session_id}
        session = _runner_session_row(row, now=now, include_claim=True, c=c)
        available = set(session.get("available_actions") or [])
        effect_payload = {
            "runner_session_id": runner_session_id,
            "host_id": session.get("host_id"),
            "action": action,
            "options": options or {},
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
                json.dumps(options or {}, sort_keys=True),
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
