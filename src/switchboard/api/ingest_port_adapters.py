"""Production adapters for the standalone Ingest boundary."""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Callable, Mapping

from fastapi import Request

import inbox_store
from db.connection import _conn
from switchboard.api import deps as api_deps
from switchboard.services.ingest import deps
from switchboard.services.ingest.ports import IngestAuthPort, IngestPort


def _canonical_hash(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(body), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RepositoryIngest(IngestPort):
    def __init__(self, executor: Callable[..., dict[str, Any]] | None = None):
        self._executor = executor

    def list_inbox(self, project: str, status: str | None = None) -> dict[str, Any]:
        return {"items": inbox_store.list_inbox(status, project=project),
                "pending": inbox_store.inbox_pending_count(project=project)}

    def intake(self, project: str, body: Mapping[str, Any], idem_key: str) -> dict[str, Any]:
        if not idem_key:
            raise ValueError("Idempotency-Key required")
        request_hash = _canonical_hash(body)
        now = time.time()
        with _conn(project) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT request_hash,status,response_json,error FROM ingest_operations WHERE idem_key=?",
                (idem_key,),
            ).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise ValueError("Idempotency-Key reused with a different request body")
                if row["status"] == "succeeded":
                    return json.loads(row["response_json"])
                if row["status"] == "failed":
                    raise RuntimeError(row["error"] or "prior intake attempt failed")
                raise ValueError("intake operation already in progress")
            c.execute(
                "INSERT INTO ingest_operations(idem_key,request_hash,status,created_at,updated_at) VALUES (?,?,?,?,?)",
                (idem_key, request_hash, "running", now, now),
            )
        try:
            kind = str(body.get("kind") or "note")
            title = str(body.get("title") or "")
            executor = self._executor
            if executor is None:
                import intake
                executor = intake.ingest_and_triage
            result = executor(kind, title, str(body.get("text") or ""), project=project)
            if result and (result.get("proposals") or result.get("new_tasks")):
                triage = {key: result.get(key, [] if key != "summary" else "")
                          for key in ("proposals", "new_tasks", "sources", "summary")}
                result["inbox_id"] = inbox_store.add_inbox_item(
                    kind, f"intake:{idem_key}", "", title or kind,
                    result.get("summary", ""), triage, project=project)
            payload = json.dumps(result, sort_keys=True)
            with _conn(project) as c:
                c.execute("UPDATE ingest_operations SET status='succeeded',response_json=?,updated_at=? WHERE idem_key=?",
                          (payload, time.time(), idem_key))
            return result
        except Exception as exc:
            with _conn(project) as c:
                c.execute("UPDATE ingest_operations SET status='failed',error=?,updated_at=? WHERE idem_key=?",
                          (f"{type(exc).__name__}: {exc}", time.time(), idem_key))
            raise RuntimeError(f"intake error: {exc}") from exc


class ProjectScopedIngestAuth(IngestAuthPort):
    def __init__(self, resolver: Callable[..., dict[str, Any]] | None = None):
        self._resolver = resolver or api_deps.resolve_principal

    def authorize(self, request: Request, project: str, scopes: tuple[str, ...]) -> dict[str, Any]:
        return dict(self._resolver(request, project, scopes, dev_actor="ingest"))


def configure_ingest_ports(*, ingest: IngestPort | None = None,
                           auth: IngestAuthPort | None = None) -> None:
    deps.configure(ingest=ingest or RepositoryIngest(), auth=auth or ProjectScopedIngestAuth())


def probe_ingest_readiness() -> dict[str, Any]:
    project = (os.environ.get("SWITCHBOARD_INGEST_READY_PROJECT") or "switchboard").strip()
    checks: dict[str, str] = {}
    try:
        with _conn(project) as c:
            c.execute("SELECT 1 FROM inbox LIMIT 1")
        checks["database_schema"] = "ok"
    except Exception as exc:
        checks["database_schema"] = type(exc).__name__
    try:
        with _conn(project) as c:
            c.execute("SELECT 1 FROM ingest_operations LIMIT 1")
        checks["operation_ledger"] = "ok"
    except Exception as exc:
        checks["operation_ledger"] = type(exc).__name__
    return {"ok": all(value == "ok" for value in checks.values()), "checks": checks}
