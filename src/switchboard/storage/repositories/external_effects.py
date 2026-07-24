"""External side-effect ledger repository (ARCH-MS-54).

Owns external_side_effects claim/update/list helpers previously living in
``repositories/shell.py``. Cross-cutting store helpers (init_db) are reached via
``_store_facade()`` during the strangler. ``store.py`` / ``shell.py`` re-export
these symbols; root ``external_effects_store.py`` is a compatibility shim.
"""
from __future__ import annotations

import json
import hashlib
import sqlite3
import time
from typing import Any, Dict, List, Optional

from constants import *  # noqa: F401,F403
from db.connection import _conn
from db.core import *  # noqa: F401,F403


def _store_facade():
    """Resolve transitional store helpers after store.py is initialized."""
    import store
    return store


def init_db(*args, **kwargs):
    return _store_facade().init_db(*args, **kwargs)


EXTERNAL_EFFECT_TERMINAL_STATUSES = {"verified", "failed", "dead_letter", "void"}


def _effect_window_key(now: float, idempotency_window_seconds: int = 0) -> str:
    window = int(idempotency_window_seconds or 0)
    return f"window:{window}:{int(now // window)}" if window > 0 else "permanent"


def make_external_effect_key(effect_type: str, target: str, resource: str,
                             payload: Optional[Dict[str, Any]] = None,
                             idempotency_window_seconds: int = 0,
                             now: Optional[float] = None,
                             project: str = DEFAULT_PROJECT) -> Dict[str, str]:
    """Deterministic key for external effects that must not double-fire."""
    now = time.time() if now is None else float(now)
    effect_type = (effect_type or "").strip().lower()
    target = (target or "").strip()
    resource = (resource or "").strip()
    payload_hash = _payload_hash(payload)
    window_key = _effect_window_key(now, idempotency_window_seconds)
    basis = {
        "project": project,
        "effect_type": effect_type,
        "target": target,
        "resource": resource,
        "payload_hash": payload_hash,
        "window_key": window_key,
    }
    digest = hashlib.sha256(json.dumps(basis, sort_keys=True).encode("utf-8")).hexdigest()
    return {"effect_key": "effect-" + digest[:32],
            "payload_hash": payload_hash, "window_key": window_key}


def _external_effect_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    d["payload"] = _json_obj(d.pop("payload_json", "{}"), {})
    d["readback"] = _json_obj(d.pop("readback_json", "{}"), {})
    return d


def _claim_external_effect_in(c: sqlite3.Connection, effect_type: str, target: str,
                              resource: str, payload: Optional[Dict[str, Any]] = None,
                              task_id: Optional[str] = None, claim_id: str = "",
                              agent_id: str = "", idem_key: str = "",
                              idempotency_window_seconds: int = 0,
                              actor: str = "system", principal_id: str = "",
                              project: str = DEFAULT_PROJECT,
                              now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else float(now)
    payload = _canonical_payload(payload)
    key = make_external_effect_key(
        effect_type, target, resource, payload,
        idempotency_window_seconds=idempotency_window_seconds, now=now, project=project)
    effect_key = key["effect_key"]
    row = c.execute("SELECT * FROM external_side_effects WHERE effect_key=?",
                    (effect_key,)).fetchone()
    if row:
        effect = _external_effect_row(row)
        out = {"claimed": False, "effect": effect, "effect_key": effect_key,
               "idempotent": effect["status"] == "verified"}
        if effect["status"] == "verified":
            out["verified"] = True
            out["proof"] = effect.get("readback") or {}
        elif effect["status"] in EXTERNAL_EFFECT_TERMINAL_STATUSES:
            out["reason"] = f"effect is {effect['status']}"
        else:
            out["reason"] = f"effect already {effect['status']}"
            out["readback_required"] = True
        return out
    c.execute(
        "INSERT INTO external_side_effects(effect_key, project, effect_type, target, "
        "resource, task_id, claim_id, agent_id, status, payload_hash, payload_json, "
        "idem_key, window_key, requested_by, claimed_by, principal_id, requested_at, "
        "claimed_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            effect_key, project, (effect_type or "").strip().lower(), target, resource,
            task_id, claim_id or None, agent_id or None, "claimed", key["payload_hash"],
            json.dumps(payload, sort_keys=True), idem_key or None, key["window_key"],
            actor, actor, principal_id or None, now, now, now,
        ),
    )
    event = {"effect_key": effect_key, "effect_type": (effect_type or "").strip().lower(),
             "target": target, "resource": resource, "payload_hash": key["payload_hash"],
             "status": "claimed", "claim_id": claim_id or None, "agent_id": agent_id or None}
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (task_id, actor, "side_effect.claimed", json.dumps(event, sort_keys=True), now))
    row = c.execute("SELECT * FROM external_side_effects WHERE effect_key=?",
                    (effect_key,)).fetchone()
    return {"claimed": True, "effect": _external_effect_row(row), "effect_key": effect_key}


