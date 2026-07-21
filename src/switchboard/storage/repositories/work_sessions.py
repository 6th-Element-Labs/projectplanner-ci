"""Work session + session-health repository (ARCH-MS-46).

Owns work_session CRUD/health, managed workspaces, claim-binding validation,
and session_health helpers previously living in ``store.py`` /
``repositories/shell.py``. Cross-cutting store helpers (write queue, idempotency,
repo_preflight, activity) are reached via ``_store_facade()`` during the
strangler. ``store.py`` / ``shell.py`` re-export these symbols; root
``work_sessions_store.py`` is a compatibility shim.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from constants import *  # noqa: F401,F403
from db.connection import _conn
from db.core import *  # noqa: F401,F403
from switchboard.storage.repositories.access import (  # noqa: F401
    has_project,
    normalize_project_id,
)
from switchboard.storage.repositories.claims import _active_task_claims_in  # noqa: F401
from switchboard.storage.repositories.provenance import _load_git_state  # noqa: F401
from switchboard.storage.repositories.tasks import (  # noqa: F401
    _task_looks_like_code_work,
    get_task,
)
from switchboard.storage.repositories.activity import append_activity  # noqa: F401 — ARCH-MS-55

# Kept next to work-session helpers (moved with ARCH-MS-46 verbatim surface).
PR_BACKED_STATUSES = frozenset({"In Review", "Done"})
PR_ACTIVE_SESSION_STATUSES = frozenset({"proposed", "active", "completed"})


def _store_facade():
    """Resolve transitional store helpers after store.py is initialized."""
    import store
    return store


def check_resources(*args, **kwargs):
    return _store_facade().check_resources(*args, **kwargs)


def claim_resources(*args, **kwargs):
    return _store_facade().claim_resources(*args, **kwargs)


def release_resource_lease(*args, **kwargs):
    return _store_facade().release_resource_lease(*args, **kwargs)


def get_project_repo_topology(*args, **kwargs):
    return _store_facade().get_project_repo_topology(*args, **kwargs)


def get_session_policy_profiles(*args, **kwargs):
    return _store_facade().get_session_policy_profiles(*args, **kwargs)


def _normalize_session_policy_profile(*args, **kwargs):
    return _store_facade()._normalize_session_policy_profile(*args, **kwargs)


def _project_session_policy_defaults(*args, **kwargs):
    return _store_facade()._project_session_policy_defaults(*args, **kwargs)


def _session_policy_profile_rules(*args, **kwargs):
    return _store_facade()._session_policy_profile_rules(*args, **kwargs)


def _session_profile_text(*args, **kwargs):
    return _store_facade()._session_profile_text(*args, **kwargs)


def _merge_gate_bool(*args, **kwargs):
    return _store_facade()._merge_gate_bool(*args, **kwargs)


def _project_env_suffix(*args, **kwargs):
    return _store_facade()._project_env_suffix(*args, **kwargs)


def _repo_git(*args, **kwargs):
    return _store_facade()._repo_git(*args, **kwargs)


def _repo_remote_slug(*args, **kwargs):
    return _store_facade()._repo_remote_slug(*args, **kwargs)


from switchboard.domain.bug_intake.policy import FAIL_FIX_FAILURE_CLASSES  # ARCH-MS-59



def _session_health_summary(session: Dict[str, Any]) -> Dict[str, Any]:
    health = session.get("health") or {}
    workspace = health.get("workspace") or {}
    return {
        "work_session_id": session.get("work_session_id"),
        "agent_id": session.get("agent_id"),
        "claim_id": session.get("claim_id"),
        "status": session.get("status"),
        "health_status": health.get("status"),
        "safe": health.get("safe"),
        "storage_mode": session.get("storage_mode"),
        "repo_role": session.get("repo_role"),
        "branch": session.get("branch") or workspace.get("branch"),
        "workspace_path": workspace.get("path"),
        "head_sha": session.get("head_sha") or workspace.get("head_sha"),
        "finding_count": health.get("finding_count", 0),
        "blocking_count": health.get("blocking_count", 0),
        "recommended_repair": health.get("recommended_repair"),
        "updated_at": session.get("updated_at"),
        "expires_at": session.get("expires_at"),
    }


def _task_session_health_in(c: sqlite3.Connection, task: Dict[str, Any],
                            project: str = DEFAULT_PROJECT,
                            active_claims: Optional[List[Dict[str, Any]]] = None,
                            git_state: Optional[Dict[str, Any]] = None,
                            now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    task_id = task.get("task_id") or ""
    rows = c.execute(
        "SELECT * FROM work_sessions WHERE task_id=? "
        "ORDER BY updated_at DESC, work_session_id",
        (task_id,),
    ).fetchall()
    sessions = [_work_session_row(row) for row in rows]
    claims = active_claims if active_claims is not None else _active_task_claims_in(c, task_id)
    active_claim_ids = {
        str(claim.get("claim_id") or claim.get("id") or "")
        for claim in (claims or [])
        if claim.get("claim_id") or claim.get("id")
    }
    nonterminal = [
        s for s in sessions
        if s.get("status") not in {"completed", "archived", "expired"}
    ]
    if active_claim_ids:
        # One task may have a long history of failed/expired attempts.  Once a
        # current claim exists, only its bound Work Session is delivery health;
        # superseded attempts remain queryable history, not 20 duplicate blockers.
        current_sessions = [
            s for s in nonterminal
            if str(s.get("claim_id") or "") in active_claim_ids
        ]
    else:
        # A task cannot legitimately have several simultaneous unclaimed Work
        # Sessions. Keep at most the newest orphan as a bounded cleanup signal.
        current_sessions = nonterminal[:1]
    active_sessions = [
        s for s in current_sessions
        if s.get("status") == "active" and (
            not s.get("expires_at") or float(s.get("expires_at") or 0) > now
        )
    ]
    unsafe = [s for s in current_sessions if (s.get("health") or {}).get("status") == "unsafe"]
    warnings = [s for s in current_sessions if (s.get("health") or {}).get("status") == "warning"]
    findings: List[Dict[str, Any]] = []
    for session in unsafe + warnings:
        health = session.get("health") or {}
        for finding in health.get("findings") or []:
            findings.append({
                "code": finding.get("code"),
                "kind": "unsafe_session" if finding.get("blocking") else "session_warning",
                "work_session_id": session.get("work_session_id"),
                "agent_id": session.get("agent_id"),
                "task_id": task_id,
                "message": finding.get("message"),
                "failure_class": finding.get("failure_class"),
                "severity": finding.get("severity"),
                "blocking": bool(finding.get("blocking")),
                "repair": finding.get("repair"),
            })

    session_claim_ids = {s.get("claim_id") for s in sessions if s.get("claim_id")}
    for claim in claims or []:
        claim_id = claim.get("claim_id")
        if claim_id and claim_id not in session_claim_ids:
            findings.append({
                "code": "active_claim_without_work_session",
                "kind": "session_warning",
                "claim_id": claim_id,
                "agent_id": claim.get("agent_id"),
                "task_id": task_id,
                "message": "Active task claim is not bound to a Work Session.",
                "failure_class": "missing_data",
                "severity": "medium",
                "blocking": False,
                "repair": "Create/bind a Work Session for code work, or use a non-code policy profile.",
            })

    blocking = [f for f in findings if f.get("blocking")]
    nonblocking = [f for f in findings if not f.get("blocking")]
    status = (
        "unsafe" if blocking else
        "warning" if nonblocking else
        "healthy" if sessions else
        "no_sessions"
    )
    gs = git_state if git_state is not None else _load_git_state(c, task_id)
    return {
        "schema": TASK_SESSION_HEALTH_SCHEMA,
        "project_id": project,
        "task_id": task_id,
        "status": status,
        "safe": not blocking,
        "blocking": bool(blocking),
        "session_count": len(sessions),
        "current_session_count": len(current_sessions),
        "active_session_count": len(active_sessions),
        "unsafe_session_count": len(unsafe),
        "warning_session_count": len(warnings),
        "active_claim_count": len(claims or []),
        "findings": findings,
        "active_sessions": [_session_health_summary(s) for s in active_sessions],
        "latest_sessions": [_session_health_summary(s) for s in sessions[:5]],
        "pr_url": (gs or {}).get("pr_url"),
        "branch": (gs or {}).get("branch"),
        "head_sha": (gs or {}).get("head_sha"),
        "recommended_repair": (
            (blocking or nonblocking)[0].get("repair") if (blocking or nonblocking) else
            "No repair needed." if sessions else
            "No Work Session is bound yet."
        ),
        "checked_at": now,
    }


def work_session_contract(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    topology = get_project_repo_topology(project)
    roles = sorted((topology.get("roles") or {}).keys())
    policy_profiles = get_session_policy_profiles(project)
    return {
        "schema": WORK_SESSION_SCHEMA,
        "project": project,
        "purpose": (
            "Bind agent code work to an explicit project, repo role, branch, workspace path, "
            "hygiene state, and lifecycle before claim/complete/merge gates enforce it."
        ),
        "lifecycle_states": sorted(WORK_SESSION_STATUSES),
        "storage_modes": sorted(WORK_SESSION_STORAGE_MODES),
        "dirty_statuses": sorted(WORK_SESSION_DIRTY_STATUSES),
        "repo_roles": roles,
        "policy_profiles": policy_profiles,
        "required_for_modes": {
            "worktree": ["worktree_path"],
            "clone": ["clone_path"],
            "external": [],
        },
        "managed_workspace": {
            "schema": MANAGED_WORK_SESSION_SCHEMA,
            "storage_modes": ["worktree", "clone"],
            "default_workspace_root": _managed_workspace_root(project, {}),
            "archive_tool": "archive_work_session_workspace",
        },
        "health": {
            "session_schema": WORK_SESSION_HEALTH_SCHEMA,
            "task_schema": TASK_SESSION_HEALTH_SCHEMA,
            "tools": ["get_work_session_health", "list_session_health"],
            "rest": [
                "GET /ixp/v1/work_sessions/{work_session_id}/health",
                "GET /ixp/v1/session_health",
            ],
            "blocking_statuses": ["unsafe"],
            "warning_statuses": ["warning", "no_sessions"],
        },
        "fail_closed_rules": [
            "unknown project ids are rejected",
            "task_id must exist when supplied",
            "repo_role must exist in the project repo_topology",
            "storage_mode, status, and dirty_status must be recognized values",
            "worktree and clone sessions require their matching path",
            "managed workspace paths must stay inside workspace_root",
            "JSON fields must decode to their expected object/list shapes",
            "session_token is never stored raw; only session_token_hash may persist",
        ],
        "audit_events": [
            "work_session.created",
            "work_session.updated",
            "work_session.completed",
            "work_session.expired",
            "work_session.managed_created",
            "work_session.workspace_archived",
        ],
    }


def _work_session_json(value: Any, default: Any, expected: type, field: str) -> Tuple[Any, str]:
    if value in (None, ""):
        return copy.deepcopy(default), ""
    if isinstance(value, expected):
        return copy.deepcopy(value), ""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return copy.deepcopy(default), f"{field} must be valid JSON"
        if isinstance(parsed, expected):
            return parsed, ""
    expected_name = "array" if expected is list else "object"
    return copy.deepcopy(default), f"{field} must be a JSON {expected_name}"


def _work_session_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["hygiene"] = _json_obj(d.pop("hygiene_json", "{}"), {})
    d["file_leases"] = _json_obj(d.pop("file_leases_json", "[]"), [])
    d["resource_leases"] = _json_obj(d.pop("resource_leases_json", "[]"), [])
    d["env"] = _json_obj(d.pop("env_json", "{}"), {})
    d["schema"] = WORK_SESSION_SCHEMA
    d["session_token_hash_present"] = bool(d.pop("session_token_hash", None))
    d["health"] = _work_session_health(d)
    return d


def _session_health_finding(code: str, message: str, failure_class: str,
                            severity: str = "high", blocking: bool = True,
                            repair: str = "", **details: Any) -> Dict[str, Any]:
    detail = FAIL_FIX_FAILURE_CLASSES.get(failure_class) or {}
    return {
        "code": code,
        "message": message,
        "failure_class": failure_class,
        "severity": severity,
        "blocking": bool(blocking),
        "expected_signal": detail.get("expected_signal"),
        "repair": repair or None,
        **details,
    }


def _work_session_health(session: Dict[str, Any],
                         now: Optional[float] = None) -> Dict[str, Any]:
    """Summarize whether a Work Session is safe for humans/coordinators to trust."""
    now = time.time() if now is None else now
    findings: List[Dict[str, Any]] = []
    session = dict(session or {})
    work_session_id = session.get("work_session_id")
    status = (session.get("status") or "").strip().lower()
    dirty = (session.get("dirty_status") or "unknown").strip().lower()
    path = session.get("worktree_path") or session.get("clone_path") or ""
    preflight = ((session.get("hygiene") or {}).get("repo_preflight") or {})

    if status == "active" and session.get("expires_at") and float(session.get("expires_at") or 0) < now:
        findings.append(_session_health_finding(
            "expired_active_session",
            "Work Session is active but its lease/expiry timestamp is in the past.",
            "stale_branch",
            repair="Renew the session/claim or archive the abandoned workspace.",
            work_session_id=work_session_id,
        ))
    if status in {"blocked", "expired"}:
        findings.append(_session_health_finding(
            f"{status}_work_session",
            f"Work Session status is {status}.",
            "failed_gate",
            repair="Repair the recorded blocker or create a fresh managed Work Session.",
            work_session_id=work_session_id,
        ))
    if status == "active" and not path:
        findings.append(_session_health_finding(
            "work_session_missing_path",
            "Active Work Session has no worktree_path or clone_path.",
            "missing_data",
            repair="Bind the session to a workspace path or create a managed Work Session.",
            work_session_id=work_session_id,
        ))
    if dirty == "dirty":
        findings.append(_session_health_finding(
            "dirty_work_session",
            "Work Session reports a dirty worktree.",
            "failed_gate",
            repair="Commit, stash, or clean the workspace, then rerun preflight.",
            work_session_id=work_session_id,
        ))
    elif dirty == "unknown" and status == "active":
        findings.append(_session_health_finding(
            "unknown_dirty_status",
            "Active Work Session has not recorded a clean/dirty verdict.",
            "missing_data",
            severity="medium",
            blocking=False,
            repair="Run preflight_work_session to refresh repo hygiene.",
            work_session_id=work_session_id,
        ))
    conflict_count = int(session.get("conflict_marker_count") or 0)
    if conflict_count > 0:
        findings.append(_session_health_finding(
            "conflict_markers",
            f"Work Session reports {conflict_count} file(s) with conflict markers.",
            "failed_gate",
            repair="Resolve conflict markers, rerun tests, then rerun preflight.",
            work_session_id=work_session_id,
            conflict_marker_count=conflict_count,
        ))

    if preflight:
        verdict = (preflight.get("verdict") or "").strip().lower()
        # BUG-115: a host-local worktree awaiting its Agent Host git attestation is
        # not a failed gate -- the server deliberately refuses to stat a path that
        # lives on the remote host. Keep it a visible, non-blocking signal so the
        # direct-CLI Work Session stays active until the heartbeat attests it.
        pending_host_attestation = (
            bool(preflight.get("pending"))
            or preflight.get("source") == "agent_host_pending"
        )
        if pending_host_attestation:
            findings.append(_session_health_finding(
                "work_session_preflight_pending",
                "Host-local worktree is awaiting an Agent Host git attestation; "
                "the server did not stat the remote path.",
                "missing_data",
                severity="medium",
                blocking=False,
                repair="Wait for the Agent Host heartbeat to attach its signed repo "
                       "attestation (BUG-97), then rerun preflight_work_session.",
                work_session_id=work_session_id,
                preflight_verdict=verdict or None,
            ))
        elif verdict == "deny" or preflight.get("ok") is False:
            findings.append(_session_health_finding(
                "work_session_preflight_failed",
                "Recorded repo preflight is not clean.",
                "failed_gate",
                repair="Repair the preflight findings and rerun preflight_work_session.",
                work_session_id=work_session_id,
                preflight_verdict=verdict or None,
            ))
        elif verdict == "warn":
            findings.append(_session_health_finding(
                "work_session_preflight_warn",
                "Recorded repo preflight has warnings.",
                "failed_gate",
                severity="medium",
                blocking=False,
                repair="Review warnings before merge or completion.",
                work_session_id=work_session_id,
                preflight_verdict=verdict,
            ))
        for finding in preflight.get("findings") or []:
            code = str(finding.get("code") or "preflight_finding")
            blocking = bool(finding.get("blocking", True))
            severity = str(finding.get("severity") or ("high" if blocking else "medium"))
            failure_class = (
                "stale_branch" if code == "stale_base"
                else "failed_gate" if blocking
                else "missing_data"
            )
            findings.append(_session_health_finding(
                code,
                str(finding.get("message") or code),
                failure_class,
                severity=severity,
                blocking=blocking,
                repair="Repair the repo preflight finding and rerun preflight_work_session.",
                work_session_id=work_session_id,
                preflight_finding=finding,
            ))
    elif status == "active":
        findings.append(_session_health_finding(
            "missing_work_session_preflight",
            "Active Work Session has no recorded repo preflight.",
            "missing_data",
            severity="medium",
            blocking=False,
            repair="Run preflight_work_session before completion or merge.",
            work_session_id=work_session_id,
        ))

    # Deduplicate when a failed preflight finding mirrors explicit dirty/conflict state.
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for finding in findings:
        key = (finding.get("code"), finding.get("work_session_id"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    blocking = [f for f in deduped if f.get("blocking")]
    warnings = [f for f in deduped if not f.get("blocking")]
    status_value = "unsafe" if blocking else ("warning" if warnings else "healthy")
    return {
        "schema": WORK_SESSION_HEALTH_SCHEMA,
        "work_session_id": work_session_id,
        "project_id": session.get("project_id"),
        "task_id": session.get("task_id"),
        "agent_id": session.get("agent_id"),
        "status": status_value,
        "safe": not blocking,
        "blocking": bool(blocking),
        "finding_count": len(deduped),
        "blocking_count": len(blocking),
        "warning_count": len(warnings),
        "findings": deduped,
        "workspace": {
            "storage_mode": session.get("storage_mode"),
            "path": path or None,
            "branch": session.get("branch"),
            "repo_role": session.get("repo_role"),
            "repo": session.get("repo"),
            "head_sha": session.get("head_sha"),
            "base_sha": session.get("base_sha"),
            "upstream": session.get("upstream"),
        },
        "recommended_repair": (
            (blocking or warnings)[0].get("repair") if (blocking or warnings) else
            "No repair needed."
        ),
        "checked_at": now,
    }


def _validate_work_session_payload(payload: Dict[str, Any], project: str,
                                   partial: bool = False) -> Tuple[Dict[str, Any], List[str]]:
    errors: List[str] = []
    if not has_project(project):
        return {}, [f"unknown project: {project}"]

    topology = get_project_repo_topology(project)
    roles = topology.get("roles") or {}
    normalized: Dict[str, Any] = {}

    def text(key: str, default: str = "") -> str:
        if partial and key not in payload:
            return ""
        return str(payload.get(key, default) or "").strip()

    repo_role = text("repo_role", "canonical")
    if not partial or "repo_role" in payload:
        if repo_role not in roles:
            errors.append("repo_role must exist in repo_topology.roles")
        normalized["repo_role"] = repo_role
        role = roles.get(repo_role) or {}
        normalized["repo"] = text("repo", role.get("repo") or "")
        normalized["default_branch"] = text("default_branch", role.get("default_branch") or "")

    for key in (
        "work_session_id", "task_id", "claim_id", "agent_id", "runtime", "branch", "upstream",
        "base_sha", "head_sha", "worktree_path", "clone_path", "policy_profile",
        "principal_id",
    ):
        if not partial or key in payload:
            normalized[key] = text(key)

    if not partial or "agent_id" in payload:
        if not normalized.get("agent_id"):
            errors.append("agent_id required")

    task_id = normalized.get("task_id") if (not partial or "task_id" in payload) else None
    if task_id:
        task = get_task(task_id, project=project)
        if not task:
            errors.append("task_id must exist in project")

    claim_id = normalized.get("claim_id") if (not partial or "claim_id" in payload) else None
    if claim_id:
        with _conn(project) as c:
            row = c.execute("SELECT task_id, agent_id FROM task_claims WHERE id=?",
                            (claim_id,)).fetchone()
        if not row:
            errors.append("claim_id must exist in project")
        else:
            if task_id and row["task_id"] != task_id:
                errors.append("claim_id must belong to task_id")
            agent = normalized.get("agent_id")
            if agent and row["agent_id"] != agent:
                errors.append("claim_id must belong to agent_id")

    if not partial or "storage_mode" in payload:
        storage_mode = text("storage_mode", "worktree").lower()
        if storage_mode not in WORK_SESSION_STORAGE_MODES:
            errors.append("storage_mode must be one of: " + ", ".join(sorted(WORK_SESSION_STORAGE_MODES)))
        normalized["storage_mode"] = storage_mode
    else:
        storage_mode = ""

    if not partial or "status" in payload:
        status = text("status", "active").lower()
        if status not in WORK_SESSION_STATUSES:
            errors.append("status must be one of: " + ", ".join(sorted(WORK_SESSION_STATUSES)))
        normalized["status"] = status

    if not partial or "dirty_status" in payload:
        dirty_status = text("dirty_status", "unknown").lower()
        if dirty_status not in WORK_SESSION_DIRTY_STATUSES:
            errors.append("dirty_status must be one of: " + ", ".join(sorted(WORK_SESSION_DIRTY_STATUSES)))
        normalized["dirty_status"] = dirty_status

    mode_for_path = storage_mode or text("storage_mode")
    if not partial or mode_for_path in WORK_SESSION_REQUIRED_PATH_MODES:
        if mode_for_path == "worktree" and not (normalized.get("worktree_path") or text("worktree_path")):
            errors.append("worktree_path required when storage_mode=worktree")
        if mode_for_path == "clone" and not (normalized.get("clone_path") or text("clone_path")):
            errors.append("clone_path required when storage_mode=clone")

    if not partial or "conflict_marker_count" in payload:
        raw_count = payload.get("conflict_marker_count", 0)
        try:
            count = int(raw_count or 0)
            if count < 0:
                raise ValueError
            normalized["conflict_marker_count"] = count
        except (TypeError, ValueError):
            errors.append("conflict_marker_count must be a non-negative integer")

    json_specs = [
        ("hygiene", "hygiene_json", {}, dict),
        ("file_leases", "file_leases_json", [], list),
        ("resource_leases", "resource_leases_json", [], list),
        ("env", "env_json", {}, dict),
    ]
    for public_key, stored_key, default, expected in json_specs:
        if partial and public_key not in payload and stored_key not in payload:
            continue
        value = payload.get(public_key, payload.get(stored_key))
        parsed, err = _work_session_json(value, default, expected, public_key)
        if err:
            errors.append(err)
        normalized[stored_key] = json.dumps(parsed, sort_keys=True)

    if not partial or "expires_at" in payload:
        raw_expires = payload.get("expires_at")
        if raw_expires in (None, ""):
            normalized["expires_at"] = None
        else:
            try:
                normalized["expires_at"] = float(raw_expires)
            except (TypeError, ValueError):
                errors.append("expires_at must be a unix timestamp")

    token = str(payload.get("session_token") or "").strip()
    token_hash = str(payload.get("session_token_hash") or "").strip()
    if token:
        token_hash = hash_token(token)
    if token_hash:
        normalized["session_token_hash"] = token_hash

    return normalized, errors


def create_work_session(payload: Dict[str, Any], actor: str = "system",
                        principal_id: str = "", project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    data, errors = _validate_work_session_payload(payload or {}, project, partial=False)
    if errors:
        return {"error": "invalid_work_session", "errors": errors,
                "contract": work_session_contract(project) if has_project(project) else None}
    with _conn(project) as c:
        return _insert_work_session_in(c, data, actor=actor,
                                       principal_id=principal_id, project=project)


def _insert_work_session_in(c: sqlite3.Connection, data: Dict[str, Any],
                            actor: str = "system", principal_id: str = "",
                            project: str = DEFAULT_PROJECT,
                            now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    data = dict(data or {})
    work_session_id = data.get("work_session_id") or f"worksession-{uuid.uuid4().hex[:16]}"
    data["work_session_id"] = work_session_id
    data["project_id"] = project
    data["principal_id"] = principal_id or data.get("principal_id") or ""
    data["created_by"] = actor
    data["updated_by"] = actor
    data["created_at"] = now
    data["updated_at"] = now
    if data.get("status") == "completed":
        data["completed_at"] = now
    else:
        data["completed_at"] = None
    columns = [
        "work_session_id", "project_id", "task_id", "claim_id", "agent_id", "runtime",
        "repo_role", "repo", "default_branch", "branch", "upstream", "base_sha", "head_sha",
        "worktree_path", "clone_path", "storage_mode", "status", "dirty_status",
        "conflict_marker_count", "hygiene_json", "file_leases_json", "resource_leases_json",
        "env_json", "policy_profile", "session_token_hash", "principal_id", "created_by",
        "updated_by", "created_at", "updated_at", "expires_at", "completed_at",
    ]
    existing = c.execute("SELECT 1 FROM work_sessions WHERE work_session_id=?",
                         (work_session_id,)).fetchone()
    if existing:
        return {"error": "duplicate_work_session", "work_session_id": work_session_id}
    c.execute(
        f"INSERT INTO work_sessions({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        [data.get(col) for col in columns],
    )
    event = {
        "work_session_id": work_session_id,
        "agent_id": data.get("agent_id"),
        "repo_role": data.get("repo_role"),
        "branch": data.get("branch"),
        "storage_mode": data.get("storage_mode"),
        "status": data.get("status"),
    }
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (data.get("task_id") or None, actor, "work_session.created",
               json.dumps(event, sort_keys=True), now))
    row = c.execute("SELECT * FROM work_sessions WHERE work_session_id=?",
                    (work_session_id,)).fetchone()
    return {"created": True, "work_session": _work_session_row(row)}


def get_work_session(work_session_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    if not has_project(project):
        return None
    with _conn(project) as c:
        row = c.execute("SELECT * FROM work_sessions WHERE work_session_id=?",
                        ((work_session_id or "").strip(),)).fetchone()
    return _work_session_row(row) if row else None


def issue_work_session_mcp_token(
        work_session_id: str, *, actor: str = "agent-host",
        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Rotate and return a one-time MCP bearer for one active Work Session."""
    now = time.time()
    token = "wst-" + uuid.uuid4().hex
    with _conn(project) as c:
        row = c.execute(
            "SELECT task_id, claim_id, agent_id, status, expires_at "
            "FROM work_sessions WHERE work_session_id=?",
            ((work_session_id or "").strip(),),
        ).fetchone()
        if not row:
            return {"error": "work_session_not_found",
                    "work_session_id": work_session_id}
        expires_at = float(row["expires_at"] or (now + 2 * 60 * 60))
        if row["status"] != "active" or expires_at <= now:
            return {"error": "work_session_not_active",
                    "work_session_id": work_session_id}
        c.execute(
            "UPDATE work_sessions SET session_token_hash=?, expires_at=?, "
            "updated_at=?, updated_by=? "
            "WHERE work_session_id=? AND status='active'",
            (hash_token(token), expires_at, now, actor, work_session_id),
        )
        c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            (row["task_id"], actor, "work_session.mcp_token_issued",
             json.dumps({
                 "work_session_id": work_session_id,
                 "claim_id": row["claim_id"],
                 "agent_id": row["agent_id"],
                 "token_returned_once": True,
                 "credential_values_redacted": True,
             }, sort_keys=True), now),
        )
    return {
        "issued": True,
        "work_session_id": work_session_id,
        "task_id": row["task_id"],
        "agent_id": row["agent_id"],
        "expires_at": expires_at,
        "token": token,
        "token_returned_once": True,
    }


