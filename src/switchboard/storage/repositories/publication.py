"""Publication evidence repository (ARCH-MS-47).

Owns publication evidence CRUD, topology/request validation, task summaries,
review-gate helpers, and reconcile findings previously living in
``repositories/shell.py``. Cross-cutting store helpers are reached via
``_store_facade()`` during the strangler. ``store.py`` / ``shell.py`` re-export
these symbols; root ``publication_store.py`` is a compatibility shim.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from constants import *  # noqa: F401,F403
from db.connection import _conn
from db.core import *  # noqa: F401,F403
from switchboard.storage.repositories.access import has_project  # noqa: F401
from switchboard.storage.repositories.external_ci import (  # noqa: F401
    GIT_SHA_RE,
    _sha_matches,
)
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


PUBLICATION_GUARD_STATUSES = {"passed", "failed", "warning", "unknown"}


def _publication_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = dict(row)
    d["guard"] = _json_obj(d.pop("guard_json", "{}"), {})
    d["repo_role"] = "public"
    d["evidence_only"] = True
    return d


def _validate_publication_guard_status(value: str) -> str:
    clean = (value or "unknown").strip().lower()
    return clean if clean in PUBLICATION_GUARD_STATUSES else ""


def _repo_mismatch(got: str, expected: str) -> bool:
    return bool(got and expected and _normalize_repo_slug(got) != _normalize_repo_slug(expected))


def _publication_topology_contract(source_project: str,
                                   data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = data or {}
    topology = get_project_repo_topology(source_project)
    roles = topology.get("roles") or {}
    canonical = roles.get("canonical") or {}
    public = roles.get("public") or {}
    script = (data.get("script") or data.get("publish_script") or "").strip()
    if not script:
        scripts = _coerce_str_list(public.get("publish_scripts"))
        script = scripts[0] if scripts else ""
    return {
        "schema": "switchboard.publication_topology_contract.v1",
        "source_project": source_project,
        "source_repo": (canonical.get("repo") or "").strip(),
        "public_repo": (public.get("repo") or data.get("public_repo") or "").strip(),
        "publish_scripts": _coerce_str_list(public.get("publish_scripts")),
        "script": script or None,
        "repo_topology_schema": topology.get("schema"),
        "repo_topology_valid": topology.get("valid"),
        "code_repo_gate": topology.get("code_repo_gate"),
        "public_role": public,
        "canonical_role": canonical,
        "evidence_only": True,
        "authority": "publish_evidence_only",
    }


def _publication_request_payload(data: Dict[str, Any], project: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source_project = (data.get("source_project") or project or DEFAULT_PROJECT).strip()
    if not has_project(source_project):
        return {}, {"error": f"unknown source project: {source_project}"}
    task_id = (data.get("task_id") or "").strip().upper()
    if task_id and not get_task(task_id, project=project):
        return {}, {"error": "unknown task", "task_id": task_id, "project": project}
    contract = _publication_topology_contract(source_project, data)
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
    public_repo, public_repo_error = _validate_github_repo(
        data.get("public_repo") or contract.get("public_repo") or "")
    if public_repo_error:
        return {}, {"error": public_repo_error, "repo": public_repo, "field": "public_repo"}
    if not public_repo:
        return {}, {"error": "public_repo required",
                    "hint": "configure repo_topology.roles.public.repo or pass public_repo"}
    configured_public = ((contract.get("public_role") or {}).get("repo") or "").strip()
    if _repo_mismatch(public_repo, configured_public):
        return {}, {"error": "public_repo must match repo_topology.roles.public.repo",
                    "repo": public_repo, "expected": configured_public,
                    "field": "public_repo", "source_project": source_project}
    source_sha = (data.get("source_sha") or "").strip()
    if not GIT_SHA_RE.match(source_sha):
        return {}, {"error": "source_sha must be a 7-64 character hex Git SHA"}
    public_sha = (data.get("public_sha") or "").strip()
    if public_sha and not GIT_SHA_RE.match(public_sha):
        return {}, {"error": "public_sha must be a 7-64 character hex Git SHA",
                    "field": "public_sha"}
    public_ref = (data.get("public_ref") or data.get("ref") or "").strip()
    public_tag = (data.get("public_tag") or data.get("tag") or "").strip() or None
    if not public_ref and public_tag:
        public_ref = f"refs/tags/{public_tag}"
    if not public_ref:
        return {}, {"error": "public_ref required"}
    guard_status = _validate_publication_guard_status(
        data.get("guard_status") or (data.get("guard") or {}).get("status") or "unknown")
    if not guard_status:
        return {}, {"error": "invalid publication guard_status",
                    "allowed": sorted(PUBLICATION_GUARD_STATUSES)}
    published_at = data.get("published_at") or data.get("timestamp")
    try:
        published_at = float(published_at) if published_at not in (None, "") else time.time()
    except (TypeError, ValueError):
        return {}, {"error": "published_at must be a unix timestamp"}
    return {
        "source_project": source_project,
        "source_repo": source_repo,
        "source_sha": source_sha.lower(),
        "public_repo": public_repo,
        "public_ref": public_ref,
        "public_sha": public_sha.lower() or None,
        "public_tag": public_tag,
        "script": (data.get("script") or data.get("publish_script") or contract.get("script") or "").strip() or None,
        "guard_status": guard_status,
        "guard": data.get("guard") or data.get("guard_result") or {},
        "artifact_url": (data.get("artifact_url") or "").strip() or None,
        "task_id": task_id or None,
        "claim_id": (data.get("claim_id") or "").strip() or None,
        "agent_id": (data.get("agent_id") or "").strip() or None,
        "principal_id": (data.get("principal_id") or "").strip() or None,
        "published_at": published_at,
        "topology_contract": contract,
    }, {}


def create_publication_evidence(data: Dict[str, Any], actor: str = "system",
                                project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    normalized, error = _publication_request_payload(data or {}, project)
    if error:
        return error
    now = time.time()
    publication_id = (data.get("publication_id") or "pub-" + uuid.uuid4().hex[:16]).strip()
    with _conn(project) as c:
        existing = c.execute(
            "SELECT * FROM publication_evidence WHERE publication_id=?",
            (publication_id,),
        ).fetchone()
        if existing:
            out = _publication_row(existing)
            out["idempotent"] = True
            return out
        c.execute(
            """INSERT INTO publication_evidence
               (publication_id, source_project, source_repo, source_sha, public_repo,
                public_ref, public_sha, public_tag, script, guard_status, guard_json,
                artifact_url, task_id, claim_id, agent_id, actor, principal_id,
                published_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                publication_id, normalized["source_project"], normalized["source_repo"],
                normalized["source_sha"], normalized["public_repo"], normalized["public_ref"],
                normalized["public_sha"], normalized["public_tag"], normalized["script"],
                normalized["guard_status"], json.dumps(normalized["guard"], sort_keys=True),
                normalized["artifact_url"], normalized["task_id"], normalized["claim_id"],
                normalized["agent_id"], actor, normalized["principal_id"],
                normalized["published_at"], now, now,
            ),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (normalized["task_id"], actor, "publication.recorded",
                   json.dumps({"publication_id": publication_id,
                               "source_project": normalized["source_project"],
                               "source_repo": normalized["source_repo"],
                               "source_sha": normalized["source_sha"],
                               "public_repo": normalized["public_repo"],
                               "public_ref": normalized["public_ref"],
                               "public_sha": normalized["public_sha"],
                               "public_tag": normalized["public_tag"],
                               "script": normalized["script"],
                               "guard_status": normalized["guard_status"],
                               "artifact_url": normalized["artifact_url"],
                               "evidence_only": True}, sort_keys=True), now))
        row = c.execute("SELECT * FROM publication_evidence WHERE publication_id=?",
                        (publication_id,)).fetchone()
    return _publication_row(row)