def claim_external_effect(effect_type: str, target: str, resource: str,
                          payload: Optional[Dict[str, Any]] = None,
                          task_id: Optional[str] = None, claim_id: str = "",
                          agent_id: str = "", idem_key: str = "",
                          idempotency_window_seconds: int = 0,
                          actor: str = "system", principal_id: str = "",
                          project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    with _conn(project) as c:
        return _claim_external_effect_in(
            c, effect_type, target, resource, payload, task_id=task_id,
            claim_id=claim_id, agent_id=agent_id, idem_key=idem_key,
            idempotency_window_seconds=idempotency_window_seconds, actor=actor,
            principal_id=principal_id, project=project)


def retry_external_effect(effect_key: str, *, expected_retry_count: int,
                          actor: str = "system",
                          project: str = DEFAULT_PROJECT,
                          now: Optional[float] = None) -> Dict[str, Any]:
    """Atomically reclaim one failed idempotent effect for a bounded retry.

    A failed row is terminal to ordinary ``claim_external_effect`` callers.
    Completion recovery uses this explicit compare-and-swap so concurrent ticks
    cannot both reissue the same external mutation.
    """
    init_db(project)
    at = time.time() if now is None else float(now)
    with _conn(project) as c:
        updated = c.execute(
            "UPDATE external_side_effects SET status='claimed',claimed_by=?,"
            "claimed_at=?,updated_at=? WHERE effect_key=? AND status='failed' "
            "AND retry_count=?",
            (actor, at, at, effect_key, int(expected_retry_count)),
        )
        row = c.execute(
            "SELECT * FROM external_side_effects WHERE effect_key=?",
            (effect_key,),
        ).fetchone()
        effect = _external_effect_row(row)
        if updated.rowcount != 1:
            return {
                "claimed": False,
                "effect_key": effect_key,
                "effect": effect,
                "reason": (
                    f"effect already {effect.get('status')}"
                    if effect else "effect_not_found"
                ),
            }
        c.execute(
            "INSERT INTO activity(task_id,actor,kind,payload,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                effect.get("task_id"), actor, "side_effect.retry_claimed",
                json.dumps({
                    "effect_key": effect_key,
                    "retry_count": int(expected_retry_count),
                }, sort_keys=True),
                at,
            ),
        )
        return {
            "claimed": True,
            "effect_key": effect_key,
            "effect": effect,
            "retry": True,
        }


def _update_external_effect_in(c: sqlite3.Connection, effect_key: str, status: str,
                               readback: Optional[Dict[str, Any]] = None,
                               last_error: str = "", actor: str = "system",
                               task_id: Optional[str] = None,
                               project: str = DEFAULT_PROJECT,
                               now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else float(now)
    row = c.execute("SELECT * FROM external_side_effects WHERE effect_key=?",
                    (effect_key,)).fetchone()
    if not row:
        return {"error": "effect_not_found", "effect_key": effect_key}
    effect = _external_effect_row(row)
    status = (status or "").strip().lower()
    if status not in {"issued", "verified", "failed", "dead_letter", "void"}:
        return {"error": "unsupported_effect_status", "status": status}
    readback_obj = _canonical_payload(readback if readback is not None else effect.get("readback"))
    sets = ["status=?", "readback_json=?", "updated_at=?"]
    vals: List[Any] = [status, json.dumps(readback_obj, sort_keys=True), now]
    if status == "issued":
        sets.extend(["issued_at=COALESCE(issued_at, ?)", "issued_by=COALESCE(issued_by, ?)"])
        vals.extend([now, actor])
    if status == "verified":
        sets.extend(["verified_at=COALESCE(verified_at, ?)", "verified_by=COALESCE(verified_by, ?)"])
        vals.extend([now, actor])
    if last_error:
        sets.append("last_error=?")
        vals.append(last_error)
    elif status in {"issued", "verified"}:
        sets.append("last_error=NULL")
    if status in {"failed", "dead_letter"}:
        sets.append("retry_count=retry_count+1")
    vals.append(effect_key)
    c.execute(f"UPDATE external_side_effects SET {', '.join(sets)} WHERE effect_key=?", vals)
    row = c.execute("SELECT * FROM external_side_effects WHERE effect_key=?",
                    (effect_key,)).fetchone()
    updated = _external_effect_row(row)
    event = {"effect_key": effect_key, "effect_type": updated["effect_type"],
             "target": updated["target"], "resource": updated["resource"],
             "status": status, "readback": readback_obj}
    if last_error:
        event["last_error"] = last_error
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (task_id or updated.get("task_id"), actor, f"side_effect.{status}",
               json.dumps(event, sort_keys=True), now))
    return {"effect_key": effect_key, "effect": updated}