def get_principal_by_work_session_token_any_project(
        token: str) -> Optional[Dict[str, Any]]:
    """Resolve an active Work Session bearer as a read-only MCP principal."""
    if not str(token or "").startswith("wst-"):
        return None
    digest = hash_token(token)
    now = time.time()
    for project in _store_facade().project_ids():
        try:
            with _conn(project, read_snapshot=True) as c:
                row = c.execute(
                    "SELECT work_session_id, task_id, claim_id, agent_id, expires_at "
                    "FROM work_sessions WHERE session_token_hash=? AND status='active' "
                    "AND expires_at>?",
                    (digest, now),
                ).fetchone()
        except sqlite3.OperationalError:
            # A newly registered project may not have had its schema initialized
            # yet; token resolution must continue to the remaining boards.
            continue
        if row:
            return {
                "id": f"work-session:{row['work_session_id']}",
                "kind": "work_session",
                "display_name": row["agent_id"] or row["work_session_id"],
                "project": project,
                "scopes": ["read"],
                "work_session_id": row["work_session_id"],
                "bound_task_id": row["task_id"],
                "claim_id": row["claim_id"],
                "session_expires_at": float(row["expires_at"]),
            }
    return None


def list_work_sessions(project: str = DEFAULT_PROJECT, task_id: str = "",
                       agent_id: str = "", status: str = "",
                       repo_role: str = "", include_expired: bool = True) -> List[Dict[str, Any]]:
    if not has_project(project):
        return []
    where = ["1=1"]
    params: List[Any] = []
    if task_id:
        where.append("task_id=?")
        params.append(task_id.strip().upper())
    if agent_id:
        where.append("agent_id=?")
        params.append(agent_id.strip())
    if status:
        where.append("status=?")
        params.append(status.strip().lower())
    if repo_role:
        where.append("repo_role=?")
        params.append(repo_role.strip())
    if not include_expired:
        now = time.time()
        where.append("(expires_at IS NULL OR expires_at>?)")
        params.append(now)
    with _conn(project) as c:
        rows = c.execute(
            "SELECT * FROM work_sessions WHERE " + " AND ".join(where) +
            " ORDER BY updated_at DESC, work_session_id",
            params,
        ).fetchall()
    return [_work_session_row(row) for row in rows]