def list_publication_evidence(task_id: str = "", source_project: str = "",
                              source_sha: str = "", public_repo: str = "",
                              project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    init_db(project)
    q = "SELECT * FROM publication_evidence WHERE 1=1"
    params: List[Any] = []
    if task_id:
        q += " AND task_id=?"; params.append(task_id.strip().upper())
    if source_project:
        q += " AND source_project=?"; params.append(source_project.strip())
    if source_sha:
        q += " AND source_sha=?"; params.append(source_sha.strip().lower())
    if public_repo:
        q += " AND public_repo=?"; params.append(public_repo.strip())
    q += " ORDER BY updated_at DESC, publication_id"
    with _conn(project) as c:
        return [_publication_row(row) for row in c.execute(q, params).fetchall()]


def _publication_summary(rows: List[Dict[str, Any]],
                         source_sha: str = "") -> Dict[str, Any]:
    matched = rows
    if source_sha:
        matched = [r for r in rows if _sha_matches(r.get("source_sha") or "", source_sha)]
    passed = [r for r in matched if r.get("guard_status") == "passed"]
    failed = [r for r in matched if r.get("guard_status") == "failed"]
    latest = matched[0] if matched else (rows[0] if rows else None)
    if passed:
        status = "published"
        selected = passed[0]
    elif matched and failed:
        status = "failed"
        selected = latest
    elif matched:
        status = "unknown"
        selected = latest
    elif source_sha and rows:
        status = "stale"
        selected = latest
    else:
        status = "missing"
        selected = None
    return {
        "status": status,
        "passed": bool(passed),
        "required": False,
        "source_repo": (selected or {}).get("source_repo"),
        "source_sha": source_sha or ((selected or {}).get("source_sha") if selected else None),
        "public_repo": (selected or {}).get("public_repo"),
        "public_ref": (selected or {}).get("public_ref"),
        "public_sha": (selected or {}).get("public_sha"),
        "public_tag": (selected or {}).get("public_tag"),
        "script": (selected or {}).get("script"),
        "guard_status": (selected or {}).get("guard_status"),
        "artifact_url": (selected or {}).get("artifact_url"),
        "published_at": (selected or {}).get("published_at"),
        "publication_count": len(matched),
        "total_publication_count": len(rows),
        "latest": selected,
        "runs": rows[:5],
        "repo_role": "public",
        "evidence_only": True,
    }


def _task_publication_summary_in(c: sqlite3.Connection, task_id: str,
                                 source_sha: str = "") -> Dict[str, Any]:
    rows = [
        _publication_row(row)
        for row in c.execute(
            "SELECT * FROM publication_evidence WHERE task_id=? "
            "ORDER BY updated_at DESC, publication_id",
            (task_id,),
        ).fetchall()
    ]
    return _publication_summary(rows, source_sha=source_sha)


def task_publication_summary(task_id: str, source_sha: str = "",
                             project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    init_db(project)
    with _conn(project) as c:
        return _task_publication_summary_in(c, task_id, source_sha=source_sha)


def _publication_required_from(task: Dict[str, Any],
                               evidence: Optional[Dict[str, Any]] = None) -> bool:
    evidence = evidence or {}
    if evidence.get("publication_required") is True or evidence.get("publish_required") is True:
        return True
    gates = evidence.get("required_gates") or evidence.get("review_gates") or []
    if isinstance(gates, str):
        gates = coerce_csv_list(gates)
    wanted = {"publication", "publication_evidence", "publish_evidence",
              "public_mirror_published", "release_evidence"}
    if any(str(g).strip().lower() in wanted for g in gates):
        return True
    state = task.get("agent_state") or {}
    for key in ("review_gate", "review_gates", "proof_requirements"):
        value = state.get(key) or {}
        if isinstance(value, dict) and (
                value.get("publication_required")
                or value.get("publication_evidence")
                or value.get("publish_evidence")):
            return True
    text = "\n".join(str(task.get(k) or "") for k in (
        "entry_criteria", "exit_criteria", "deliverable"))
    lowered = text.lower()
    return (
        "publication_evidence" in lowered
        or "public_mirror_published" in lowered
        or "publish evidence" in lowered
        or "release evidence" in lowered
    )


def _publication_review_gate(task: Dict[str, Any],
                             evidence: Optional[Dict[str, Any]] = None,
                             c: Optional[sqlite3.Connection] = None,
                             project: str = DEFAULT_PROJECT,
                             summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    evidence = evidence or {}
    git_state = task.get("git_state") or {}
    source_sha = (
        evidence.get("publication_source_sha")
        or evidence.get("source_sha")
        or evidence.get("head_sha")
        or git_state.get("merged_sha")
        or git_state.get("head_sha")
        or ""
    )
    if summary is not None:
        summary = dict(summary)
    elif c is None:
        with _conn(project) as own:
            summary = _task_publication_summary_in(own, task["task_id"], source_sha=source_sha)
    else:
        summary = _task_publication_summary_in(c, task["task_id"], source_sha=source_sha)
    required = _publication_required_from(task, evidence)
    summary["required"] = required
    summary["gate"] = {
        "name": "publication_evidence",
        "required": required,
        "passed": summary["passed"],
        "status": (
            "passed" if summary["passed"] else
            "blocked" if required else
            "not_required"
        ),
        "message": (
            "Public mirror publication evidence passed for this source SHA."
            if summary["passed"] else
            "Public mirror publication evidence is required before publish/release review."
            if required else
            "Public mirror publication evidence is optional for this task."
        ),
    }
    return summary


def _publication_reconcile_findings(tasks: List[Dict[str, Any]],
                                    git_states: Dict[str, Dict[str, Any]],
                                    project: str = DEFAULT_PROJECT) -> Tuple[
                                        List[Dict[str, Any]],
                                        Dict[str, Any],
                                    ]:
    findings: List[Dict[str, Any]] = []
    checked = 0
    stale = 0
    missing = 0
    with _conn(project) as c:
        for task in tasks:
            task_id = task["task_id"]
            state = git_states.get(task_id, {})
            source_sha = state.get("merged_sha") or state.get("head_sha") or ""
            summary = _task_publication_summary_in(c, task_id, source_sha=source_sha)
            checked += 1
            required = _publication_required_from(task, state.get("evidence") or {})
            if required and not summary.get("passed"):
                missing += 1
                findings.append({
                    "severity": "medium",
                    "task_id": task_id,
                    "code": "publication_evidence_missing",
                    "detail": (
                        "Task requires public mirror publication evidence, but no passed "
                        "publication record matches the current source SHA."
                    ),
                    "repo_role": "public",
                    "expected_source_sha": source_sha or None,
                    "failure_class": "missing_data",
                })
            if summary.get("status") != "stale":
                continue
            latest = summary.get("latest") or {}
            stale += 1
            findings.append({
                "severity": "medium",
                "task_id": task_id,
                "code": "publish_drift_stale_public_mirror",
                "detail": (
                    "Public mirror evidence is stale: latest publication points at "
                    f"{latest.get('source_sha') or 'unknown'} but current source SHA is "
                    f"{source_sha or 'unknown'}."
                ),
                "repo_role": "public",
                "public_repo": latest.get("public_repo") or "",
                "public_ref": latest.get("public_ref") or "",
                "latest_source_sha": latest.get("source_sha") or "",
                "expected_source_sha": source_sha or "",
                "failure_class": "stale_branch",
            })
    return findings, {
        "publication_evidence": "checked",
        "publication_tasks_checked": checked,
        "publication_missing_count": missing,
        "publication_stale_count": stale,
    }



class StorePublicationRepository:
    """Thin repository wrapper over module-level publication helpers."""

    def create_publication_evidence(self, data, actor="system", project=DEFAULT_PROJECT):
        return create_publication_evidence(data, actor=actor, project=project)

    def list_publication_evidence(self, project=DEFAULT_PROJECT, **kwargs):
        return list_publication_evidence(project=project, **kwargs)

    def task_publication_summary(self, task_id, project=DEFAULT_PROJECT):
        return task_publication_summary(task_id, project=project)


def default_publication_repository() -> StorePublicationRepository:
    return StorePublicationRepository()


__all__ = [
    "PUBLICATION_GUARD_STATUSES",
    "StorePublicationRepository",
    "default_publication_repository",
    "create_publication_evidence",
    "list_publication_evidence",
    "task_publication_summary",
    "_publication_row",
    "_validate_publication_guard_status",
    "_repo_mismatch",
    "_publication_topology_contract",
    "_publication_request_payload",
    "_publication_summary",
    "_task_publication_summary_in",
    "_publication_required_from",
    "_publication_review_gate",
    "_publication_reconcile_findings",
]