def mark_external_effect_issued(effect_key: str, readback: Optional[Dict[str, Any]] = None,
                                actor: str = "system",
                                project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    with _conn(project) as c:
        return _update_external_effect_in(c, effect_key, "issued", readback=readback,
                                          actor=actor, project=project)


def verify_external_effect(effect_key: str, readback: Optional[Dict[str, Any]] = None,
                           actor: str = "system",
                           project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    with _conn(project) as c:
        return _update_external_effect_in(c, effect_key, "verified", readback=readback,
                                          actor=actor, project=project)


def fail_external_effect(effect_key: str, error: str, readback: Optional[Dict[str, Any]] = None,
                         dead_letter: bool = False, actor: str = "system",
                         project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    with _conn(project) as c:
        return _update_external_effect_in(
            c, effect_key, "dead_letter" if dead_letter else "failed",
            readback=readback or {}, last_error=error or "effect_failed",
            actor=actor, project=project)


def list_external_effects(effect_type: str = "", status: str = "", task_id: str = "",
                          target: str = "", project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    init_db(project)
    q = "SELECT * FROM external_side_effects WHERE 1=1"
    params: List[Any] = []
    if effect_type:
        q += " AND effect_type=?"; params.append(effect_type.strip().lower())
    if status:
        q += " AND status=?"; params.append(status.strip().lower())
    if task_id:
        q += " AND task_id=?"; params.append(task_id)
    if target:
        q += " AND target=?"; params.append(target)
    q += " ORDER BY updated_at DESC, effect_key"
    with _conn(project) as c:
        return [_external_effect_row(row) for row in c.execute(q, params).fetchall()]



class StoreExternalEffectsRepository:
    """Thin repository wrapper over module-level external-effect helpers."""

    def make_external_effect_key(self, *args, **kwargs):
        return make_external_effect_key(*args, **kwargs)

    def claim_external_effect(self, *args, **kwargs):
        return claim_external_effect(*args, **kwargs)

    def mark_external_effect_issued(self, *args, **kwargs):
        return mark_external_effect_issued(*args, **kwargs)

    def retry_external_effect(self, *args, **kwargs):
        return retry_external_effect(*args, **kwargs)

    def verify_external_effect(self, *args, **kwargs):
        return verify_external_effect(*args, **kwargs)

    def fail_external_effect(self, *args, **kwargs):
        return fail_external_effect(*args, **kwargs)

    def list_external_effects(self, *args, **kwargs):
        return list_external_effects(*args, **kwargs)


def default_external_effects_repository() -> StoreExternalEffectsRepository:
    return StoreExternalEffectsRepository()


__all__ = [
    "EXTERNAL_EFFECT_TERMINAL_STATUSES",
    "StoreExternalEffectsRepository",
    "default_external_effects_repository",
    "make_external_effect_key",
    "_effect_window_key",
    "_external_effect_row",
    "_claim_external_effect_in",
    "claim_external_effect",
    "retry_external_effect",
    "_update_external_effect_in",
    "mark_external_effect_issued",
    "verify_external_effect",
    "fail_external_effect",
    "list_external_effects",
]
