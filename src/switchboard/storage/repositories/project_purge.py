"""Durable registry persistence for guarded project purge (ACCESS-24)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping


class ProjectPurgeRepository:
    def _connect(self, registry_db_path: str) -> sqlite3.Connection:
        connection = sqlite3.connect(registry_db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _intent(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        item = dict(row)
        item["intent"] = json.loads(item.pop("intent_json") or "{}")
        item["failure"] = json.loads(item.pop("failure_json") or "null")
        return item

    def put_intent(self, registry_db_path: str, intent: Mapping[str, Any]) -> dict[str, Any]:
        with self._connect(registry_db_path) as c:
            existing = c.execute(
                "SELECT * FROM project_purge_intents WHERE intent_hash=?",
                (intent["intent_hash"],),
            ).fetchone()
            if existing:
                return self._intent(existing)
            c.execute(
                "INSERT INTO project_purge_intents(intent_id,project_id,status,intent_hash,"
                "impact_report_hash,export_uri,export_hash,export_created_at,retention_days,"
                "intent_json,actor,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (intent["intent_id"], intent["project_id"], "prepared", intent["intent_hash"],
                 intent["impact_report_hash"], intent["export_uri"], intent["export_hash"],
                 intent["export_created_at"], intent["retention_days"],
                 json.dumps(dict(intent["intent"]), sort_keys=True, separators=(",", ":")),
                 intent["actor"], intent["reason"], intent["created_at"]),
            )
            row = c.execute("SELECT * FROM project_purge_intents WHERE intent_id=?",
                            (intent["intent_id"],)).fetchone()
        return self._intent(row)

    def get_intent(self, registry_db_path: str, intent_id: str) -> dict[str, Any]:
        with self._connect(registry_db_path) as c:
            row = c.execute("SELECT * FROM project_purge_intents WHERE intent_id=?",
                            (intent_id,)).fetchone()
        return self._intent(row)

    def mark_verified(self, registry_db_path: str, intent_id: str, verifier: str,
                      verified_at: float) -> dict[str, Any]:
        with self._connect(registry_db_path) as c:
            c.execute(
                "UPDATE project_purge_intents SET status='verified', verified_by=?, verified_at=? "
                "WHERE intent_id=? AND status IN ('prepared','verified')",
                (verifier, verified_at, intent_id),
            )
        return self.get_intent(registry_db_path, intent_id)

    def prepare_tombstone(self, registry_db_path: str, *, intent_id: str, project_id: str,
                          registry_record: Mapping[str, Any], receipt: Mapping[str, Any],
                          database_path_hash: str, created_at: float) -> dict[str, Any]:
        tombstone_id = f"tombstone-{intent_id.removeprefix('purge-')}"
        with self._connect(registry_db_path) as c:
            c.execute(
                "INSERT OR IGNORE INTO project_purge_tombstones(tombstone_id,intent_id,project_id,"
                "registry_record_json,audit_receipt_json,database_path_hash,database_removed,created_at) "
                "VALUES (?,?,?,?,?,?,0,?)",
                (tombstone_id, intent_id, project_id,
                 json.dumps(dict(registry_record), sort_keys=True, separators=(",", ":")),
                 json.dumps(dict(receipt), sort_keys=True, separators=(",", ":")),
                 database_path_hash, created_at),
            )
            row = c.execute("SELECT * FROM project_purge_tombstones WHERE intent_id=?",
                            (intent_id,)).fetchone()
        return dict(row) if row else {}

    def mark_executed(self, registry_db_path: str, intent_id: str, actor: str,
                      executed_at: float, *, database_removed: bool) -> dict[str, Any]:
        with self._connect(registry_db_path) as c:
            c.execute(
                "UPDATE project_purge_intents SET status='executed', executed_by=?, executed_at=? "
                "WHERE intent_id=?", (actor, executed_at, intent_id))
            c.execute(
                "UPDATE project_purge_tombstones SET database_removed=? WHERE intent_id=?",
                (int(database_removed), intent_id))
        return self.get_intent(registry_db_path, intent_id)

    def record_cleanup_review(self, registry_db_path: str,
                              review: Mapping[str, Any]) -> dict[str, Any]:
        with self._connect(registry_db_path) as c:
            c.execute(
                "INSERT OR IGNORE INTO project_cleanup_reviews(review_id,project_id,decision,"
                "impact_report_hash,impact_receipt_json,approved_by,approved_at,rationale,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (review["review_id"], review["project_id"], review["decision"],
                 review["impact_report_hash"],
                 json.dumps(review["impact_report_receipt"], sort_keys=True,
                            separators=(",", ":")),
                 review["approved_by"], review["approved_at"], review["rationale"],
                 review["created_at"]),
            )
            row = c.execute(
                "SELECT * FROM project_cleanup_reviews WHERE project_id=? AND impact_report_hash=?",
                (review["project_id"], review["impact_report_hash"]),
            ).fetchone()
        item = dict(row) if row else {}
        if item:
            item["impact_report_receipt"] = json.loads(item.pop("impact_receipt_json"))
        return item


default_project_purge_repository = ProjectPurgeRepository()
