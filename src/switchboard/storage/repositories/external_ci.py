"""External CI mirror repository (ARCH-MS-47).

Owns external_ci_runs CRUD, topology/request validation, task summaries, and
review-gate helpers previously living in ``repositories/shell.py``. Cross-cutting
store helpers (init_db, topology, external effects) are reached via
``_store_facade()`` during the strangler. ``store.py`` / ``shell.py`` re-export
these symbols; root ``external_ci_store.py`` is a compatibility shim.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from constants import *  # noqa: F401,F403
from db.connection import _conn
from db.core import *  # noqa: F401,F403
from switchboard.storage.repositories.access import has_project  # noqa: F401
from switchboard.storage.repositories.provenance import _normalize_repo_slug  # noqa: F401
from switchboard.storage.repositories.tasks import get_task  # noqa: F401


def _store_facade():
    """Resolve transitional store helpers after store.py is initialized."""
    import store
    return store


def init_db(*args, **kwargs):
    return _store_facade().init_db(*args, **kwargs)


def get_project_repo_topology(*args, **kwargs):
    return _store_facade().get_project_repo_topology(*args, **kwargs)


def get_project_github_repo(*args, **kwargs):
    return _store_facade().get_project_github_repo(*args, **kwargs)


def _validate_github_repo(*args, **kwargs):
    return _store_facade()._validate_github_repo(*args, **kwargs)


def _claim_external_effect_in(*args, **kwargs):
    return _store_facade()._claim_external_effect_in(*args, **kwargs)


def _update_external_effect_in(*args, **kwargs):
    return _store_facade()._update_external_effect_in(*args, **kwargs)


EXTERNAL_CI_STATUSES = {
    "requested", "mirrored", "triggered", "running", "success", "failure", "cancelled", "error"
}
EXTERNAL_CI_TERMINAL_STATUSES = {"success", "failure", "cancelled", "error"}
EXTERNAL_CI_FAILURE_CLASSES = {
    "mirror_sync_failed": "stale_branch",
    "workflow_trigger_failed": "broken_connection",
    "workflow_poll_failed": "broken_connection",
    "workflow_failed": "failed_gate",
}
GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
WORKFLOW_REF_RE = re.compile(r"^[A-Za-z0-9_.@:/-]+$")

def default_external_ci_mirror_branch(task_id: str, source_sha: str) -> str:
    task = re.sub(r"[^A-Za-z0-9_.-]+", "-", (task_id or "task").strip()).strip("-") or "task"
    sha = (source_sha or "").strip()[:12] or "unknown"
    return f"ci/{task}/{sha}"


def _external_ci_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    d["artifacts"] = _json_obj(d.pop("artifacts_json", "[]"), [])
    d["request"] = _json_obj(d.pop("request_json", "{}"), {})
    d["result"] = _json_obj(d.pop("result_json", "{}"), {})
    d["ci_repo"] = d.get("mirror_repo")
    d["status_context"] = (
        d.get("status_context")
        or (d.get("request") or {}).get("status_context")
        or (d.get("result") or {}).get("status_context")
        or None
    )
    d["required_status_contexts"] = (
        (d.get("request") or {}).get("required_status_contexts")
        or ([d["status_context"]] if d.get("status_context") else [])
    )
    d["repo_role"] = "public_ci"
    d["evidence_only"] = True
    return d


def _validate_external_ci_status(status: str) -> str:
    clean = (status or "requested").strip().lower()
    return clean if clean in EXTERNAL_CI_STATUSES else ""


def _validate_external_ci_failure_class(value: str) -> str:
    clean = (value or "").strip().lower()
    return clean if not clean or clean in EXTERNAL_CI_FAILURE_CLASSES else ""


def _external_ci_topology_contract(source_project: str,
                                   data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resolve the canonical source repo and public CI role for external proof."""
    data = data or {}
    topology = get_project_repo_topology(source_project)
    roles = topology.get("roles") or {}
    canonical = roles.get("canonical") or {}
    public_ci = roles.get("public_ci") or {}
    source_repo = (canonical.get("repo") or "").strip()
    ci_repo = (public_ci.get("repo") or "").strip()
    required_contexts = _coerce_str_list(public_ci.get("required_status_contexts"))
    requested_context = (
        data.get("status_context")
        or data.get("required_status_context")
        or data.get("required_status_contexts")
        or ""
    )
    if isinstance(requested_context, (list, tuple)):
        requested_context = requested_context[0] if requested_context else ""
    status_context = str(requested_context or "").strip()
    if not status_context and required_contexts:
        status_context = required_contexts[0]
    return {
        "schema": "switchboard.external_ci_topology_contract.v1",
        "source_project": source_project,
        "source_repo": source_repo,
        "ci_repo": ci_repo,
        "status_context": status_context or None,
        "required_status_contexts": required_contexts,
        "repo_topology_schema": topology.get("schema"),
        "repo_topology_valid": topology.get("valid"),
        "code_repo_gate": topology.get("code_repo_gate"),
        "public_ci_role": public_ci,
        "canonical_role": canonical,
        "evidence_only": True,
        "authority": "verification_only",
    }