def pr_backed_by_process(task: Dict[str, Any], project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """The one definition of 'is this task backed by board process' (ADR-0006).

    Answers the single question both gates ask — the SESSION-12 claim gate (enforced at
    the CI chokepoint) and merge_gate (cooperative, the agent asks). A task is backed if
    it is already In Review/Done, holds an active claim, carries git provenance, or has an
    active Work Session. Returns {backed, signal}. Each gate layers its own stricter
    requirements (merge_gate: canonical session hygiene + tests) on top of this base.
    """
    task = task or {}
    status = task.get("status") or ""
    if status in PR_BACKED_STATUSES:
        return {"backed": True, "signal": "status", "detail": status}
    if task.get("active_claims"):
        return {"backed": True, "signal": "active_claim"}
    git_state = task.get("git_state") or {}
    if git_state.get("merged_sha") or git_state.get("pr_number"):
        return {"backed": True, "signal": "git_provenance"}
    task_id = task.get("task_id") or ""
    try:
        sessions = list_work_sessions(project, task_id=task_id, include_expired=False)
    except Exception:
        sessions = []
    if any((s.get("status") or "") in PR_ACTIVE_SESSION_STATUSES for s in sessions):
        return {"backed": True, "signal": "work_session"}
    return {"backed": False, "signal": None}


def get_work_session_health(work_session_id: str,
                            project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    session = get_work_session(work_session_id, project=project)
    if not session:
        return None
    return session.get("health") or _work_session_health(session)


def list_session_health(project: str = DEFAULT_PROJECT, task_id: str = "",
                        agent_id: str = "", status: str = "",
                        only_unsafe: bool = False) -> Dict[str, Any]:
    sessions = list_work_sessions(
        project=project, task_id=task_id, agent_id=agent_id, status=status)
    health_rows = [s.get("health") or _work_session_health(s) for s in sessions]
    if only_unsafe:
        health_rows = [h for h in health_rows if h.get("status") == "unsafe"]
    task_health = None
    if task_id:
        task = get_task(task_id, project=project)
        task_health = (task or {}).get("session_health")
    return {
        "schema": "switchboard.session_health_list.v1",
        "project_id": project,
        "task_id": (task_id or "").strip().upper() or None,
        "agent_id": (agent_id or "").strip() or None,
        "only_unsafe": bool(only_unsafe),
        "count": len(health_rows),
        "unsafe_count": sum(1 for h in health_rows if h.get("status") == "unsafe"),
        "warning_count": sum(1 for h in health_rows if h.get("status") == "warning"),
        "task_session_health": task_health,
        "session_health": health_rows,
    }


def update_work_session(work_session_id: str, payload: Dict[str, Any], actor: str = "system",
                        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    work_session_id = (work_session_id or "").strip()
    existing = get_work_session(work_session_id, project=project)
    if not existing:
        return {"error": "work_session_not_found", "work_session_id": work_session_id}
    data, errors = _validate_work_session_payload(payload or {}, project, partial=True)
    if errors:
        return {"error": "invalid_work_session", "errors": errors,
                "contract": work_session_contract(project)}
    if not data:
        return {"updated": False, "work_session": existing}
    now = time.time()
    data["updated_at"] = now
    data["updated_by"] = actor
    status = data.get("status")
    if status == "completed" and not existing.get("completed_at"):
        data["completed_at"] = now
    sets = [f"{key}=?" for key in data]
    vals = list(data.values()) + [work_session_id]
    event_kind = "work_session.completed" if status == "completed" else "work_session.updated"
    if status == "expired":
        event_kind = "work_session.expired"
    with _conn(project) as c:
        c.execute(f"UPDATE work_sessions SET {', '.join(sets)} WHERE work_session_id=?", vals)
        event = {
            "work_session_id": work_session_id,
            "updated_fields": sorted(data.keys()),
            "status": status or existing.get("status"),
        }
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (existing.get("task_id") or None, actor, event_kind,
                   json.dumps(event, sort_keys=True), now))
        row = c.execute("SELECT * FROM work_sessions WHERE work_session_id=?",
                        (work_session_id,)).fetchone()
    return {"updated": True, "work_session": _work_session_row(row)}


def _managed_workspace_error(code: str, message: str, failure_class: str = "failed_gate",
                             **details: Any) -> Dict[str, Any]:
    return {
        "schema": MANAGED_WORK_SESSION_SCHEMA,
        "created": False,
        "error": code,
        "message": message,
        "failure_class": failure_class,
        **details,
    }


def _managed_workspace_git(repo_path: str, args: List[str],
                           timeout_seconds: int = 60) -> Dict[str, Any]:
    return _repo_git(repo_path, args, timeout_seconds=timeout_seconds)


def _managed_workspace_slug(value: str, fallback: str = "work") -> str:
    slug = normalize_project_id(value or "")
    return (slug or fallback)[:48].strip("-_") or fallback


def _managed_workspace_branch(task: Dict[str, Any], agent_id: str,
                              requested: str = "") -> str:
    branch = (requested or "").strip()
    if branch:
        return branch
    runtime = (agent_id or "agent").split("/", 1)[0].strip() or "agent"
    task_id = task.get("task_id") or "TASK"
    slug = _managed_workspace_slug(task.get("title") or task_id)
    return f"{runtime}/{task_id}-{slug}"


def _managed_workspace_root(project: str, payload: Dict[str, Any]) -> str:
    configured = str(payload.get("workspace_root") or "").strip()
    if configured:
        return configured
    suffix = _project_env_suffix(project)
    return (
        os.environ.get(f"PM_WORKSPACE_ROOT_{suffix}") if suffix else ""
    ) or os.environ.get("PM_WORKSPACE_ROOT") or "/var/lib/projectplanner/workspaces"


def _managed_workspace_source_path(project: str, payload: Dict[str, Any]) -> str:
    configured = str(payload.get("source_path") or payload.get("repo_path") or "").strip()
    if configured:
        return configured
    suffix = _project_env_suffix(project)
    env = (
        os.environ.get(f"PM_REPO_PATH_{suffix}") if suffix else ""
    ) or os.environ.get("PM_REPO_PATH")
    if env:
        return env
    # work_sessions.py lives under src/switchboard/storage/repositories/ → repo root.
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    )


def _managed_workspace_path(root: str, project: str, task_id: str, branch: str,
                            requested: str = "") -> Tuple[str, str]:
    root_abs = os.path.abspath(os.path.expanduser(root))
    if requested:
        path_abs = os.path.abspath(os.path.expanduser(requested))
    else:
        safe_branch = branch.replace("/", "__")
        path_abs = os.path.join(root_abs, project, task_id.lower(), safe_branch)
    try:
        common = os.path.commonpath([root_abs, path_abs])
    except ValueError:
        common = ""
    if common != root_abs:
        return path_abs, "workspace_path must be inside workspace_root"
    return path_abs, ""


def _managed_role(project: str, repo_role: str) -> Tuple[Dict[str, Any], str]:
    topology = get_project_repo_topology(project)
    role = (topology.get("roles") or {}).get(repo_role) or {}
    if not role:
        return {}, "repo_role must exist in repo_topology.roles"
    if repo_role == "canonical" and not (topology.get("code_repo_gate") or {}).get("passed"):
        return role, "canonical repo is not configured"
    if not (role.get("repo") or "").strip():
        return role, f"repo_topology.roles.{repo_role}.repo is not configured"
    return role, ""


def _managed_verify_source_repo(source_path: str, project: str,
                                repo_role: str) -> Tuple[Dict[str, Any], str]:
    source_path = os.path.abspath(os.path.expanduser(source_path))
    inside = _repo_git(source_path, ["rev-parse", "--is-inside-work-tree"])
    if not inside.get("ok") or inside.get("stdout") != "true":
        return {}, "source_path is not a git worktree"
    top = _repo_git(source_path, ["rev-parse", "--show-toplevel"])
    repo_path = os.path.abspath(top.get("stdout") or source_path)
    remote = _repo_git(repo_path, ["remote", "get-url", "origin"])
    remote_url = remote.get("stdout") if remote.get("ok") else ""
    expected_repo = (((get_project_repo_topology(project).get("roles") or {})
                      .get(repo_role) or {}).get("repo") or "").strip()
    actual_slug = _repo_remote_slug(remote_url)
    expected_slug = _repo_remote_slug(expected_repo)
    if actual_slug and expected_slug and actual_slug.lower() != expected_slug.lower():
        return {
            "repo_path": repo_path,
            "remote": {"url": remote_url, "repo": actual_slug},
            "expected_repo": expected_repo,
        }, "source_path origin does not match project repo topology"
    return {
        "repo_path": repo_path,
        "remote": {"url": remote_url, "repo": actual_slug},
        "expected_repo": expected_repo,
    }, ""


def _managed_prepare_worktree(source_path: str, workspace_path: str, branch: str,
                              base_ref: str, fetch: bool) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    steps: List[Dict[str, Any]] = []
    if fetch and base_ref.startswith("origin/"):
        fetched = _managed_workspace_git(source_path, ["fetch", "origin", base_ref.split("/", 1)[1]])
        steps.append({"cmd": "git fetch", "ok": fetched.get("ok"), "stderr": fetched.get("stderr")})
        if not fetched.get("ok"):
            return fetched, steps
    base = _managed_workspace_git(source_path, ["rev-parse", f"{base_ref}^{{commit}}"])
    steps.append({"cmd": "git rev-parse base", "ok": base.get("ok"), "stderr": base.get("stderr")})
    if not base.get("ok"):
        return base, steps
    existing_branch = _managed_workspace_git(
        source_path, ["rev-parse", "--verify", f"refs/heads/{branch}"])
    if existing_branch.get("ok"):
        return {"ok": False, "stderr": "branch already exists", "code": "branch_exists"}, steps
    added = _managed_workspace_git(
        source_path, ["worktree", "add", "-b", branch, workspace_path, base_ref],
        timeout_seconds=120,
    )
    steps.append({"cmd": "git worktree add", "ok": added.get("ok"), "stderr": added.get("stderr")})
    if not added.get("ok"):
        return added, steps
    if base_ref.startswith("origin/"):
        upstream = _managed_workspace_git(
            workspace_path, ["branch", "--set-upstream-to", base_ref, branch])
        steps.append({"cmd": "git branch --set-upstream-to", "ok": upstream.get("ok"),
                      "stderr": upstream.get("stderr")})
    return {"ok": True, "base_sha": base.get("stdout")}, steps


def _managed_prepare_clone(source_path: str, workspace_path: str, branch: str,
                           base_ref: str, fetch: bool,
                           role_repo: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    steps: List[Dict[str, Any]] = []
    clone_source = source_path or (f"https://github.com/{role_repo}.git" if role_repo else "")
    if not clone_source:
        return {"ok": False, "stderr": "clone source unavailable"}, steps
    parent = os.path.dirname(workspace_path)
    os.makedirs(parent, exist_ok=True)
    cloned = subprocess.run(
        ["git", "clone", clone_source, workspace_path],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    clone_result = {"ok": cloned.returncode == 0, "stdout": (cloned.stdout or "").strip(),
                    "stderr": (cloned.stderr or "").strip(), "returncode": cloned.returncode}
    steps.append({"cmd": "git clone", "ok": clone_result.get("ok"),
                  "stderr": clone_result.get("stderr")})
    if not clone_result.get("ok"):
        return clone_result, steps
    if fetch and base_ref.startswith("origin/"):
        fetched = _managed_workspace_git(workspace_path, ["fetch", "origin", base_ref.split("/", 1)[1]])
        steps.append({"cmd": "git fetch", "ok": fetched.get("ok"), "stderr": fetched.get("stderr")})
        if not fetched.get("ok"):
            return fetched, steps
    base = _managed_workspace_git(workspace_path, ["rev-parse", f"{base_ref}^{{commit}}"])
    steps.append({"cmd": "git rev-parse base", "ok": base.get("ok"), "stderr": base.get("stderr")})
    if not base.get("ok"):
        return base, steps
    checked = _managed_workspace_git(workspace_path, ["checkout", "-b", branch, base_ref])
    steps.append({"cmd": "git checkout -b", "ok": checked.get("ok"), "stderr": checked.get("stderr")})
    if not checked.get("ok"):
        return checked, steps
    if base_ref.startswith("origin/"):
        upstream = _managed_workspace_git(
            workspace_path, ["branch", "--set-upstream-to", base_ref, branch])
        steps.append({"cmd": "git branch --set-upstream-to", "ok": upstream.get("ok"),
                      "stderr": upstream.get("stderr")})
    return {"ok": True, "base_sha": base.get("stdout")}, steps


def create_managed_work_session(payload: Dict[str, Any], actor: str = "system",
                                principal_id: str = "",
                                project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Create a git-backed isolated workspace and persist it as a Work Session."""
    payload = dict(payload or {})
    if not has_project(project):
        return _managed_workspace_error("unknown_project", f"Unknown project: {project}",
                                        "invalid_input")
    task_id = str(payload.get("task_id") or "").strip().upper()
    agent_id = str(payload.get("agent_id") or "").strip()
    if not task_id:
        return _managed_workspace_error("task_id_required", "task_id is required.",
                                        "missing_data")
    if not agent_id:
        return _managed_workspace_error("agent_id_required", "agent_id is required.",
                                        "missing_data")
    task = get_task(task_id, project=project)
    if not task:
        return _managed_workspace_error("task_not_found", "task_id must exist in project.",
                                        "invalid_input", task_id=task_id)
    repo_role = str(payload.get("repo_role") or "canonical").strip()
    role, role_error = _managed_role(project, repo_role)
    if role_error:
        return _managed_workspace_error("repo_role_unavailable", role_error,
                                        "missing_data", repo_role=repo_role)
    storage_mode = str(payload.get("storage_mode") or role.get("workspace_mode")
                       or payload.get("default_mode") or "worktree").strip().lower()
    if storage_mode not in {"worktree", "clone"}:
        return _managed_workspace_error(
            "managed_storage_mode_not_allowed",
            "Managed workspace creation supports storage_mode=worktree or clone.",
            "invalid_input",
            storage_mode=storage_mode,
        )
    source_path = _managed_workspace_source_path(project, payload)
    source_info, source_error = _managed_verify_source_repo(source_path, project, repo_role)
    if source_error:
        return _managed_workspace_error("source_repo_invalid", source_error,
                                        "wrong_repo", source_path=source_path,
                                        source=source_info)
    source_path = source_info.get("repo_path") or os.path.abspath(source_path)
    branch = _managed_workspace_branch(task, agent_id, str(payload.get("branch") or ""))
    workspace_root = _managed_workspace_root(project, payload)
    requested_path = str(payload.get("workspace_path") or payload.get("worktree_path")
                         or payload.get("clone_path") or "").strip()
    workspace_path, path_error = _managed_workspace_path(
        workspace_root, project, task_id, branch, requested_path)
    if path_error:
        return _managed_workspace_error("workspace_path_invalid", path_error,
                                        "invalid_input", workspace_path=workspace_path,
                                        workspace_root=os.path.abspath(workspace_root))
    if os.path.exists(workspace_path):
        return _managed_workspace_error("workspace_path_exists",
                                        "Managed workspace path already exists.",
                                        "failed_gate",
                                        workspace_path=workspace_path)
    conflicts = check_resources("worktree", [workspace_path], project=project)
    if conflicts:
        return _managed_workspace_error("workspace_path_leased",
                                        "Managed workspace path is already leased.",
                                        "failed_gate",
                                        workspace_path=workspace_path,
                                        conflicts=conflicts)
    default_branch = str(role.get("default_branch") or "master").strip() or "master"
    base_ref = str(payload.get("base_ref") or f"origin/{default_branch}").strip()
    fetch = _merge_gate_bool(payload.get("fetch"), default=True)
    os.makedirs(os.path.dirname(workspace_path), exist_ok=True)
    if storage_mode == "worktree":
        prepared, steps = _managed_prepare_worktree(
            source_path, workspace_path, branch, base_ref, bool(fetch))
    else:
        prepared, steps = _managed_prepare_clone(
            source_path, workspace_path, branch, base_ref, bool(fetch), role.get("repo") or "")
    if not prepared.get("ok"):
        if os.path.exists(workspace_path):
            shutil.rmtree(workspace_path, ignore_errors=True)
        code = prepared.get("code") or "workspace_create_failed"
        return _managed_workspace_error(
            code,
            "Git workspace creation failed.",
            "failed_gate",
            git_error=prepared,
            git_steps=steps,
        )
    ttl_seconds = int(payload.get("ttl_seconds") or payload.get("ttl_s") or 7200)
    session_token = "wst-" + uuid.uuid4().hex
    env = dict(payload.get("env") or {})
    env.update({
        "managed_workspace": True,
        "workspace_root": os.path.abspath(workspace_root),
        "workspace_namespace": _managed_workspace_slug(f"{project}-{task_id}"),
        "port_namespace": 10000 + (int(hashlib.sha1(f"{project}:{task_id}".encode()).hexdigest()[:4], 16) % 50000),
        "source_path": source_path,
        "base_ref": base_ref,
        "git_steps": steps,
    })
    lease = claim_resources(
        agent_id=agent_id,
        resource_type="worktree",
        names=[workspace_path],
        task_id=task_id,
        ttl_seconds=ttl_seconds,
        principal_id=principal_id,
        actor=actor,
        idem_key=str(payload.get("idem_key") or f"managed-workspace:{project}:{task_id}:{agent_id}:{workspace_path}"),
        project=project,
    )
    if lease.get("conflict") or lease.get("error"):
        if storage_mode == "worktree":
            _managed_workspace_git(source_path, ["worktree", "remove", "--force", workspace_path])
        else:
            shutil.rmtree(workspace_path, ignore_errors=True)
        return _managed_workspace_error("workspace_lease_failed",
                                        "Could not claim managed workspace lease.",
                                        "failed_gate", lease=lease)
    preflight = _store_facade().repo_preflight(
        workspace_path,
        project=project,
        task_id=task_id,
        agent_id=agent_id,
        repo_role=repo_role,
        expected_branch=branch,
        expected_base_ref=base_ref,
    )
    session_payload = {
        "task_id": task_id,
        "agent_id": agent_id,
        "runtime": payload.get("runtime") or agent_id.split("/", 1)[0],
        "repo_role": repo_role,
        "branch": branch,
        "upstream": preflight.get("upstream") or base_ref,
        "base_sha": preflight.get("base_sha") or prepared.get("base_sha") or "",
        "head_sha": preflight.get("head_sha") or prepared.get("base_sha") or "",
        "worktree_path": workspace_path if storage_mode == "worktree" else "",
        "clone_path": workspace_path if storage_mode == "clone" else "",
        "storage_mode": storage_mode,
        "status": "active",
        "dirty_status": "dirty" if preflight.get("dirty") else "clean",
        "conflict_marker_count": int(preflight.get("conflict_marker_count") or 0),
        "hygiene": {"repo_preflight": preflight, "managed": {"schema": MANAGED_WORK_SESSION_SCHEMA}},
        "resource_leases": [lease],
        "env": env,
        "policy_profile": payload.get("policy_profile") or "code_strict",
        "expires_at": time.time() + max(1, ttl_seconds),
        "session_token": session_token,
    }
    created = create_work_session(
        session_payload, actor=actor, principal_id=principal_id, project=project)
    if created.get("error"):
        release_resource_lease(lease.get("lease_id") or "", actor=actor, project=project)
        if storage_mode == "worktree":
            _managed_workspace_git(source_path, ["worktree", "remove", "--force", workspace_path])
        else:
            shutil.rmtree(workspace_path, ignore_errors=True)
        return _managed_workspace_error("work_session_create_failed",
                                        "Managed workspace was created but Work Session persist failed.",
                                        "failed_gate", result=created)
    try:
        from switchboard.storage.repositories.preflight_runs import record_preflight_run
        recorded = record_preflight_run(
            preflight,
            work_session_id=created["work_session"]["work_session_id"],
            actor=actor,
            source="managed_create",
            project=project,
        )
        if isinstance(recorded, dict) and recorded.get("run"):
            hygiene = dict(created["work_session"].get("hygiene") or {})
            hygiene["repo_preflight"] = preflight
            hygiene["preflight_run_id"] = recorded["run"].get("run_id")
            update_work_session(
                created["work_session"]["work_session_id"],
                {"hygiene": hygiene},
                actor=actor, project=project,
            )
            created["work_session"] = get_work_session(
                created["work_session"]["work_session_id"], project=project) or created["work_session"]
    except Exception:  # noqa: BLE001
        recorded = {"recorded": False}
    append_activity(
        "work_session.managed_created",
        actor,
        {
            "schema": MANAGED_WORK_SESSION_SCHEMA,
            "work_session_id": created["work_session"]["work_session_id"],
            "task_id": task_id,
            "agent_id": agent_id,
            "storage_mode": storage_mode,
            "workspace_path": workspace_path,
            "branch": branch,
            "base_ref": base_ref,
            "lease_id": lease.get("lease_id"),
            "preflight_run_id": (recorded.get("run") or {}).get("run_id") if isinstance(recorded, dict) else None,
        },
        task_id=task_id,
        project=project,
    )
    return {
        "schema": MANAGED_WORK_SESSION_SCHEMA,
        "created": True,
        "managed": True,
        "project": project,
        "task_id": task_id,
        "agent_id": agent_id,
        "storage_mode": storage_mode,
        "branch": branch,
        "workspace_path": workspace_path,
        "base_ref": base_ref,
        "lease": lease,
        "session_token": session_token,
        "work_session": created["work_session"],
        "preflight_run": recorded if isinstance(recorded, dict) else {"recorded": False},
    }


def archive_work_session_workspace(work_session_id: str, remove_workspace: bool = False,
                                   actor: str = "system",
                                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    session = get_work_session(work_session_id, project=project)
    if not session:
        return {"error": "work_session_not_found", "work_session_id": work_session_id}
    env = session.get("env") or {}
    workspace_root = os.path.abspath(os.path.expanduser(str(env.get("workspace_root") or "")))
    workspace_path = os.path.abspath(os.path.expanduser(
        session.get("worktree_path") or session.get("clone_path") or ""))
    if remove_workspace:
        if not env.get("managed_workspace"):
            return {"error": "not_managed_workspace", "work_session_id": work_session_id}
        if not workspace_root or not workspace_path:
            return {"error": "managed_workspace_path_missing",
                    "work_session_id": work_session_id}
        try:
            common = os.path.commonpath([workspace_root, workspace_path])
        except ValueError:
            common = ""
        if common != workspace_root:
            return {"error": "workspace_path_outside_root",
                    "work_session_id": work_session_id,
                    "workspace_path": workspace_path,
                    "workspace_root": workspace_root}
        if os.path.exists(workspace_path):
            if session.get("storage_mode") == "worktree":
                source_path = str(env.get("source_path") or "").strip()
                removed = _managed_workspace_git(
                    source_path or workspace_path,
                    ["worktree", "remove", "--force", workspace_path],
                    timeout_seconds=120,
                )
                if not removed.get("ok") and os.path.exists(workspace_path):
                    return {"error": "workspace_remove_failed",
                            "work_session_id": work_session_id,
                            "git_error": removed}
            else:
                shutil.rmtree(workspace_path, ignore_errors=True)
    updated = update_work_session(
        work_session_id,
        {"status": "archived", "hygiene": {**(session.get("hygiene") or {}),
                                           "archived_workspace": {
                                               "removed_workspace": bool(remove_workspace),
                                               "workspace_path": workspace_path,
                                           }}},
        actor=actor,
        project=project,
    )
    append_activity(
        "work_session.workspace_archived",
        actor,
        {
            "work_session_id": work_session_id,
            "remove_workspace": bool(remove_workspace),
            "removed_workspace": bool(remove_workspace and not os.path.exists(workspace_path)),
            "workspace_path": workspace_path,
        },
        task_id=session.get("task_id") or None,
        project=project,
    )
    return {
        "archived": updated.get("updated") is True,
        "removed_workspace": bool(remove_workspace and not os.path.exists(workspace_path)),
        "work_session": updated.get("work_session"),
    }


def preflight_work_session(work_session_id: str, actor: str = "system",
                           project: str = DEFAULT_PROJECT,
                           expected_branch: str = "",
                           expected_base_ref: str = "") -> Dict[str, Any]:
    session = get_work_session(work_session_id, project=project)
    if not session:
        return {"error": "work_session_not_found", "work_session_id": work_session_id}
    worktree_path = session.get("worktree_path") or session.get("clone_path") or ""
    if not worktree_path:
        return {"error": "work_session_missing_path", "work_session_id": work_session_id}
    if os.path.isdir(os.path.abspath(os.path.expanduser(worktree_path))):
        report = _store_facade().repo_preflight(
            worktree_path,
            project=project,
            task_id=session.get("task_id") or "",
            agent_id=session.get("agent_id") or "",
            repo_role=session.get("repo_role") or "canonical",
            expected_branch=expected_branch or session.get("branch") or "",
            expected_base_ref=expected_base_ref,
        )
    else:
        report = _remote_host_preflight(
            session,
            project=project,
            expected_branch=expected_branch or session.get("branch") or "",
            expected_base_ref=expected_base_ref,
        )
        if report is None:
            # BUG-115: a direct-CLI session on an enrolled host creates its Work
            # Session and preflights it before the Agent Host heartbeat has had a
            # chance to attach the BUG-97 attestation. While a live host runner
            # owns this host-local worktree, refuse to stat a path that is
            # definitionally not on the coordinator's filesystem: return a visible,
            # non-blocking "awaiting host attestation" pending report so the session
            # stays active until the heartbeat attests it.
            report = _host_owned_pending_preflight(
                session,
                project=project,
                expected_branch=expected_branch or session.get("branch") or "",
                expected_base_ref=expected_base_ref,
            )
        if report is None:
            # Preserve the original fail-closed result when no fresh, exact host
            # attestation is bound and no live host runner owns the worktree.
            report = _store_facade().repo_preflight(
                worktree_path,
                project=project,
                task_id=session.get("task_id") or "",
                agent_id=session.get("agent_id") or "",
                repo_role=session.get("repo_role") or "canonical",
                expected_branch=expected_branch or session.get("branch") or "",
                expected_base_ref=expected_base_ref,
            )
    try:
        from switchboard.storage.repositories.preflight_runs import record_preflight_run
        recorded = record_preflight_run(
            report,
            work_session_id=work_session_id,
            claim_id=str(session.get("claim_id") or ""),
            actor=actor,
            source="preflight_work_session",
            project=project,
        )
    except Exception as exc:  # noqa: BLE001 — never hide preflight behind a write failure
        recorded = {
            "recorded": False,
            "error": "preflight_run_persist_failed",
            "message": str(exc),
        }
    hygiene = dict(session.get("hygiene") or {})
    hygiene["repo_preflight"] = report
    if isinstance(recorded, dict) and recorded.get("run"):
        hygiene["preflight_run_id"] = recorded["run"].get("run_id")
    updates = {
        "hygiene": hygiene,
        "dirty_status": "dirty" if report.get("dirty") else "clean",
        "conflict_marker_count": int(report.get("conflict_marker_count") or 0),
        "branch": report.get("branch") or session.get("branch") or "",
        "upstream": report.get("upstream") or session.get("upstream") or "",
        "base_sha": report.get("base_sha") or session.get("base_sha") or "",
        "head_sha": report.get("head_sha") or session.get("head_sha") or "",
    }
    updated = update_work_session(work_session_id, updates, actor=actor, project=project)
    return {
        "work_session_id": work_session_id,
        "project": project,
        "preflight": report,
        "preflight_run": recorded,
        "updated": updated,
    }


def _remote_host_preflight(session: Dict[str, Any], *, project: str,
                           expected_branch: str = "",
                           expected_base_ref: str = "") -> Optional[Dict[str, Any]]:
    """Validate a fresh Agent Host Git attestation for a host-local workspace."""
    now = time.time()
    task_id = str(session.get("task_id") or "").upper()
    agent_id = str(session.get("agent_id") or "")
    work_session_id = str(session.get("work_session_id") or "")
    with _conn(project) as c:
        rows = c.execute(
            "SELECT * FROM runner_sessions WHERE task_id=? AND agent_id=? "
            "AND status IN ('ready','running') ORDER BY heartbeat_at DESC LIMIT 8",
            (task_id, agent_id),
        ).fetchall()
    for row in rows:
        runner = dict(row)
        metadata = _json_obj(runner.get("metadata_json"), {})
        if str(metadata.get("work_session_id") or "") != work_session_id:
            continue
        attested = metadata.get("host_repo_preflight")
        if not isinstance(attested, dict):
            continue
        report = copy.deepcopy(attested)
        findings = list(report.get("findings") or [])
        ttl = max(10, int(runner.get("heartbeat_ttl_s") or 60))
        captured_at = float(report.get("captured_at") or 0)
        expected = {
            "host_id": str(runner.get("host_id") or ""),
            "runner_session_id": str(runner.get("runner_session_id") or ""),
            "work_session_id": work_session_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "repo_path": str(session.get("worktree_path") or session.get("clone_path") or ""),
            "branch": str(expected_branch or session.get("branch") or ""),
        }
        for field, value in expected.items():
            actual = str(report.get(field) or "")
            if field == "task_id":
                actual = actual.upper()
            if actual != value:
                findings.append({
                    "code": f"host_preflight_{field}_mismatch",
                    "message": f"Agent Host preflight {field} does not match the bound session.",
                    "failure_class": "failed_gate", "severity": "high", "blocking": True,
                    "details": {"expected": value, "actual": actual},
                })
        if (float(runner.get("heartbeat_at") or 0) + ttl <= now
                or captured_at <= 0 or captured_at + max(ttl, 180) <= now):
            findings.append({
                "code": "host_preflight_stale",
                "message": "Agent Host preflight attestation is stale.",
                "failure_class": "stale_branch", "severity": "high", "blocking": True,
            })
        head_sha = str(report.get("head_sha") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            findings.append({
                "code": "host_preflight_head_invalid",
                "message": "Agent Host preflight has no valid exact head SHA.",
                "failure_class": "missing_data", "severity": "high", "blocking": True,
            })
        topology = get_project_repo_topology(project)
        role = ((topology.get("roles") or {}).get(session.get("repo_role") or "canonical") or {})
        expected_repo = _repo_remote_slug(str(role.get("repo") or ""))
        actual_repo = _repo_remote_slug(str(report.get("origin_url") or ""))
        if expected_repo and actual_repo.lower() != expected_repo.lower():
            findings.append({
                "code": "host_preflight_repo_mismatch",
                "message": "Agent Host origin does not match the project repo role.",
                "failure_class": "wrong_repo", "severity": "high", "blocking": True,
                "details": {"expected": expected_repo, "actual": actual_repo},
            })
        blocking = any(bool(item.get("blocking", True)) for item in findings)
        report.update({
            "schema": "switchboard.repo_preflight.v1",
            "source": "agent_host_attestation",
            "project": project,
            "repo_role": session.get("repo_role") or "canonical",
            "expected_branch": expected_branch,
            "expected_base_ref": expected_base_ref,
            "findings": findings,
            "ok": not blocking,
            "verdict": "deny" if blocking else "pass",
            "validated_at": now,
        })
        return report
    return None


def _host_owned_pending_preflight(session: Dict[str, Any], *, project: str,
                                  expected_branch: str = "",
                                  expected_base_ref: str = "") -> Optional[Dict[str, Any]]:
    """Return a non-blocking pending report when a live host runner owns a
    host-local worktree that has not produced a BUG-97 attestation yet.

    Direct-CLI sessions (start_task -> direct_codex_session) create their Work
    Session and preflight it before the Agent Host heartbeat late-binds the
    ``work_session_id`` and attaches ``host_repo_preflight``. During that window
    ``_remote_host_preflight`` cannot find a fresh attestation, and the path lives
    only on the enrolled host. Rather than stat a path the coordinator can never
    see (a false ``worktree_missing`` deny that leaves the session born blocked),
    recognise that a live host runner owns the session and surface a visible,
    auditable pending signal. The heartbeat attestation upgrades it to a real pass
    on the next ``preflight_work_session`` run; completion/merge still require that
    pass, so nothing lands unverified.
    """
    now = time.time()
    task_id = str(session.get("task_id") or "").upper()
    agent_id = str(session.get("agent_id") or "")
    work_session_id = str(session.get("work_session_id") or "")
    principal_id = str(session.get("principal_id") or "")
    if not task_id or not agent_id:
        return None
    with _conn(project) as c:
        rows = c.execute(
            "SELECT * FROM runner_sessions WHERE task_id=? AND agent_id=? "
            "AND status IN ('ready','running') ORDER BY heartbeat_at DESC LIMIT 8",
            (task_id, agent_id),
        ).fetchall()
    owning_runner: Optional[Dict[str, Any]] = None
    for row in rows:
        runner = dict(row)
        # A dead/stale runner must never mask a genuinely missing worktree.
        ttl = max(10, int(runner.get("heartbeat_ttl_s") or 60))
        if float(runner.get("heartbeat_at") or 0) + ttl <= now:
            continue
        metadata = _json_obj(runner.get("metadata_json"), {})
        runner_session_id = str(runner.get("runner_session_id") or "")
        owns = (
            (work_session_id
             and str(metadata.get("work_session_id") or "") == work_session_id)
            or (principal_id
                and principal_id == f"direct-session/{runner_session_id}")
        )
        if owns:
            owning_runner = runner
            break
    if owning_runner is None:
        return None
    worktree_path = str(session.get("worktree_path") or session.get("clone_path") or "")
    finding = {
        "code": "host_preflight_pending",
        "message": ("Host-local worktree awaits an Agent Host git attestation; the "
                    "coordinator will not stat a path on the enrolled host."),
        "failure_class": "missing_data",
        "severity": "medium",
        "blocking": False,
        "details": {
            "worktree_path": worktree_path,
            "host_id": str(owning_runner.get("host_id") or ""),
            "runner_session_id": str(owning_runner.get("runner_session_id") or ""),
        },
    }
    return {
        "schema": "switchboard.repo_preflight.v1",
        "source": "agent_host_pending",
        "pending": True,
        "project": project,
        "repo_role": session.get("repo_role") or "canonical",
        "repo_path": worktree_path,
        "expected_branch": expected_branch,
        "expected_base_ref": expected_base_ref,
        "branch": str(session.get("branch") or ""),
        "head_sha": str(session.get("head_sha") or ""),
        "base_sha": str(session.get("base_sha") or ""),
        "upstream": str(session.get("upstream") or ""),
        "host_id": str(owning_runner.get("host_id") or ""),
        "runner_session_id": str(owning_runner.get("runner_session_id") or ""),
        "dirty": False,
        "conflict_marker_count": 0,
        "findings": [finding],
        "ok": False,
        "verdict": "warn",
        "validated_at": now,
    }


def _coerce_work_session_payload(value: Any) -> Tuple[Dict[str, Any], str]:
    if value in (None, ""):
        return {}, ""
    if isinstance(value, dict):
        return copy.deepcopy(value), ""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}, "work_session must be valid JSON"
        if isinstance(parsed, dict):
            return parsed, ""
    return {}, "work_session must be a JSON object"


def _task_work_session_profile(task: Dict[str, Any],
                               requested_profile: str = "",
                               project: str = DEFAULT_PROJECT) -> str:
    requested = (requested_profile or "").strip().lower()
    if requested:
        return _normalize_session_policy_profile(requested)
    state = task.get("agent_state") or {}
    for key in ("work_session", "session_policy", "dispatch"):
        item = state.get(key) or {}
        if isinstance(item, dict):
            profile = str(item.get("policy_profile") or item.get("profile") or "").strip().lower()
            if profile:
                return _normalize_session_policy_profile(profile)
    text = _session_profile_text(task)
    match = re.search(r"(?:policy_profile|session_profile)\s*[:=]\s*([A-Za-z0-9_-]+)", text)
    if match:
        return _normalize_session_policy_profile(match.group(1))
    defaults = _project_session_policy_defaults(project)
    if _task_looks_like_code_work(task):
        return defaults.get("code_task_default_profile") or "code_strict"
    return defaults.get("default_profile") or "docs_review"


def _work_session_required(task: Dict[str, Any], requested_profile: str = "",
                           require_work_session: bool = False,
                           project: str = DEFAULT_PROJECT) -> Tuple[bool, str]:
    profile = _task_work_session_profile(task, requested_profile, project=project)
    rules = _session_policy_profile_rules(profile, project=project)
    required = bool(require_work_session or rules.get("work_session_required"))
    return required, profile


def _unknown_session_policy_profile(profile: str, project: str) -> Dict[str, Any]:
    known = sorted((get_session_policy_profiles(project).get("profiles") or {}).keys())
    return _work_session_failure(
        "unknown_policy_profile",
        f"Unknown session policy profile: {profile or '<empty>'}.",
        "invalid_input",
        details={"policy_profile": profile or "", "known_profiles": known},
    )


def _branch_matches_task(agent_id: str, task_id: str, branch: str) -> bool:
    branch = (branch or "").strip()
    task_id = (task_id or "").strip()
    if not branch or not task_id:
        return False
    runtime = (agent_id or "").split("/", 1)[0].strip()
    expected_prefix = f"{runtime}/{task_id}" if runtime else task_id
    return branch.startswith(expected_prefix) or f"/{task_id}" in branch


def _work_session_failure(reason: str, message: str, failure_class: str,
                          severity: str = "high",
                          details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "message": message,
        "failure_class": failure_class,
        "severity": severity,
        **(details or {}),
    }


def _active_work_session_row_in(c: sqlite3.Connection, work_session_id: str = "",
                                task_id: str = "", agent_id: str = "",
                                now: Optional[float] = None) -> Optional[sqlite3.Row]:
    now = time.time() if now is None else now
    if work_session_id:
        return c.execute(
            "SELECT * FROM work_sessions WHERE work_session_id=?",
            (work_session_id,),
        ).fetchone()
    return c.execute(
        "SELECT * FROM work_sessions WHERE task_id=? AND agent_id=? "
        "AND status IN ('active','proposed') AND (expires_at IS NULL OR expires_at>?) "
        "ORDER BY updated_at DESC, created_at DESC, work_session_id LIMIT 1",
        (task_id, agent_id, now),
    ).fetchone()


def _validate_work_session_claim_binding_in(
        c: sqlite3.Connection, task: Dict[str, Any], agent_id: str,
        project: str = DEFAULT_PROJECT, work_session_id: str = "",
        work_session: Any = None, policy_profile: str = "",
        require_work_session: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    required, profile = _work_session_required(task, policy_profile, require_work_session,
                                               project=project)
    rules = _session_policy_profile_rules(profile, project=project)
    if not rules:
        return _unknown_session_policy_profile(profile, project)
    task_id = task.get("task_id") or ""
    payload, payload_error = _coerce_work_session_payload(work_session)
    if payload_error:
        return _work_session_failure("malformed_work_session", payload_error,
                                     "malformed_payload", details={"policy_profile": profile})
    if payload:
        payload = {
            **payload,
            "task_id": payload.get("task_id") or task_id,
            "agent_id": payload.get("agent_id") or agent_id,
            "policy_profile": payload.get("policy_profile") or profile,
        }
        data, errors = _validate_work_session_payload(payload, project, partial=False)
        if errors:
            return _work_session_failure(
                "invalid_work_session",
                "Work Session payload failed model validation.",
                "invalid_input",
                details={"errors": errors, "policy_profile": profile},
            )
        storage_mode = data.get("storage_mode") or ""
        allowed_modes = set(rules.get("allowed_storage_modes") or [])
        if allowed_modes and storage_mode not in allowed_modes:
            return _work_session_failure(
                "storage_mode_not_allowed",
                f"Policy profile {profile} does not allow storage_mode={storage_mode}.",
                "invalid_input",
                details={"policy_profile": profile, "storage_mode": storage_mode,
                         "allowed_storage_modes": sorted(allowed_modes)},
            )
        if data.get("work_session_id"):
            existing = c.execute("SELECT 1 FROM work_sessions WHERE work_session_id=?",
                                 (data["work_session_id"],)).fetchone()
            if existing:
                return _work_session_failure(
                    "duplicate_work_session",
                    "Work Session id already exists.",
                    "invalid_input",
                    details={"work_session_id": data["work_session_id"],
                             "policy_profile": profile},
                )
        session = _work_session_row_from_data(data, project=project)
        return _validate_work_session_claim_state(
            session, task, agent_id, project, required=required, profile=profile,
            source="payload", normalized_payload=data, now=now)

    row = _active_work_session_row_in(c, work_session_id=work_session_id,
                                      task_id=task_id, agent_id=agent_id, now=now)
    if not row:
        if required:
            return _work_session_failure(
                "work_session_required",
                "A valid Work Session is required before claiming code-strict work.",
                "missing_data",
                details={"policy_profile": profile, "required": True},
            )
        return {"ok": True, "required": False, "policy_profile": profile,
                "source": "not_required", "work_session": None}
    session = _work_session_row(row)
    return _validate_work_session_claim_state(
        session, task, agent_id, project, required=required, profile=profile,
        source="existing", normalized_payload=None, now=now)


def _work_session_row_from_data(data: Dict[str, Any], project: str) -> Dict[str, Any]:
    return {
        "schema": WORK_SESSION_SCHEMA,
        "work_session_id": data.get("work_session_id") or "",
        "project_id": project,
        "task_id": data.get("task_id") or "",
        "claim_id": data.get("claim_id") or "",
        "agent_id": data.get("agent_id") or "",
        "runtime": data.get("runtime") or "",
        "repo_role": data.get("repo_role") or "",
        "repo": data.get("repo") or "",
        "default_branch": data.get("default_branch") or "",
        "branch": data.get("branch") or "",
        "upstream": data.get("upstream") or "",
        "base_sha": data.get("base_sha") or "",
        "head_sha": data.get("head_sha") or "",
        "worktree_path": data.get("worktree_path") or "",
        "clone_path": data.get("clone_path") or "",
        "storage_mode": data.get("storage_mode") or "",
        "status": data.get("status") or "",
        "dirty_status": data.get("dirty_status") or "",
        "conflict_marker_count": data.get("conflict_marker_count") or 0,
        "hygiene": _json_obj(data.get("hygiene_json") or "{}", {}),
        "file_leases": _json_obj(data.get("file_leases_json") or "[]", []),
        "resource_leases": _json_obj(data.get("resource_leases_json") or "[]", []),
        "env": _json_obj(data.get("env_json") or "{}", {}),
        "policy_profile": data.get("policy_profile") or "",
        "expires_at": data.get("expires_at"),
        "session_token_hash_present": bool(data.get("session_token_hash")),
    }


def _validate_work_session_claim_state(
        session: Dict[str, Any], task: Dict[str, Any], agent_id: str,
        project: str, required: bool, profile: str, source: str,
        normalized_payload: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
        allow_dirty: bool = False) -> Dict[str, Any]:
    now = time.time() if now is None else now
    task_id = task.get("task_id") or ""
    rules = _session_policy_profile_rules(profile, project=project)
    if not rules:
        return _unknown_session_policy_profile(profile, project)
    problems: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    def add_problem(reason: str, failure_class: str, message: str) -> None:
        item = {"reason": reason, "failure_class": failure_class, "message": message}
        if reason in set(rules.get("warn_hygiene") or []):
            warnings.append(item)
        else:
            problems.append(item)

    if session.get("project_id") != project:
        add_problem("wrong_project", "invalid_input",
                    "Work Session project_id does not match claim project.")
    if session.get("task_id") and session.get("task_id") != task_id:
        add_problem("wrong_task", "invalid_input",
                    "Work Session task_id does not match claimed task.")
    if session.get("agent_id") and session.get("agent_id") != agent_id:
        add_problem("wrong_agent", "unbound_identity",
                    "Work Session agent_id does not match claiming agent.")
    if session.get("status") not in {"active", "proposed"}:
        add_problem("inactive_work_session", "failed_gate",
                    "Work Session is not active/proposed.")
    expires_at = session.get("expires_at")
    if expires_at is not None:
        try:
            if float(expires_at) <= now:
                add_problem("expired_work_session", "stale_branch",
                            "Work Session is expired.")
        except (TypeError, ValueError):
            add_problem("invalid_work_session_expiry", "invalid_input",
                        "Work Session expires_at is invalid.")
    if session.get("dirty_status") == "dirty" and not allow_dirty:
        add_problem("dirty_work_session", "failed_gate",
                    "Work Session reports a dirty workspace.")
    if int(session.get("conflict_marker_count") or 0) > 0:
        add_problem("conflict_markers", "failed_gate",
                    "Work Session reports conflict markers.")
    if rules.get("requires_branch_task_scope") and not _branch_matches_task(
            agent_id, task_id, session.get("branch") or ""):
        add_problem("wrong_branch", "stale_branch",
                    "Work Session branch must be task-scoped.")
    if rules.get("requires_upstream") and not session.get("upstream"):
        add_problem("missing_upstream", "missing_data",
                    "Work Session upstream is required for this profile.")
    if rules.get("requires_base_sha") and not session.get("base_sha"):
        add_problem("missing_base_sha", "missing_data",
                    "Work Session base_sha is required for this profile.")
    if problems:
        first = problems[0]
        return _work_session_failure(
            first["reason"], first["message"], first["failure_class"],
            details={"problems": problems, "policy_profile": profile,
                     "required": required, "work_session_id": session.get("work_session_id") or None},
        )
    return {
        "ok": True,
        "required": required,
        "policy_profile": profile,
        "policy": rules,
        "warnings": warnings,
        "source": source,
        "work_session": session,
        "normalized_payload": normalized_payload,
    }


def _work_session_stale_lease_problems(session: Dict[str, Any],
                                       now: float) -> List[Dict[str, Any]]:
    problems: List[Dict[str, Any]] = []
    for field in ("file_leases", "resource_leases"):
        for lease in session.get(field) or []:
            if not isinstance(lease, dict):
                continue
            if lease.get("released_at") or str(lease.get("status") or "").lower() in {"released", "completed"}:
                continue
            expires_at = lease.get("expires_at")
            if expires_at in (None, ""):
                continue
            try:
                expired = float(expires_at) <= now
            except (TypeError, ValueError):
                expired = True
            if expired:
                problems.append({
                    "reason": "stale_work_session_lease",
                    "failure_class": "stale_branch",
                    "message": f"Work Session has an expired unreleased {field[:-1]}.",
                    "lease": lease,
                })
    return problems


class StoreWorkSessionsRepository:
    """SQL-backed work session + session health repository (ARCH-MS-46)."""

    def create_work_session(self, payload: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        return create_work_session(payload, **kwargs)

    def get_work_session(self, work_session_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        return get_work_session(work_session_id, **kwargs)

    def list_work_sessions(self, **kwargs) -> List[Dict[str, Any]]:
        return list_work_sessions(**kwargs)

    def update_work_session(self, work_session_id: str, payload: Dict[str, Any],
                            **kwargs) -> Dict[str, Any]:
        return update_work_session(work_session_id, payload, **kwargs)

    def get_work_session_health(self, work_session_id: str, **kwargs) -> Dict[str, Any]:
        return get_work_session_health(work_session_id, **kwargs)

    def list_session_health(self, **kwargs) -> Dict[str, Any]:
        return list_session_health(**kwargs)

    def create_managed_work_session(self, payload: Dict[str, Any],
                                    **kwargs) -> Dict[str, Any]:
        return create_managed_work_session(payload, **kwargs)

    def preflight_work_session(self, work_session_id: str, **kwargs) -> Dict[str, Any]:
        return preflight_work_session(work_session_id, **kwargs)

    def archive_work_session_workspace(self, work_session_id: str,
                                       **kwargs) -> Dict[str, Any]:
        return archive_work_session_workspace(work_session_id, **kwargs)


def default_work_sessions_repository() -> StoreWorkSessionsRepository:
    return StoreWorkSessionsRepository()


__all__ = [
    "StoreWorkSessionsRepository",
    "default_work_sessions_repository",
    "PR_BACKED_STATUSES",
    "PR_ACTIVE_SESSION_STATUSES",
    "work_session_contract",
    "create_work_session",
    "get_work_session",
    "issue_work_session_mcp_token",
    "get_principal_by_work_session_token_any_project",
    "list_work_sessions",
    "update_work_session",
    "get_work_session_health",
    "list_session_health",
    "create_managed_work_session",
    "archive_work_session_workspace",
    "preflight_work_session",
    "pr_backed_by_process",
    "_session_health_summary",
    "_task_session_health_in",
    "_work_session_json",
    "_work_session_row",
    "_session_health_finding",
    "_work_session_health",
    "_validate_work_session_payload",
    "_insert_work_session_in",
    "_managed_workspace_error",
    "_managed_workspace_git",
    "_managed_workspace_slug",
    "_managed_workspace_branch",
    "_managed_workspace_root",
    "_managed_workspace_source_path",
    "_managed_workspace_path",
    "_managed_role",
    "_managed_verify_source_repo",
    "_managed_prepare_worktree",
    "_managed_prepare_clone",
    "_coerce_work_session_payload",
    "_task_work_session_profile",
    "_work_session_required",
    "_unknown_session_policy_profile",
    "_branch_matches_task",
    "_work_session_failure",
    "_active_work_session_row_in",
    "_validate_work_session_claim_binding_in",
    "_work_session_row_from_data",
    "_validate_work_session_claim_state",
    "_work_session_stale_lease_problems",
]
