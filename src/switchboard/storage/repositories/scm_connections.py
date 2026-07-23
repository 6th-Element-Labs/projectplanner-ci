"""Project-scoped SCM installation trust boundary (ACCESS-28)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from db.core import _registry_conn
from db.schema import init_project_registry


SCM_CONNECTION_SCHEMA = "switchboard.scm_connection.v1"
SCM_PREFLIGHT_SCHEMA = "switchboard.scm_authorization_preflight.v1"
ALLOWED_OPERATIONS = frozenset({"clone", "fetch", "read", "push", "create_pr", "merge"})
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SECRET_KEYS = frozenset({
    "token", "access_token", "installation_token", "private_key", "client_secret",
    "password", "secret",
})


class SCMConnectionError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "error_code": self.code, "message": self.message}


def _normalized_list(value: Any, *, lower: bool = True) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        raise SCMConnectionError("invalid_scm_connection", "allowlists and scopes must be lists")
    items = {str(item or "").strip() for item in value}
    items.discard("")
    return sorted(item.lower() if lower else item for item in items)


def _reject_secrets(payload: Mapping[str, Any]) -> None:
    offending = sorted(key for key in payload if str(key).lower() in _SECRET_KEYS)
    if offending:
        raise SCMConnectionError(
            "raw_scm_credential_rejected",
            "raw SCM credentials are not accepted; store only an opaque installation_ref",
        )


def _installation_reference(value: Any) -> str:
    reference = str(value or "").strip()
    lowered = reference.lower()
    token_like = (
        lowered.startswith(("ghs_", "ghp_", "github_pat_"))
        or "-----begin " in lowered
        or any(character.isspace() for character in reference)
    )
    if not reference or len(reference) > 512:
        raise SCMConnectionError("installation_ref_required", "opaque installation_ref is required")
    if token_like:
        raise SCMConnectionError(
            "raw_scm_credential_rejected",
            "installation_ref must be an opaque vault/provider reference, not credential material",
        )
    return reference


def _topology(project: str) -> dict[str, Any]:
    # Lazy import avoids making the storage package depend on the legacy facade at import time.
    import store
    return dict(store.get_project_repo_topology(project) or {})


class SCMConnectionRepository:
    def __init__(self, topology_provider: Callable[[str], Mapping[str, Any]] = _topology):
        self._topology_provider = topology_provider

    @staticmethod
    def _prepare() -> None:
        init_project_registry()

    def _validate_topology_allowlists(
            self, projects: list[str], repos: list[str], orgs: list[str]) -> None:
        for project in projects:
            topology = dict(self._topology_provider(project) or {})
            canonical = str(
                (((topology.get("roles") or {}).get("canonical") or {}).get("repo"))
                or ""
            ).strip().lower()
            canonical_org = canonical.split("/", 1)[0] if "/" in canonical else ""
            if not topology.get("valid") or not canonical:
                raise SCMConnectionError(
                    "canonical_repository_unavailable",
                    "each allowlisted project must have a valid canonical repository",
                    status_code=409,
                )
            if canonical not in repos or canonical_org not in orgs:
                raise SCMConnectionError(
                    "repository_not_authorized",
                    "each project's canonical repository and organization must be allowlisted",
                    status_code=403,
                )

    @staticmethod
    def _row(c: sqlite3.Connection, connection_id: str) -> sqlite3.Row:
        row = c.execute(
            "SELECT * FROM scm_connections WHERE connection_id=?", (connection_id,)
        ).fetchone()
        if not row or str(row["lifecycle_state"]) == "deleted":
            raise SCMConnectionError("scm_connection_not_found", "SCM connection was not found", status_code=404)
        return row

    @staticmethod
    def _public(row: Mapping[str, Any]) -> dict[str, Any]:
        source = dict(row)
        ref = str(source["installation_ref"])
        return {
            "schema": SCM_CONNECTION_SCHEMA,
            "connection_id": source["connection_id"],
            "provider": source["provider"],
            "installation_ref": ref,
            "installation_ref_fingerprint": hashlib.sha256(ref.encode()).hexdigest(),
            "installation_version": int(source["installation_version"]),
            "org_allowlist": json.loads(source["org_allowlist_json"]),
            "project_allowlist": json.loads(source["project_allowlist_json"]),
            "repository_allowlist": json.loads(source["repository_allowlist_json"]),
            "operation_scopes": json.loads(source["operation_scopes_json"]),
            "lifecycle_state": source["lifecycle_state"],
            "created_at": source["created_at"],
            "created_by": source["created_by"],
            "rotated_at": source["rotated_at"],
            "revoked_at": source["revoked_at"],
            "revocation_reason": source["revocation_reason"],
            "updated_at": source["updated_at"],
            "updated_by": source["updated_by"],
        }

    @classmethod
    def _require_project(cls, row: Mapping[str, Any], project: str) -> None:
        if project and str(project).strip().lower() not in cls._public(row)["project_allowlist"]:
            raise SCMConnectionError(
                "repository_not_authorized",
                "SCM connection is not authorized for this project",
                status_code=403,
            )

    @staticmethod
    def _event(c: sqlite3.Connection, connection_id: str, event_type: str, actor: str,
               *, project: str = "", repository: str = "", operation: str = "",
               reason_code: str = "", details: Mapping[str, Any] | None = None) -> None:
        safe_details = {
            key: value for key, value in dict(details or {}).items()
            if key in {"installation_version", "lifecycle_state"} and
            isinstance(value, (str, int, float, bool, type(None)))
        }
        c.execute(
            "INSERT INTO scm_connection_events(event_id,connection_id,event_type,actor,"
            "project_id,repository,operation,reason_code,details_json,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"scm-event-{uuid.uuid4().hex[:16]}", connection_id, event_type,
             actor or "system", project or None, repository or None, operation or None,
             reason_code or None, json.dumps(safe_details, sort_keys=True), time.time()),
        )

    def create(self, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        _reject_secrets(payload)
        provider = str(payload.get("provider") or "github_app").strip().lower()
        if provider != "github_app":
            raise SCMConnectionError("unsupported_scm_provider", "only github_app is supported")
        installation_ref = _installation_reference(payload.get("installation_ref"))
        orgs = _normalized_list(payload.get("org_allowlist") or [])
        projects = _normalized_list(payload.get("project_allowlist") or [])
        repos = _normalized_list(payload.get("repository_allowlist") or [])
        scopes = _normalized_list(payload.get("operation_scopes") or [])
        if not orgs or not projects or not repos or not scopes:
            raise SCMConnectionError("scm_allowlist_required", "org, project, repository, and operation allowlists are required")
        selected_project = str(payload.get("project") or "").strip().lower()
        if selected_project and selected_project not in projects:
            raise SCMConnectionError(
                "repository_not_authorized",
                "the administering project must be in the project allowlist",
                status_code=403,
            )
        if any(not _REPOSITORY.fullmatch(repo) for repo in repos):
            raise SCMConnectionError("invalid_repository", "repositories must use exact owner/name form")
        if any(scope not in ALLOWED_OPERATIONS for scope in scopes):
            raise SCMConnectionError("invalid_scm_operation_scope", "unsupported SCM operation scope")
        self._validate_topology_allowlists(projects, repos, orgs)
        connection_id = str(payload.get("connection_id") or f"scm-{uuid.uuid4().hex[:16]}")
        now = time.time()
        self._prepare()
        with _registry_conn() as c:
            c.execute(
                "INSERT INTO scm_connections(connection_id,provider,installation_ref,"
                "installation_version,org_allowlist_json,project_allowlist_json,"
                "repository_allowlist_json,operation_scopes_json,lifecycle_state,"
                "created_at,created_by,updated_at,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (connection_id, provider, installation_ref, 1, json.dumps(orgs),
                 json.dumps(projects), json.dumps(repos), json.dumps(scopes), "active",
                 now, actor, now, actor),
            )
            self._event(c, connection_id, "created", actor,
                        details={"installation_version": 1, "lifecycle_state": "active"})
            return self._public(self._row(c, connection_id))

    def list(self, *, project: str = "") -> list[dict[str, Any]]:
        self._prepare()
        with _registry_conn() as c:
            rows = c.execute(
                "SELECT * FROM scm_connections WHERE lifecycle_state!='deleted' ORDER BY updated_at DESC"
            ).fetchall()
            result = [self._public(row) for row in rows]
        return [item for item in result if not project or project.lower() in item["project_allowlist"]]

    def get(self, connection_id: str, *, include_events: bool = False) -> dict[str, Any]:
        self._prepare()
        with _registry_conn() as c:
            result = self._public(self._row(c, connection_id))
            if include_events:
                events = c.execute(
                    "SELECT event_id,event_type,actor,project_id,repository,operation,"
                    "reason_code,details_json,created_at FROM scm_connection_events "
                    "WHERE connection_id=? ORDER BY created_at,event_id", (connection_id,)
                ).fetchall()
                result["events"] = [
                    {**dict(event), "details": json.loads(event["details_json"])}
                    for event in events
                ]
                for event in result["events"]:
                    event.pop("details_json", None)
            return result

    def update(self, connection_id: str, payload: Mapping[str, Any], *, actor: str,
               project: str = "") -> dict[str, Any]:
        _reject_secrets(payload)
        allowed = {"org_allowlist", "project_allowlist", "repository_allowlist", "operation_scopes"}
        if not set(payload).issubset(allowed):
            raise SCMConnectionError("invalid_scm_connection_update", "only allowlists and operation scopes may be updated")
        self._prepare()
        with _registry_conn() as c:
            row = self._row(c, connection_id)
            self._require_project(row, project)
            current = self._public(row)
            merged = {key: payload.get(key, current[key]) for key in allowed}
            orgs = _normalized_list(merged["org_allowlist"])
            projects = _normalized_list(merged["project_allowlist"])
            repos = _normalized_list(merged["repository_allowlist"])
            scopes = _normalized_list(merged["operation_scopes"])
            if not orgs or not projects or not repos or not scopes:
                raise SCMConnectionError("scm_allowlist_required", "allowlists and scopes cannot be empty")
            if any(not _REPOSITORY.fullmatch(repo) for repo in repos):
                raise SCMConnectionError("invalid_repository", "repositories must use exact owner/name form")
            if any(scope not in ALLOWED_OPERATIONS for scope in scopes):
                raise SCMConnectionError("invalid_scm_operation_scope", "unsupported SCM operation scope")
            self._validate_topology_allowlists(projects, repos, orgs)
            c.execute(
                "UPDATE scm_connections SET org_allowlist_json=?,project_allowlist_json=?,"
                "repository_allowlist_json=?,operation_scopes_json=?,updated_at=?,updated_by=? "
                "WHERE connection_id=?",
                (json.dumps(orgs), json.dumps(projects), json.dumps(repos), json.dumps(scopes),
                 time.time(), actor, connection_id),
            )
            self._event(c, connection_id, "updated", actor)
            return self._public(self._row(c, connection_id))

    def rotate(self, connection_id: str, installation_ref: str, *, actor: str,
               project: str = "") -> dict[str, Any]:
        installation_ref = _installation_reference(installation_ref)
        self._prepare()
        with _registry_conn() as c:
            row = self._row(c, connection_id)
            self._require_project(row, project)
            if row["lifecycle_state"] != "active":
                raise SCMConnectionError("scm_connection_not_active", "only active SCM connections may rotate", status_code=409)
            version = int(row["installation_version"]) + 1
            now = time.time()
            c.execute(
                "UPDATE scm_connections SET installation_ref=?,installation_version=?,"
                "rotated_at=?,rotated_by=?,updated_at=?,updated_by=? WHERE connection_id=?",
                (installation_ref, version, now, actor, now, actor, connection_id),
            )
            self._event(c, connection_id, "rotated", actor, details={"installation_version": version})
            return self._public(self._row(c, connection_id))

    def revoke(self, connection_id: str, reason: str, *, actor: str,
               project: str = "") -> dict[str, Any]:
        if not str(reason or "").strip():
            raise SCMConnectionError("revocation_reason_required", "revocation reason is required")
        self._prepare()
        with _registry_conn() as c:
            row = self._row(c, connection_id)
            self._require_project(row, project)
            now = time.time()
            c.execute(
                "UPDATE scm_connections SET lifecycle_state='revoked',revoked_at=?,"
                "revoked_by=?,revocation_reason=?,updated_at=?,updated_by=? WHERE connection_id=?",
                (now, actor, reason.strip(), now, actor, connection_id),
            )
            self._event(c, connection_id, "revoked", actor, reason_code="scm_connection_revoked",
                        details={"lifecycle_state": "revoked"})
            return self._public(self._row(c, connection_id))

    def delete(self, connection_id: str, reason: str, *, actor: str,
               project: str = "") -> dict[str, Any]:
        if not str(reason or "").strip():
            raise SCMConnectionError("deletion_reason_required", "deletion reason is required")
        self._prepare()
        with _registry_conn() as c:
            row = self._row(c, connection_id)
            self._require_project(row, project)
            now = time.time()
            self._event(c, connection_id, "deleted", actor, reason_code="scm_connection_deleted",
                        details={"lifecycle_state": "deleted"})
            c.execute(
                "UPDATE scm_connections SET lifecycle_state='deleted',installation_ref='',"
                "deleted_at=?,deleted_by=?,deletion_reason=?,updated_at=?,updated_by=? "
                "WHERE connection_id=?", (now, actor, reason.strip(), now, actor, connection_id),
            )
        return {"schema": SCM_CONNECTION_SCHEMA, "connection_id": connection_id, "deleted": True}

    def preflight(self, connection_id: str, *, project: str, repository: str,
                  operation: str, actor: str = "system") -> dict[str, Any]:
        project_id = str(project or "").strip().lower()
        repo = str(repository or "").strip().lower()
        op = str(operation or "").strip().lower()
        self._prepare()
        with _registry_conn() as c:
            row = self._row(c, connection_id)
            public = self._public(row)
            topology = dict(self._topology_provider(project_id) or {})
            canonical = str((((topology.get("roles") or {}).get("canonical") or {}).get("repo")) or "").lower()
            repo_org = repo.split("/", 1)[0] if "/" in repo else ""
            allowed = (
                public["lifecycle_state"] == "active"
                and project_id in public["project_allowlist"]
                and repo in public["repository_allowlist"]
                and repo_org in public["org_allowlist"]
                and op in public["operation_scopes"]
                and bool(topology.get("valid"))
                and repo == canonical
            )
            reason = "" if allowed else "repository_not_authorized"
            self._event(c, connection_id, "preflight_allowed" if allowed else "preflight_denied",
                        actor, project=project_id, repository=repo, operation=op,
                        reason_code=reason)
            result = {
                "schema": SCM_PREFLIGHT_SCHEMA,
                "allowed": allowed,
                "connection_id": connection_id,
                "project": project_id,
                "repository": repo,
                "operation": op,
                "installation_ref": public["installation_ref"] if allowed else "",
                "installation_version": public["installation_version"],
            }
            if not allowed:
                result.update({
                    "error": reason,
                    "error_code": reason,
                    "message": "repository is not authorized for this project and SCM connection",
                })
            return result


default_scm_connection_repository = SCMConnectionRepository()

__all__ = [
    "ALLOWED_OPERATIONS", "SCM_CONNECTION_SCHEMA", "SCM_PREFLIGHT_SCHEMA",
    "SCMConnectionError", "SCMConnectionRepository", "default_scm_connection_repository",
]