def _repo_mismatch(got: str, expected: str) -> bool:
    return bool(got and expected and _normalize_repo_slug(got) != _normalize_repo_slug(expected))


def _external_ci_request_payload(data: Dict[str, Any], project: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source_project = (data.get("source_project") or project or DEFAULT_PROJECT).strip()
    if not has_project(source_project):
        return {}, {"error": f"unknown source project: {source_project}"}
    task_id = (data.get("task_id") or "").strip().upper()
    if task_id and not get_task(task_id, project=project):
        return {}, {"error": "unknown task", "task_id": task_id, "project": project}
    contract = _external_ci_topology_contract(source_project, data)
    if not (contract.get("code_repo_gate") or {}).get("passed"):
        return {}, {"error": "canonical source repo is not configured",
                    "source_project": source_project,
                    "code_repo_gate": contract.get("code_repo_gate")}
    source_repo, source_repo_error = _validate_github_repo(
        data.get("source_repo") or contract.get("source_repo") or get_project_github_repo(source_project))
    if source_repo_error:
        return {}, {"error": source_repo_error, "repo": source_repo, "field": "source_repo"}
    if not source_repo:
        return {}, {"error": "source_repo required", "source_project": source_project}
    if _repo_mismatch(source_repo, contract.get("source_repo") or ""):
        return {}, {"error": "source_repo must match repo_topology.roles.canonical.repo",
                    "repo": source_repo, "expected": contract.get("source_repo"),
                    "field": "source_repo", "source_project": source_project}
    mirror_repo, mirror_repo_error = _validate_github_repo(
        data.get("mirror_repo") or data.get("ci_repo") or contract.get("ci_repo") or "")
    if mirror_repo_error:
        return {}, {"error": mirror_repo_error, "repo": mirror_repo, "field": "mirror_repo"}
    if not mirror_repo:
        return {}, {"error": "mirror_repo required",
                    "hint": "configure repo_topology.roles.public_ci.repo or pass mirror_repo"}
    if _repo_mismatch(mirror_repo, contract.get("ci_repo") or ""):
        return {}, {"error": "mirror_repo must match repo_topology.roles.public_ci.repo",
                    "repo": mirror_repo, "expected": contract.get("ci_repo"),
                    "field": "mirror_repo", "source_project": source_project}
    source_sha = (data.get("source_sha") or "").strip()
    if not GIT_SHA_RE.match(source_sha):
        return {}, {"error": "source_sha must be a 7-64 character hex Git SHA"}
    workflow = (data.get("workflow") or "").strip()
    if not workflow:
        return {}, {"error": "workflow required"}
    if not WORKFLOW_REF_RE.match(workflow):
        return {}, {"error": "workflow contains unsupported characters"}
    mirror_branch = (data.get("mirror_branch") or
                     default_external_ci_mirror_branch(task_id, source_sha)).strip()
    if not mirror_branch.startswith("ci/"):
        return {}, {"error": "mirror_branch must be under ci/"}
    status = _validate_external_ci_status(data.get("status") or "requested")
    if not status:
        return {}, {"error": "invalid external CI status",
                    "allowed": sorted(EXTERNAL_CI_STATUSES)}
    failure_class = _validate_external_ci_failure_class(data.get("failure_class") or "")
    if (data.get("failure_class") or "") and not failure_class:
        return {}, {"error": "invalid external CI failure_class",
                    "allowed": sorted(EXTERNAL_CI_FAILURE_CLASSES)}
    return {
        "source_project": source_project,
        "source_repo": source_repo,
        "source_branch": (data.get("source_branch") or "").strip() or None,
        "source_sha": source_sha.lower(),
        "mirror_repo": mirror_repo,
        "mirror_branch": mirror_branch,
        "workflow": workflow,
        "status_context": contract.get("status_context"),
        "required_status_contexts": contract.get("required_status_contexts") or [],
        "status": status,
        "conclusion": (data.get("conclusion") or "").strip() or None,
        "run_url": (data.get("run_url") or "").strip() or None,
        "logs_url": (data.get("logs_url") or "").strip() or None,
        "artifacts": data.get("artifacts") or [],
        "failure_class": failure_class or None,
        "failure_reason": (data.get("failure_reason") or "").strip() or None,
        "task_id": task_id or None,
        "claim_id": (data.get("claim_id") or "").strip() or None,
        "agent_id": (data.get("agent_id") or "").strip() or None,
        "principal_id": (data.get("principal_id") or "").strip() or None,
        "request": data.get("request") or {},
        "result": data.get("result") or {},
        "topology_contract": contract,
    }, {}


def create_external_ci_run(data: Dict[str, Any], actor: str = "system",
                           project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    normalized, error = _external_ci_request_payload(data or {}, project)
    if error:
        return error
    now = time.time()
    run_id = (data.get("run_id") or "ecir-" + uuid.uuid4().hex[:16]).strip()
    side_payload = {
        "source_project": normalized["source_project"],
        "source_repo": normalized["source_repo"],
        "source_branch": normalized["source_branch"],
        "source_sha": normalized["source_sha"],
        "mirror_repo": normalized["mirror_repo"],
        "mirror_branch": normalized["mirror_branch"],
        "workflow": normalized["workflow"],
        "status_context": normalized["status_context"],
        "required_status_contexts": normalized["required_status_contexts"],
        "ci_repo": normalized["mirror_repo"],
        "evidence_only": True,
        "task_id": normalized["task_id"],
        "claim_id": normalized["claim_id"],
    }
    request_payload = {
        **(normalized["request"] or {}),
        "source_repo": normalized["source_repo"],
        "source_sha": normalized["source_sha"],
        "ci_repo": normalized["mirror_repo"],
        "mirror_repo": normalized["mirror_repo"],
        "status_context": normalized["status_context"],
        "required_status_contexts": normalized["required_status_contexts"],
        "repo_topology": {
            "schema": normalized["topology_contract"].get("repo_topology_schema"),
            "source_project": normalized["source_project"],
            "source_repo": normalized["source_repo"],
            "ci_repo": normalized["mirror_repo"],
            "status_context": normalized["status_context"],
            "evidence_only": True,
        },
    }
    with _conn(project) as c:
        effect = _claim_external_effect_in(
            c,
            "external_ci_mirror",
            normalized["mirror_repo"],
            normalized["mirror_branch"],
            side_payload,
            task_id=normalized["task_id"],
            claim_id=normalized["claim_id"] or "",
            agent_id=normalized["agent_id"] or "",
            idem_key=(data.get("idem_key") or ""),
            actor=actor,
            principal_id=normalized["principal_id"] or "",
            project=project,
            now=now,
        )
        effect_key = effect["effect_key"]
        existing = c.execute("SELECT * FROM external_ci_runs WHERE effect_key=?",
                             (effect_key,)).fetchone()
        if existing:
            out = _external_ci_row(existing)
            out["idempotent"] = True
            out["side_effect"] = effect
            return out
        c.execute(
            """INSERT INTO external_ci_runs
               (run_id, source_project, source_repo, source_branch, source_sha,
                mirror_repo, mirror_branch, workflow, status_context, status, conclusion, run_url,
                logs_url, artifacts_json, failure_class, failure_reason, task_id,
                claim_id, agent_id, actor, principal_id, effect_key, request_json,
                result_json, requested_at, mirrored_at, triggered_at, completed_at,
                updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, normalized["source_project"], normalized["source_repo"],
                normalized["source_branch"], normalized["source_sha"], normalized["mirror_repo"],
                normalized["mirror_branch"], normalized["workflow"], normalized["status_context"],
                normalized["status"],
                normalized["conclusion"], normalized["run_url"], normalized["logs_url"],
                json.dumps(normalized["artifacts"], sort_keys=True),
                normalized["failure_class"], normalized["failure_reason"], normalized["task_id"],
                normalized["claim_id"], normalized["agent_id"], actor,
                normalized["principal_id"], effect_key,
                json.dumps(request_payload, sort_keys=True),
                json.dumps(normalized["result"], sort_keys=True),
                now,
                now if normalized["status"] in {"mirrored", "triggered", "running", "success", "failure"} else None,
                now if normalized["status"] in {"triggered", "running", "success", "failure"} else None,
                now if normalized["status"] in EXTERNAL_CI_TERMINAL_STATUSES else None,
                now,
            ),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (normalized["task_id"], actor, "external_ci.requested",
                   json.dumps({"run_id": run_id, "effect_key": effect_key,
                               "source_project": normalized["source_project"],
                               "source_repo": normalized["source_repo"],
                               "source_sha": normalized["source_sha"],
                               "ci_repo": normalized["mirror_repo"],
                               "mirror_repo": normalized["mirror_repo"],
                               "mirror_branch": normalized["mirror_branch"],
                               "workflow": normalized["workflow"],
                               "status_context": normalized["status_context"],
                               "evidence_only": True}, sort_keys=True), now))
        row = c.execute("SELECT * FROM external_ci_runs WHERE run_id=?", (run_id,)).fetchone()
    out = _external_ci_row(row)
    out["side_effect"] = effect
    return out


def update_external_ci_run(run_id: str, fields: Dict[str, Any], actor: str = "system",
                           project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    allowed = {"status", "conclusion", "run_url", "logs_url", "artifacts",
               "failure_class", "failure_reason", "result"}
    updates = {k: v for k, v in (fields or {}).items() if k in allowed}
    if not updates:
        return get_external_ci_run(run_id, project=project) or {"error": "external_ci_run not found"}
    status = _validate_external_ci_status(updates.get("status") or "")
    if "status" in updates and not status:
        return {"error": "invalid external CI status", "allowed": sorted(EXTERNAL_CI_STATUSES)}
    failure_class = _validate_external_ci_failure_class(updates.get("failure_class") or "")
    if (updates.get("failure_class") or "") and not failure_class:
        return {"error": "invalid external CI failure_class",
                "allowed": sorted(EXTERNAL_CI_FAILURE_CLASSES)}
    now = time.time()
    sets: List[str] = ["updated_at=?"]
    vals: List[Any] = [now]
    if "status" in updates:
        sets.append("status=?"); vals.append(status)
        if status in {"mirrored", "triggered", "running", "success", "failure"}:
            sets.append("mirrored_at=COALESCE(mirrored_at, ?)")
            vals.append(now)
        if status in {"triggered", "running", "success", "failure"}:
            sets.append("triggered_at=COALESCE(triggered_at, ?)")
            vals.append(now)
        if status in EXTERNAL_CI_TERMINAL_STATUSES:
            sets.append("completed_at=COALESCE(completed_at, ?)")
            vals.append(now)
    for key, column in (("conclusion", "conclusion"), ("run_url", "run_url"),
                        ("logs_url", "logs_url"), ("failure_reason", "failure_reason")):
        if key in updates:
            sets.append(f"{column}=?"); vals.append((updates.get(key) or "").strip() or None)
    if "failure_class" in updates:
        sets.append("failure_class=?"); vals.append(failure_class or None)
    if "artifacts" in updates:
        sets.append("artifacts_json=?")
        vals.append(json.dumps(updates.get("artifacts") or [], sort_keys=True))
    if "result" in updates:
        sets.append("result_json=?")
        vals.append(json.dumps(updates.get("result") or {}, sort_keys=True))
    vals.append(run_id)
    with _conn(project) as c:
        row = c.execute("SELECT * FROM external_ci_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return {"error": "external_ci_run not found", "run_id": run_id}
        c.execute(f"UPDATE external_ci_runs SET {', '.join(sets)} WHERE run_id=?", vals)
        if "status" in updates:
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "external_ci.status",
                       json.dumps({"run_id": run_id, "status": status,
                                   "conclusion": updates.get("conclusion")},
                                  sort_keys=True), now))
        updated = c.execute("SELECT * FROM external_ci_runs WHERE run_id=?",
                            (run_id,)).fetchone()
    return _external_ci_row(updated)


def get_external_ci_run(run_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    init_db(project)
    with _conn(project) as c:
        return _external_ci_row(c.execute(
            "SELECT * FROM external_ci_runs WHERE run_id=?", (run_id,)).fetchone())


def list_external_ci_runs(task_id: str = "", source_project: str = "",
                          source_sha: str = "", status: str = "",
                          project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    init_db(project)
    q = "SELECT * FROM external_ci_runs WHERE 1=1"
    params: List[Any] = []
    if task_id:
        q += " AND task_id=?"; params.append(task_id.strip().upper())
    if source_project:
        q += " AND source_project=?"; params.append(source_project.strip())
    if source_sha:
        q += " AND source_sha=?"; params.append(source_sha.strip().lower())
    if status:
        q += " AND status=?"; params.append(status.strip().lower())
    q += " ORDER BY updated_at DESC, run_id"
    with _conn(project) as c:
        return [_external_ci_row(row) for row in c.execute(q, params).fetchall()]


def _sha_matches(candidate: str, target: str) -> bool:
    cand = (candidate or "").strip().lower()
    want = (target or "").strip().lower()
    if not cand or not want:
        return False
    return cand.startswith(want) or want.startswith(cand)


def _external_ci_summary(rows: List[Dict[str, Any]], source_sha: str = "",
                         project: str = DEFAULT_PROJECT,
                         contract: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if source_sha:
        rows = [r for r in rows if _sha_matches(r.get("source_sha") or "", source_sha)]
    success = [r for r in rows if r.get("status") == "success" and r.get("conclusion") == "success"]
    failures = [r for r in rows if r.get("status") in {"failure", "error", "cancelled"}]
    pending = [r for r in rows if r.get("status") in {"requested", "mirrored", "triggered", "running"}]
    latest = rows[0] if rows else None
    passed = bool(success)
    if passed:
        status = "passed"
        selected = success[0]
    elif pending:
        status = "pending"
        selected = latest
    elif failures:
        status = "failed"
        selected = latest
    else:
        status = "missing"
        selected = None
    contract = contract or _external_ci_topology_contract(project)
    source_repo = (
        (selected or {}).get("source_repo")
        or (rows[0].get("source_repo") if rows else None)
        or contract.get("source_repo")
    )
    ci_repo = (
        (selected or {}).get("ci_repo")
        or (selected or {}).get("mirror_repo")
        or (rows[0].get("ci_repo") if rows else None)
        or (rows[0].get("mirror_repo") if rows else None)
        or contract.get("ci_repo")
    )
    status_context = (
        (selected or {}).get("status_context")
        or (rows[0].get("status_context") if rows else None)
        or contract.get("status_context")
    )
    run_url = (selected or {}).get("run_url") or (rows[0].get("run_url") if rows else None)
    return {
        "status": status,
        "passed": passed,
        "required": False,
        "source_repo": source_repo,
        "source_sha": source_sha or ((selected or {}).get("source_sha") if selected else None),
        "ci_repo": ci_repo,
        "mirror_repo": ci_repo,
        "run_url": run_url,
        "status_context": status_context,
        "required_status_contexts": (
            (selected or {}).get("required_status_contexts")
            or (rows[0].get("required_status_contexts") if rows else None)
            or contract.get("required_status_contexts")
            or []
        ),
        "repo_role": "public_ci",
        "evidence_only": True,
        "run_count": len(rows),
        "success_count": len(success),
        "failure_count": len(failures),
        "pending_count": len(pending),
        "latest": selected,
        "runs": rows[:5],
    }


def _task_external_ci_summary_in(c: sqlite3.Connection, task_id: str,
                                 source_sha: str = "",
                                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    rows = [
        _external_ci_row(row)
        for row in c.execute(
            "SELECT * FROM external_ci_runs WHERE task_id=? "
            "ORDER BY updated_at DESC, run_id",
            (task_id,),
        ).fetchall()
    ]
    return _external_ci_summary(rows, source_sha=source_sha, project=project)


def task_external_ci_summary(task_id: str, source_sha: str = "",
                             project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    with _conn(project) as c:
        return _task_external_ci_summary_in(c, task_id, source_sha=source_sha, project=project)


def _external_ci_required_from(task: Dict[str, Any],
                               evidence: Optional[Dict[str, Any]] = None) -> bool:
    evidence = evidence or {}
    if evidence.get("external_ci_required") is True:
        return True
    gates = evidence.get("required_gates") or evidence.get("review_gates") or []
    if isinstance(gates, str):
        gates = coerce_csv_list(gates)
    if any(str(g).strip().lower() in {"external_ci", "external_ci_passed"} for g in gates):
        return True
    state = task.get("agent_state") or {}
    for key in ("review_gate", "review_gates", "proof_requirements"):
        value = state.get(key) or {}
        if isinstance(value, dict) and (
                value.get("external_ci_required") or value.get("external_ci_passed")):
            return True
    text = "\n".join(str(task.get(k) or "") for k in (
        "entry_criteria", "exit_criteria", "deliverable"))
    return "external_ci_passed" in text or "external ci passed" in text.lower()


def _external_ci_review_gate(task: Dict[str, Any],
                             evidence: Optional[Dict[str, Any]] = None,
                             c: Optional[sqlite3.Connection] = None,
                             project: str = DEFAULT_PROJECT,
                             summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    evidence = evidence or {}
    source_sha = (
        evidence.get("external_ci_source_sha")
        or evidence.get("source_sha")
        or evidence.get("head_sha")
        or (task.get("git_state") or {}).get("head_sha")
        or ""
    )
    if summary is not None:
        summary = dict(summary)
    elif c is None:
        with _conn(project) as own:
            summary = _task_external_ci_summary_in(
                own, task["task_id"], source_sha=source_sha, project=project)
    else:
        summary = _task_external_ci_summary_in(
            c, task["task_id"], source_sha=source_sha, project=project)
    required = _external_ci_required_from(task, evidence)
    summary["required"] = required
    summary["gate"] = {
        "name": "external_ci_passed",
        "required": required,
        "passed": summary["passed"],
        "status": (
            "passed" if summary["passed"] else
            "blocked" if required else
            "not_required"
        ),
        "message": (
            "External CI mirror passed for this source SHA."
            if summary["passed"] else
            "External CI mirror evidence is required before review/merge."
            if required else
            "External CI mirror evidence is optional for this task."
        ),
    }
    return summary



class StoreExternalCiRepository:
    """Thin repository wrapper over module-level external CI helpers."""

    def create_external_ci_run(self, data, actor="system", project=DEFAULT_PROJECT):
        return create_external_ci_run(data, actor=actor, project=project)

    def update_external_ci_run(self, run_id, fields, actor="system", project=DEFAULT_PROJECT):
        return update_external_ci_run(run_id, fields, actor=actor, project=project)

    def get_external_ci_run(self, run_id, project=DEFAULT_PROJECT):
        return get_external_ci_run(run_id, project=project)

    def list_external_ci_runs(self, project=DEFAULT_PROJECT, **kwargs):
        return list_external_ci_runs(project=project, **kwargs)

    def task_external_ci_summary(self, task_id, project=DEFAULT_PROJECT):
        return task_external_ci_summary(task_id, project=project)


def default_external_ci_repository() -> StoreExternalCiRepository:
    return StoreExternalCiRepository()


__all__ = [
    "EXTERNAL_CI_STATUSES",
    "EXTERNAL_CI_TERMINAL_STATUSES",
    "EXTERNAL_CI_FAILURE_CLASSES",
    "GIT_SHA_RE",
    "WORKFLOW_REF_RE",
    "StoreExternalCiRepository",
    "default_external_ci_repository",
    "default_external_ci_mirror_branch",
    "create_external_ci_run",
    "update_external_ci_run",
    "get_external_ci_run",
    "list_external_ci_runs",
    "task_external_ci_summary",
    "_external_ci_row",
    "_validate_external_ci_status",
    "_validate_external_ci_failure_class",
    "_external_ci_topology_contract",
    "_repo_mismatch",
    "_external_ci_request_payload",
    "_sha_matches",
    "_external_ci_summary",
    "_task_external_ci_summary_in",
    "_external_ci_required_from",
    "_external_ci_review_gate",
]
