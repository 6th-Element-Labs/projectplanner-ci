"""Persistence for receipt-gated project consolidation workflows (ACCESS-23)."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Any, Mapping

from db.connection import bust_project_cache
from db.core import _registry_conn
from db.schema import init_project_registry


SAFE_ROUTING_META_KEYS = frozenset({
    "comms_inbound_domains",
    "comms_digest_recipients",
    "comms_notify_recipients",
})


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _table_exists(c: sqlite3.Connection, table: str) -> bool:
    return bool(c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _open(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(os.path.abspath(os.path.expanduser(path)))
    c.row_factory = sqlite3.Row
    return c


def _rows(c: sqlite3.Connection, table: str, columns: str,
          order_by: str) -> list[dict[str, Any]]:
    if not _table_exists(c, table):
        return []
    return [dict(row) for row in c.execute(
        f"SELECT {columns} FROM {table} ORDER BY {order_by}").fetchall()]


def _meta_raw(c: sqlite3.Connection, key: str) -> dict[str, Any]:
    if not _table_exists(c, "meta"):
        return {"exists": False, "raw": None, "value": []}
    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return {"exists": False, "raw": None, "value": []}
    raw = row["value"]
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        value = None
    return {"exists": True, "raw": raw, "value": value}


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return sorted({str(item).strip() for item in value if str(item).strip()})


class ProjectConsolidationRepository:
    """Registry receipts plus narrowly allowlisted board routing transfers."""

    def history_snapshot(self, project_id: str,
                         project_configs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        path = str((project_configs.get(project_id) or {}).get("db") or "")
        if not path or not os.path.isfile(path):
            return {"error": "project_database_unavailable", "project_id": project_id}
        try:
            with _open(path) as c:
                body = {
                    "tasks": _rows(
                        c, "tasks",
                        "task_id, title, status, workstream_id, depends_on, assignee, updated_at",
                        "task_id"),
                    "task_git_state": _rows(
                        c, "task_git_state",
                        "task_id, branch, head_sha, pr_number, pr_url, merged_sha, merged_at, "
                        "in_main_content, evidence_json, updated_at",
                        "task_id"),
                    "boards": _rows(
                        c, "project_boards", "id, title, kind, status, updated_at", "id"),
                    "deliverables": _rows(
                        c, "deliverables", "id, board_id, title, status, updated_at", "id"),
                }
        except (OSError, sqlite3.Error) as exc:
            return {"error": "project_database_unavailable", "project_id": project_id,
                    "error_type": type(exc).__name__}
        return {
            "schema": "switchboard.project_consolidation.history.v1",
            "project_id": project_id,
            "counts": {key: len(value) for key, value in body.items()},
            "history_hash": _hash(body),
        }

    def target_snapshot(self, project_id: str,
                        project_configs: Mapping[str, Mapping[str, Any]], *,
                        board_id: str = "", mission_id: str = "",
                        deliverable_id: str = "") -> dict[str, Any]:
        path = str((project_configs.get(project_id) or {}).get("db") or "")
        if not path or not os.path.isfile(path):
            return {"valid": False, "errors": ["replacement project database unavailable"]}
        errors: list[str] = []
        selected_board = board_id or mission_id
        try:
            with _open(path) as c:
                board = None
                if selected_board:
                    if not _table_exists(c, "project_boards"):
                        errors.append("replacement project has no board registry")
                    else:
                        row = c.execute(
                            "SELECT id, title, kind, status FROM project_boards WHERE id=?",
                            (selected_board,),
                        ).fetchone()
                        board = dict(row) if row else None
                        if not board:
                            errors.append(f"unknown replacement board/mission: {selected_board}")
                        elif mission_id and str(board.get("kind") or "").lower() != "mission":
                            errors.append(f"replacement mission is not mission-kind: {mission_id}")
                deliverable = None
                if deliverable_id:
                    if not _table_exists(c, "deliverables"):
                        errors.append("replacement project has no deliverable registry")
                    else:
                        row = c.execute(
                            "SELECT id, board_id, title, status FROM deliverables WHERE id=?",
                            (deliverable_id,),
                        ).fetchone()
                        deliverable = dict(row) if row else None
                        if not deliverable:
                            errors.append(f"unknown replacement deliverable: {deliverable_id}")
                        elif selected_board and deliverable.get("board_id") != selected_board:
                            errors.append("replacement deliverable does not belong to selected board/mission")
        except (OSError, sqlite3.Error) as exc:
            return {"valid": False, "errors": ["replacement project database unavailable"],
                    "error_type": type(exc).__name__}
        return {
            "valid": not errors,
            "errors": errors,
            "project_id": project_id,
            "board": board,
            "deliverable": deliverable,
        }

    def routing_plan(self, source_project_id: str, replacement_project_id: str,
                     keys: list[str],
                     project_configs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        unknown = sorted(set(keys) - SAFE_ROUTING_META_KEYS)
        if unknown:
            return {"error": "unsafe_routing_keys", "keys": unknown,
                    "allowed": sorted(SAFE_ROUTING_META_KEYS)}
        source_path = str((project_configs.get(source_project_id) or {}).get("db") or "")
        target_path = str((project_configs.get(replacement_project_id) or {}).get("db") or "")
        try:
            with _open(source_path) as source, _open(target_path) as target:
                proposed = []
                for key in sorted(keys):
                    source_state = _meta_raw(source, key)
                    target_state = _meta_raw(target, key)
                    source_values = _string_list(source_state.get("value"))
                    target_values = _string_list(target_state.get("value"))
                    if source_values is None or target_values is None:
                        return {"error": "unsafe_routing_value", "key": key,
                                "message": "safe routing values must be JSON string lists"}
                    merged = sorted(set(source_values) | set(target_values))
                    proposed.append({
                        "key": key,
                        "source_count": len(source_values),
                        "target_count_before": len(target_values),
                        "target_count_after": len(merged),
                        "source_value_hash": _hash(source_values),
                        "target_value_hash_after": _hash(merged),
                    })
        except (OSError, sqlite3.Error) as exc:
            return {"error": "routing_inventory_unavailable",
                    "error_type": type(exc).__name__}
        return {"rewrites": proposed, "keys": sorted(keys)}

    def get(self, consolidation_id: str) -> dict[str, Any] | None:
        init_project_registry()
        with _registry_conn() as c:
            row = c.execute(
                "SELECT * FROM project_consolidations WHERE consolidation_id=?",
                (consolidation_id,),
            ).fetchone()
        return self._decode(dict(row)) if row else None

    def get_by_plan_hash(self, plan_hash: str) -> dict[str, Any] | None:
        init_project_registry()
        with _registry_conn() as c:
            row = c.execute(
                "SELECT * FROM project_consolidations WHERE plan_hash=?", (plan_hash,)
            ).fetchone()
        return self._decode(dict(row)) if row else None

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        for key in ("plan_json", "history_json", "routing_json", "rollback_json"):
            raw = row.pop(key, "{}")
            row[key[:-5]] = json.loads(raw or "{}")
        return row

    @staticmethod
    def _restore_meta(path: str, before: Mapping[str, Mapping[str, Any]]) -> None:
        with _open(path) as c:
            for key, state in before.items():
                if state.get("exists"):
                    c.execute(
                        "INSERT INTO meta(key,value) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, state.get("raw")),
                    )
                else:
                    c.execute("DELETE FROM meta WHERE key=?", (key,))

    def apply(self, plan: Mapping[str, Any], history: Mapping[str, Any], *,
              actor: str,
              project_configs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        receipt = dict(plan.get("receipt") or {})
        plan_hash = str(receipt.get("plan_hash") or "")
        existing = self.get_by_plan_hash(plan_hash)
        if existing:
            return {"record": existing, "idempotent": True}
        source = str(plan.get("source_project_id") or "")
        target = str(plan.get("replacement_project_id") or "")
        source_path = str((project_configs.get(source) or {}).get("db") or "")
        target_path = str((project_configs.get(target) or {}).get("db") or "")
        keys = list((plan.get("routing_rewrites") or {}).get("keys") or [])
        target_before: dict[str, dict[str, Any]] = {}
        target_after: dict[str, list[str]] = {}
        try:
            with _open(source_path) as source_db, _open(target_path) as target_db:
                for key in keys:
                    source_state = _meta_raw(source_db, key)
                    target_state = _meta_raw(target_db, key)
                    source_values = _string_list(source_state.get("value"))
                    target_values = _string_list(target_state.get("value"))
                    if source_values is None or target_values is None:
                        return {"error": "unsafe_routing_value", "key": key}
                    target_before[key] = {
                        "exists": bool(target_state.get("exists")),
                        "raw": target_state.get("raw"),
                    }
                    merged = sorted(set(source_values) | set(target_values))
                    target_db.execute(
                        "INSERT INTO meta(key,value) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(merged, sort_keys=True)),
                    )
                    target_after[key] = merged
        except (OSError, sqlite3.Error) as exc:
            return {"error": "routing_rewrite_failed", "error_type": type(exc).__name__}

        suffix = plan_hash.removeprefix("sha256:")[:16]
        consolidation_id = f"project-consolidation-{suffix}"
        now = time.time()
        approval = dict(plan.get("approval") or {})
        rollback = {
            "schema": "switchboard.project_consolidation.rollback_receipt.v1",
            "consolidation_id": consolidation_id,
            "source_project_id": source,
            "replacement_project_id": target,
            "history_hash": history.get("history_hash"),
            "target_meta_before": target_before,
            "target_meta_after_hashes": {key: _hash(value)
                                          for key, value in target_after.items()},
        }
        try:
            init_project_registry()
            with _registry_conn() as c:
                c.execute(
                    "INSERT INTO project_consolidations("
                    "consolidation_id, source_project_id, replacement_project_id, "
                    "replacement_board_id, replacement_mission_id, replacement_deliverable_id, "
                    "status, plan_hash, impact_report_hash, plan_json, history_json, routing_json, "
                    "rollback_json, actor, reason, approved_by, approved_at, created_at, applied_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (consolidation_id, source, target,
                     plan.get("replacement_board_id"), plan.get("replacement_mission_id"),
                     plan.get("replacement_deliverable_id"), "applied", plan_hash,
                     (plan.get("impact_receipt") or {}).get("report_hash") or "",
                     json.dumps(dict(plan), sort_keys=True),
                     json.dumps(dict(history), sort_keys=True),
                     json.dumps({"keys": keys, "target_after": target_after}, sort_keys=True),
                     json.dumps(rollback, sort_keys=True), actor,
                     str(plan.get("reason") or ""), approval.get("approved_by") or actor,
                     float(approval.get("approved_at") or now), now, now),
                )
                c.execute(
                    "UPDATE projects SET replacement_project_id=?, replacement_board_id=?, "
                    "replacement_mission_id=?, replacement_deliverable_id=?, "
                    "replacement_consolidation_id=?, updated_at=?, updated_by=? WHERE id=?",
                    (target, plan.get("replacement_board_id"), plan.get("replacement_mission_id"),
                     plan.get("replacement_deliverable_id"), consolidation_id, now, actor, source),
                )
        except (OSError, sqlite3.Error) as exc:
            self._restore_meta(target_path, target_before)
            return {"error": "consolidation_receipt_write_failed",
                    "error_type": type(exc).__name__}
        bust_project_cache()
        return {"record": self.get(consolidation_id), "idempotent": False}

    def routing_matches(self, record: Mapping[str, Any],
                        project_configs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        target = str(record.get("replacement_project_id") or "")
        path = str((project_configs.get(target) or {}).get("db") or "")
        expected = dict((record.get("routing") or {}).get("target_after") or {})
        mismatches = []
        try:
            with _open(path) as c:
                for key, value in sorted(expected.items()):
                    observed = _string_list(_meta_raw(c, key).get("value"))
                    if observed != value:
                        mismatches.append(key)
        except (OSError, sqlite3.Error) as exc:
            return {"ok": False, "mismatches": sorted(expected),
                    "error_type": type(exc).__name__}
        return {"ok": not mismatches, "mismatches": mismatches}

    def mark_verified(self, consolidation_id: str) -> dict[str, Any] | None:
        now = time.time()
        init_project_registry()
        with _registry_conn() as c:
            c.execute(
                "UPDATE project_consolidations SET status='verified', verified_at=? "
                "WHERE consolidation_id=? AND status IN ('applied','verified')",
                (now, consolidation_id),
            )
        return self.get(consolidation_id)

    def rollback(self, record: Mapping[str, Any], *, actor: str, reason: str,
                 project_configs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        consolidation_id = str(record.get("consolidation_id") or "")
        if record.get("status") == "rolled_back":
            return {"record": dict(record), "idempotent": True}
        target = str(record.get("replacement_project_id") or "")
        target_path = str((project_configs.get(target) or {}).get("db") or "")
        before = dict((record.get("rollback") or {}).get("target_meta_before") or {})
        try:
            self._restore_meta(target_path, before)
            now = time.time()
            init_project_registry()
            with _registry_conn() as c:
                c.execute(
                    "UPDATE projects SET replacement_project_id=NULL, replacement_board_id=NULL, "
                    "replacement_mission_id=NULL, replacement_deliverable_id=NULL, "
                    "replacement_consolidation_id=NULL, updated_at=?, updated_by=? "
                    "WHERE id=? AND replacement_consolidation_id=?",
                    (now, actor, record.get("source_project_id"), consolidation_id),
                )
                c.execute(
                    "UPDATE project_consolidations SET status='rolled_back', rolled_back_at=?, "
                    "rollback_reason=?, rollback_actor=? WHERE consolidation_id=?",
                    (now, reason, actor, consolidation_id),
                )
        except (OSError, sqlite3.Error) as exc:
            return {"error": "consolidation_rollback_failed",
                    "error_type": type(exc).__name__}
        bust_project_cache()
        return {"record": self.get(consolidation_id), "idempotent": False}


default_project_consolidation_repository = ProjectConsolidationRepository()
