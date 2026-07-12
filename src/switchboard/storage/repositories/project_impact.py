"""Read-only SQLite projection for project lifecycle impact reports.

Every connection is opened with SQLite ``mode=ro`` and ``query_only``.  The
repository deliberately returns counts plus bounded, stable samples; it never
initializes schemas, repairs rows, or updates lifecycle state.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote


TERMINAL_TASK_STATUSES = {"done", "archived", "cancelled", "canceled"}
TERMINAL_DELIVERABLE_STATUSES = {"done", "completed", "archived", "cancelled", "canceled"}
ACTIVE_CLAIM_STATUSES = {"active"}
ACTIVE_WORK_SESSION_STATUSES = {"active", "proposed"}
ACTIVE_BOARD_STATUSES = {"active", "proposed"}
ACTIVE_JOB_STATUSES = {"pending", "queued", "running", "retrying"}
ACTIVE_MONITOR_STATUSES = {"pending", "active", "fired"}
PENDING_WEBHOOK_STATUSES = {"pending", "processing", "failed", "error"}
PENDING_CI_STATUSES = {"requested", "mirrored", "triggered", "pending", "running", "failed", "error"}


def _decode_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, type(default)):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _depends_on(value: Any) -> list[str]:
    parsed = _decode_json(value, [])
    if parsed:
        return sorted({str(item).strip().upper() for item in parsed if str(item).strip()})
    return sorted({part.strip().upper() for part in str(value or "").replace(",", " ").split()
                   if part.strip()})


def _bounded(items: Iterable[dict[str, Any]], limit: int) -> dict[str, Any]:
    rows = list(items)
    return {
        "total": len(rows),
        "returned": min(len(rows), limit),
        "truncated": len(rows) > limit,
        "items": rows[:limit],
    }


class ReadOnlyDatabase:
    """Tiny fail-closed read adapter for one already-existing SQLite file."""

    def __init__(self, path: str, *, connector: Callable[..., sqlite3.Connection] = sqlite3.connect):
        self.path = os.path.abspath(os.path.expanduser(str(path or ""))) if path else ""
        self.connector = connector
        self.connection: sqlite3.Connection | None = None
        self.error_code: str | None = None
        self.error_type: str | None = None

    def _fail(self, code: str, exc: BaseException | None = None) -> None:
        if self.connection is not None:
            self.connection.close()
        self.connection = None
        self.error_code = code
        self.error_type = type(exc).__name__ if exc is not None else None

    def __enter__(self) -> ReadOnlyDatabase:
        if not self.path or not os.path.isfile(self.path):
            self._fail("database_missing")
            return self
        uri = f"file:{quote(self.path, safe='/')}?mode=ro"
        try:
            self.connection = self.connector(uri, uri=True)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only=ON")
            # Force SQLite to parse the schema now so corrupt/non-SQLite files
            # cannot masquerade as empty projects through has_table().
            self.connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        except (OSError, sqlite3.Error) as exc:
            self._fail("database_unreadable", exc)
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.connection is not None:
            self.connection.close()

    def has_table(self, table: str) -> bool:
        cursor = self._execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
        row = cursor.fetchone() if cursor is not None else None
        return bool(row)

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor | None:
        if self.connection is None:
            return None
        try:
            return self.connection.execute(sql, params)
        except sqlite3.Error as exc:
            self._fail("database_read_failed", exc)
            return None

    def read_status(self) -> dict[str, Any]:
        return {
            "available": self.connection is not None and self.error_code is None,
            "error_code": self.error_code,
            "error_type": self.error_type,
        }

    def count(self, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
        if not self.has_table(table):
            return 0
        clause = f" WHERE {where}" if where else ""
        cursor = self._execute(f"SELECT COUNT(*) AS n FROM {table}{clause}", params)
        row = cursor.fetchone() if cursor is not None else None
        return int(row["n"] if row else 0)

    def rows(self, table: str, columns: str, where: str = "",
             params: tuple[Any, ...] = (), order_by: str = "") -> list[dict[str, Any]]:
        if not self.has_table(table):
            return []
        clause = f" WHERE {where}" if where else ""
        order = f" ORDER BY {order_by}" if order_by else ""
        cursor = self._execute(f"SELECT {columns} FROM {table}{clause}{order}", params)
        return [dict(row) for row in cursor.fetchall()] if cursor is not None else []

    def status_counts(self, table: str, column: str = "status",
                      where: str = "", params: tuple[Any, ...] = ()) -> dict[str, int]:
        if not self.has_table(table):
            return {}
        clause = f" WHERE {where}" if where else ""
        cursor = self._execute(
            f"SELECT COALESCE({column}, '') AS value, COUNT(*) AS n "
            f"FROM {table}{clause} GROUP BY COALESCE({column}, '') ORDER BY value", params
        )
        rows = cursor.fetchall() if cursor is not None else []
        return {str(row["value"]): int(row["n"]) for row in rows}

    def max_value(self, table: str, column: str) -> float | None:
        if not self.has_table(table):
            return None
        cursor = self._execute(f"SELECT MAX({column}) AS value FROM {table}")
        row = cursor.fetchone() if cursor is not None else None
        return float(row["value"]) if row and row["value"] is not None else None

    def meta(self, key: str, default: Any) -> Any:
        if not self.has_table("meta"):
            return default
        cursor = self._execute("SELECT value FROM meta WHERE key=?", (key,))
        row = cursor.fetchone() if cursor is not None else None
        return _decode_json(row["value"], default) if row else default


def _task_graph(project_configs: Mapping[str, Mapping[str, Any]], target_project: str,
                limit: int) -> dict[str, Any]:
    tasks_by_project: dict[str, list[dict[str, Any]]] = {}
    owners_by_task: dict[str, list[str]] = {}
    deliverable_links: list[dict[str, Any]] = []
    unavailable_projects: list[dict[str, Any]] = []
    for project_id in sorted(project_configs):
        db_path = str((project_configs.get(project_id) or {}).get("db") or "")
        with ReadOnlyDatabase(db_path) as db:
            rows = db.rows("tasks", "task_id, depends_on", order_by="task_id")
            tasks_by_project[project_id] = rows
            for row in rows:
                owners_by_task.setdefault(str(row.get("task_id") or "").upper(), []).append(project_id)
            for row in db.rows(
                    "deliverable_task_links",
                    "deliverable_id, board_id, milestone_id, project_id, task_id, role, blocks_deliverable",
                    order_by="deliverable_id, project_id, task_id"):
                deliverable_links.append({"home_project": project_id, **row})
            read_status = db.read_status()
            if not read_status["available"]:
                unavailable_projects.append({"project_id": project_id, **read_status})

    inbound: list[dict[str, Any]] = []
    outbound: list[dict[str, Any]] = []
    target_task_ids = {str(row.get("task_id") or "").upper()
                       for row in tasks_by_project.get(target_project, [])}
    for source_project in sorted(tasks_by_project):
        for row in tasks_by_project[source_project]:
            source_task = str(row.get("task_id") or "").upper()
            for dependency in _depends_on(row.get("depends_on")):
                owners = sorted(owners_by_task.get(dependency, []))
                if source_project == target_project:
                    for owner in owners:
                        if owner != target_project:
                            outbound.append({
                                "kind": "task_dependency",
                                "source_project": target_project,
                                "source_task_id": source_task,
                                "target_project": owner,
                                "target_task_id": dependency,
                            })
                elif dependency in target_task_ids:
                    inbound.append({
                        "kind": "task_dependency",
                        "source_project": source_project,
                        "source_task_id": source_task,
                        "target_project": target_project,
                        "target_task_id": dependency,
                    })

    for link in deliverable_links:
        task_project = str(link.get("project_id") or "")
        home_project = str(link.get("home_project") or "")
        item = {
            "kind": "deliverable_task_link",
            "home_project": home_project,
            "deliverable_id": link.get("deliverable_id"),
            "task_project": task_project,
            "task_id": link.get("task_id"),
            "role": link.get("role"),
            "blocks_deliverable": bool(link.get("blocks_deliverable")),
        }
        if task_project == target_project and home_project != target_project:
            inbound.append(item)
        elif home_project == target_project and task_project != target_project:
            outbound.append(item)

    sort_key = lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
    inbound.sort(key=sort_key)
    outbound.sort(key=sort_key)
    return {
        "inbound": _bounded(inbound, limit),
        "outbound": _bounded(outbound, limit),
        "total": len(inbound) + len(outbound),
        "scan": {
            "complete": not unavailable_projects,
            "project_count": len(project_configs),
            "unavailable_project_count": len(unavailable_projects),
            "unavailable_projects": _bounded(unavailable_projects, limit),
        },
    }


def _storage(path: str) -> dict[str, Any]:
    resolved = os.path.abspath(os.path.expanduser(path)) if path else ""
    files = []
    for label, candidate in (("database", resolved), ("wal", resolved + "-wal"),
                             ("shm", resolved + "-shm")):
        if candidate and os.path.isfile(candidate):
            files.append({"kind": label, "bytes": int(os.path.getsize(candidate))})
    return {
        "database_file": Path(resolved).name if resolved else None,
        "database_exists": bool(resolved and os.path.isfile(resolved)),
        "database_bytes": next((item["bytes"] for item in files if item["kind"] == "database"), 0),
        "total_sqlite_bytes": sum(item["bytes"] for item in files),
        "files": files,
    }


class ProjectImpactRepository:
    """Collect one bounded project impact snapshot without mutating any database."""

    def collect(self, project_id: str, *, project_configs: Mapping[str, Mapping[str, Any]],
                registry_db_path: str, limit: int) -> dict[str, Any]:
        config = dict(project_configs.get(project_id) or {})
        db_path = str(config.get("db") or "")
        with ReadOnlyDatabase(db_path) as db:
            task_rows = db.rows(
                "tasks", "task_id, title, status, workstream_id, assignee, updated_at",
                order_by="task_id")
            nonterminal = [row for row in task_rows
                           if str(row.get("status") or "").strip().lower() not in TERMINAL_TASK_STATUSES]
            open_prs = db.rows(
                "task_git_state", "task_id, branch, head_sha, pr_number, pr_url, pushed_at, updated_at",
                "COALESCE(pr_url, '')<>'' AND COALESCE(merged_sha, '')='' "
                "AND COALESCE(in_main_content, 0)=0", order_by="task_id")
            active_claims = db.rows(
                "task_claims", "id, task_id, agent_id, claimed_at, expires_at",
                "LOWER(status)='active'", order_by="task_id, id")
            active_sessions = db.rows(
                "work_sessions",
                "work_session_id, task_id, agent_id, runtime, branch, repo_role, storage_mode, status, "
                "dirty_status, updated_at, expires_at",
                "LOWER(status) IN ('active','proposed')", order_by="task_id, work_session_id")
            boards = db.rows(
                "project_boards", "id, title, kind, status, updated_at", order_by="id")
            deliverables = db.rows(
                "deliverables", "id, board_id, title, status, updated_at", order_by="id")

            task_statuses = db.status_counts("tasks")
            claim_statuses = db.status_counts("task_claims")
            session_statuses = db.status_counts("work_sessions")
            board_statuses = db.status_counts("project_boards")
            deliverable_statuses = db.status_counts("deliverables")
            ci_statuses = db.status_counts("external_ci_runs")
            webhook_statuses = db.status_counts("webhook_inbox")
            inbox_statuses = db.status_counts("inbox")
            job_statuses = db.status_counts("background_job_runs")
            monitor_statuses = db.status_counts("coordination_monitors")

            principals = db.rows(
                "principals", "id, kind, project, scopes, created_at, revoked_at",
                "project=? OR project='*'", (project_id,), "id")
            active_principals = [row for row in principals if row.get("revoked_at") is None]
            active_token_principals = [{
                "id": row.get("id"),
                "kind": row.get("kind"),
                "project": row.get("project"),
                "scopes": sorted(_decode_json(row.get("scopes"), [])),
            } for row in active_principals]

            inbound_domains = sorted({str(item).strip().lower() for item in
                                      db.meta("comms_inbound_domains", []) if str(item).strip()})
            digest_recipients = db.meta("comms_digest_recipients", [])
            notify_recipients = db.meta("comms_notify_recipients", [])
            cadence = str(db.meta("comms_digest_cadence", "weekly") or "weekly")

            activity_candidates = [
                ("activity", db.max_value("activity", "created_at")),
                ("tasks", db.max_value("tasks", "updated_at")),
                ("claims", db.max_value("task_claims", "claimed_at")),
                ("work_sessions", db.max_value("work_sessions", "updated_at")),
                ("agent_messages", db.max_value("agent_messages", "sent_at")),
                ("background_jobs", db.max_value("background_job_runs", "updated_at")),
                ("webhooks", db.max_value("webhook_inbox", "updated_at")),
            ]
            activity_candidates = [(source, value) for source, value in activity_candidates
                                   if value is not None]
            activity_candidates.sort(key=lambda item: (-item[1], item[0]))
            last_activity = ({"source": activity_candidates[0][0],
                              "timestamp": activity_candidates[0][1]}
                             if activity_candidates else {"source": None, "timestamp": None})

            snapshot = {
                "tasks": {
                    "total": len(task_rows),
                    "status_counts": task_statuses,
                    "nonterminal": _bounded(nonterminal, limit),
                },
                "provenance": {
                    "records": db.count("task_git_state"),
                    "open_prs": _bounded(open_prs, limit),
                    "publication_evidence_count": db.count("publication_evidence"),
                },
                "coordination": {
                    "claim_status_counts": claim_statuses,
                    "active_claims": _bounded(active_claims, limit),
                    "work_session_status_counts": session_statuses,
                    "active_work_sessions": _bounded(active_sessions, limit),
                },
                "hosted_outcomes": {
                    "board_status_counts": board_statuses,
                    "boards": _bounded(boards, limit),
                    "active_board_count": sum(count for status, count in board_statuses.items()
                                              if status.lower() in ACTIVE_BOARD_STATUSES),
                    "deliverable_status_counts": deliverable_statuses,
                    "deliverables": _bounded(deliverables, limit),
                    "active_deliverable_count": sum(
                        count for status, count in deliverable_statuses.items()
                        if status.lower() not in TERMINAL_DELIVERABLE_STATUSES),
                },
                "repo_ci_webhooks": {
                    "external_ci_status_counts": ci_statuses,
                    "pending_external_ci_count": sum(
                        count for status, count in ci_statuses.items()
                        if status.lower() in PENDING_CI_STATUSES),
                    "webhook_status_counts": webhook_statuses,
                    "pending_webhook_count": sum(
                        count for status, count in webhook_statuses.items()
                        if status.lower() in PENDING_WEBHOOK_STATUSES),
                },
                "access": {
                    "principal_count": len(principals),
                    "active_token_principal_count": len(active_principals),
                    "revoked_token_principal_count": len(principals) - len(active_principals),
                    "active_token_principals": _bounded(active_token_principals, limit),
                },
                "communications": {
                    "inbound_domains": inbound_domains[:limit],
                    "inbound_domains_total": len(inbound_domains),
                    "inbound_domains_truncated": len(inbound_domains) > limit,
                    "digest_recipient_count": len(digest_recipients),
                    "notify_recipient_count": len(notify_recipients),
                    "digest_cadence": cadence,
                    "inbox_status_counts": inbox_statuses,
                    "pending_inbox_count": sum(
                        count for status, count in inbox_statuses.items()
                        if status.lower() not in {"done", "closed", "archived", "resolved"}),
                    "agent_message_count": db.count("agent_messages"),
                    "unacked_agent_message_count": db.count(
                        "agent_messages", "requires_ack=1 AND acked_at IS NULL"),
                },
                "automation": {
                    "background_job_status_counts": job_statuses,
                    "active_background_job_count": sum(
                        count for status, count in job_statuses.items()
                        if status.lower() in ACTIVE_JOB_STATUSES),
                    "monitor_status_counts": monitor_statuses,
                    "active_monitor_count": sum(
                        count for status, count in monitor_statuses.items()
                        if status.lower() in ACTIVE_MONITOR_STATUSES),
                },
                "activity": {"last": last_activity},
            }
            target_read_status = db.read_status()

        with ReadOnlyDatabase(registry_db_path) as registry:
            org_id = ""
            access_rows = registry.rows(
                "project_access", "project_id, org_id, owner_user_id, visibility, updated_at",
                "project_id=?", (project_id,), "project_id")
            if access_rows:
                org_id = str(access_rows[0].get("org_id") or "")
            memberships = registry.rows(
                "org_memberships", "org_id, user_id, role, created_at",
                "org_id=?", (org_id,), "role, user_id") if org_id else []
            grants = registry.rows(
                "project_role_grants",
                "project_id, subject_kind, subject_id, role, scopes, created_at, revoked_at",
                "project_id=?", (project_id,), "role, subject_kind, subject_id")
            active_grants = [row for row in grants if row.get("revoked_at") is None]
            role_counts: dict[str, int] = {}
            for row in active_grants:
                role = str(row.get("role") or "")
                role_counts[role] = role_counts.get(role, 0) + 1
            snapshot["access"].update({
                "org_id": org_id or None,
                "member_count": len(memberships),
                "membership_role_counts": {
                    role: sum(1 for row in memberships if str(row.get("role") or "") == role)
                    for role in sorted({str(row.get("role") or "") for row in memberships})
                },
                "active_project_role_grant_count": len(active_grants),
                "revoked_project_role_grant_count": len(grants) - len(active_grants),
                "project_role_counts": dict(sorted(role_counts.items())),
            })
            snapshot["access"]["registry_read"] = registry.read_status()

        snapshot["cross_project_links"] = _task_graph(project_configs, project_id, limit)
        snapshot["storage"] = {**_storage(db_path), "database_read": target_read_status}
        return snapshot


default_project_impact_repository = ProjectImpactRepository()
