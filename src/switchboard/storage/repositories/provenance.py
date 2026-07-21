"""Provenance / reconcile repository (ARCH-MS-34).

Owns git merge provenance persistence and reconcile/webhook coupling previously
planned for ``provenance_store.py`` / ``reconcile.py``: task_git_state helpers,
mark_task_pr_opened / mark_task_merged / offline Done, GitHub PR backstops, and
reconcile drift orchestration. Cross-cutting store helpers (write queue, meta,
activity, publication findings) are reached via ``_store_facade()`` during the
strangler. ``store.py`` re-exports these symbols; root ``provenance_store.py``
is a compatibility shim.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import orphan_merge_discovery
from constants import *  # noqa: F401,F403
from db.connection import _conn
from db.core import _slug_token
from switchboard.domain.provenance.git import (
    has_done_provenance as _has_done_provenance,
    offline_evidence_from_state as _offline_evidence_from_state,
    provenance_summary as _provenance_summary,
    valid_evidence_hash as _valid_evidence_hash,
)
from switchboard.domain.provenance.semantic import (
    merge_completion_evidence,
    merge_done_gate,
    semantic_completion_gate,
)
from switchboard.storage.repositories.tasks import (
    _heal_dependency_blocked_tasks_in,
    _task_row,
)


def _store_facade():
    """Resolve transitional store helpers after store.py is initialized."""
    import store
    return store


SEVERITY_VALUE = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _severity_value(severity: str) -> int:
    return SEVERITY_VALUE.get((severity or "").strip().lower(), 0)


RECONCILE_FAILURE_CLASS_BY_CODE = {
    "canonical_main_sha_not_found": "stale_branch",
    "claim_evidence_missing": "missing_data",
    "claim_without_evidence": "missing_data",
    "done_pr_not_merged": "hidden_fallback",
    "done_without_merged_sha": "hidden_fallback",
    "head_sha_not_found": "stale_branch",
    "merged_sha_mismatch": "invalid_input",
    "merged_sha_not_found": "stale_branch",
    "merged_sha_not_on_canonical_main": "stale_branch",
    "missing_canonical_main_sha": "missing_data",
    "publish_drift_stale_public_mirror": "stale_branch",
    "publication_evidence_missing": "missing_data",
    "progress_without_pushed_head": "missing_data",
    "pr_state_unavailable": "broken_connection",
    "review_without_provenance": "missing_data",
    "stale_file_lease": "failed_gate",
    "stale_resource_lease": "failed_gate",
    "stale_task_claim": "failed_gate",
}



def _reconcile_failure_class(code: str) -> str:
    return RECONCILE_FAILURE_CLASS_BY_CODE.get(
        _slug_token(code or ""), "failed_gate")


def _annotate_reconcile_finding(finding: Dict[str, Any]) -> Dict[str, Any]:
    failure_class = finding.get("failure_class") or _reconcile_failure_class(
        str(finding.get("code") or ""))
    detail = _store_facade()._failure_class_detail(failure_class) or {}
    annotated = dict(finding)
    annotated["failure_class"] = failure_class
    annotated["expected_signal"] = annotated.get(
        "expected_signal") or detail.get("expected_signal")
    return annotated


def _git_state_row(r: Optional[sqlite3.Row]) -> Dict[str, Any]:
    if not r:
        return {"branch": None, "head_sha": None, "pushed_at": None, "pr_number": None,
                "pr_url": None, "merged_sha": None, "merged_at": None,
                "in_main_content": False, "published_ref": None,
                "last_reconciled_at": None, "evidence": {}}
    d = dict(r)
    d["in_main_content"] = bool(d.get("in_main_content"))
    d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
    return d


def _load_git_state(c: sqlite3.Connection, task_id: str) -> Dict[str, Any]:
    state = _git_state_row(c.execute("SELECT * FROM task_git_state WHERE task_id=?",
                                     (task_id,)).fetchone())
    state["provenance_type"] = _provenance_summary(state)["type"]
    return state


def _git_states_by_task(c: sqlite3.Connection,
                        task_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load full git-state rows for many task ids in bounded queries."""
    if not task_ids:
        return {}
    by_id: Dict[str, sqlite3.Row] = {}
    chunk = 400  # stay well under SQLite's 999-variable limit
    for i in range(0, len(task_ids), chunk):
        batch = task_ids[i:i + chunk]
        placeholders = ",".join("?" * len(batch))
        for r in c.execute(
            f"SELECT * FROM task_git_state WHERE task_id IN ({placeholders})", batch
        ).fetchall():
            by_id[r["task_id"]] = r
    states: Dict[str, Dict[str, Any]] = {}
    for task_id in task_ids:
        state = _git_state_row(by_id.get(task_id))
        state["provenance_type"] = _provenance_summary(state)["type"]
        states[task_id] = state
    return states


def _provenance_by_task(c: sqlite3.Connection, task_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Batch equivalent of _provenance_summary(_load_git_state(c, id)) for many tasks:
    one query for all task_git_state rows instead of one per task. This is the board
    N+1 fix (HARDEN-34) — the whole-board list needs provenance for every card's
    Done-proof badge, and doing it per-task was ~1 query/task."""
    return {
        task_id: _provenance_summary(state)
        for task_id, state in _git_states_by_task(c, task_ids).items()
    }


def _parse_evidence(evidence: Any) -> Dict[str, Any]:
    if isinstance(evidence, dict):
        return dict(evidence)
    if not evidence:
        return {}
    if isinstance(evidence, str):
        try:
            parsed = json.loads(evidence)
            return parsed if isinstance(parsed, dict) else {"note": evidence}
        except Exception:
            return {"note": evidence}
    return {"value": evidence}


def _upsert_git_state(c: sqlite3.Connection, task_id: str,
                      updates: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    current = _load_git_state(c, task_id)
    evidence = dict(current.get("evidence") or {})
    if "evidence" in updates and isinstance(updates["evidence"], dict):
        evidence = merge_completion_evidence(evidence, updates.pop("evidence"))
    clean_updates = {k: v for k, v in updates.items() if v is not None}
    merged = {**current, **clean_updates}
    branch = merged.get("branch")
    head_sha = merged.get("head_sha")
    pushed_at = merged.get("pushed_at")
    pr_number = merged.get("pr_number")
    pr_url = merged.get("pr_url")
    # Derive pr_number from a recorded pr_url at write time (ADR-0006): every write path
    # then persists pr_number alongside pr_url, so reconcile can read it directly instead
    # of scraping evidence at check time (retires the PR-evidence hydration path).
    if not pr_number and pr_url:
        _pr_match = GITHUB_PR_URL_RE.search(str(pr_url))
        if _pr_match:
            pr_number = int(_pr_match.group(2))
    merged_sha = merged.get("merged_sha")
    merged_at = merged.get("merged_at")
    in_main = 1 if merged.get("in_main_content") else 0
    published_ref = merged.get("published_ref")
    last_reconciled_at = merged.get("last_reconciled_at")
    c.execute(
        "INSERT INTO task_git_state(task_id, branch, head_sha, pushed_at, pr_number, pr_url, "
        "merged_sha, merged_at, in_main_content, published_ref, last_reconciled_at, "
        "evidence_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(task_id) DO UPDATE SET branch=excluded.branch, head_sha=excluded.head_sha, "
        "pushed_at=excluded.pushed_at, pr_number=excluded.pr_number, pr_url=excluded.pr_url, "
        "merged_sha=excluded.merged_sha, merged_at=excluded.merged_at, "
        "in_main_content=excluded.in_main_content, published_ref=excluded.published_ref, "
        "last_reconciled_at=excluded.last_reconciled_at, evidence_json=excluded.evidence_json, "
        "updated_at=excluded.updated_at",
        (task_id, branch, head_sha, pushed_at, pr_number, pr_url, merged_sha, merged_at,
         in_main, published_ref, last_reconciled_at, json.dumps(evidence, sort_keys=True), now),
    )
    return _load_git_state(c, task_id)


def _same_pr_reference(current: Dict[str, Any], evidence_obj: Dict[str, Any]) -> bool:
    current_pr = current.get("pr_number")
    incoming_pr = evidence_obj.get("pr_number")
    if current_pr is not None and incoming_pr is not None and str(current_pr) == str(incoming_pr):
        return True
    current_url = (current.get("pr_url") or "").strip()
    incoming_url = (evidence_obj.get("pr_url") or "").strip()
    return bool(current_url and incoming_url and current_url == incoming_url)


def _preserve_provider_pr_evidence(current: Dict[str, Any],
                                   updates: Dict[str, Any],
                                   evidence_obj: Dict[str, Any]) -> Dict[str, Any]:
    """Keep webhook/GitHub PR evidence authoritative over later stale claim evidence."""
    if not _same_pr_reference(current, evidence_obj):
        return updates
    provider = {
        field: current.get(field)
        for field in ("branch", "head_sha", "pr_number", "pr_url")
        if current.get(field) not in (None, "")
    }
    if not provider:
        return updates
    claim_evidence = dict(evidence_obj)
    conflicts = {}
    for field, provider_value in provider.items():
        claim_value = evidence_obj.get(field)
        if claim_value not in (None, "") and str(claim_value) != str(provider_value):
            conflicts[field] = {"claim": claim_value, "provider": provider_value}
        updates[field] = provider_value
    if current.get("pushed_at"):
        updates["pushed_at"] = current.get("pushed_at")
    preserved_evidence = dict(evidence_obj)
    preserved_evidence.update(provider)
    if conflicts:
        preserved_evidence["claim_evidence"] = claim_evidence
        preserved_evidence["provider_evidence_preserved"] = {
            "source": "existing_pr_evidence",
            "conflicts": conflicts,
        }
    updates["evidence"] = preserved_evidence
    return updates


def mark_task_pr_opened(task_id: str, pr_number: int, pr_url: str = "",
                        branch: str = "", head_sha: str = "",
                        actor: str = "github-webhook",
                        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    # Retry the whole write on a transient sqlite lock so a busy DB never silently drops the
    # PR-open provenance event (dropped webhook -> stuck 'Not Started' task -> blocked claim gate).
    s = _store_facade()
    return s._write_through(project, lambda: s._mark_task_pr_opened_impl(
        task_id, pr_number, pr_url, branch, head_sha, actor, project))


def _mark_task_pr_opened_impl(task_id: str, pr_number: int, pr_url: str = "",
                              branch: str = "", head_sha: str = "",
                              actor: str = "github-webhook",
                              project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return {"error": "task not found", "task_id": task_id}
        current = _load_git_state(c, task_id)
        same_pr = (
            current.get("pr_number") == pr_number and
            (not pr_url or current.get("pr_url") == pr_url) and
            (not branch or current.get("branch") == branch) and
            (not head_sha or current.get("head_sha") == head_sha)
        )
        if row["status"] in ("In Review", "Done") and same_pr:
            task = _task_row(row)
            return {"task_id": task_id, "status": task["status"],
                    "git_state": current, "idempotent": True}
        if row["status"] == "Done":
            return {"task_id": task_id, "status": "Done", "git_state": current,
                    "skipped": True, "reason": "task_already_done"}
        c.execute("UPDATE tasks SET status='In Review', updated_at=? WHERE task_id=? "
                  "AND status NOT IN ('Done', 'Cancelled', 'Canceled')",
                  (now, task_id))
        git_state = _upsert_git_state(c, task_id, {
            "branch": branch or None,
            "head_sha": head_sha or None,
            "pushed_at": now if head_sha else None,
            "pr_number": pr_number,
            "pr_url": pr_url or None,
            "evidence": {"pr_number": pr_number, "pr_url": pr_url,
                         "branch": branch, "head_sha": head_sha},
        })
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "git.pr_opened",
                   json.dumps({"pr_number": pr_number, "pr_url": pr_url,
                               "branch": branch, "head_sha": head_sha}, sort_keys=True), now))
    return {"task_id": task_id, "status": "In Review", "git_state": git_state}


def mark_task_merged(task_id: str, merged_sha: str, pr_number: Optional[int] = None,
                     pr_url: str = "", branch: str = "", head_sha: str = "",
                     actor: str = "github-webhook",
                     project: str = DEFAULT_PROJECT,
                     provenance_source: str = "",
                     task_ids_found: Any = None) -> Dict[str, Any]:
    # Retry the whole write on a transient sqlite lock so a busy DB never silently drops the
    # merge provenance event (dropped webhook -> task stuck In Review instead of Done).
    s = _store_facade()
    return s._write_through(project, lambda: s._mark_task_merged_impl(
        task_id, merged_sha, pr_number, pr_url, branch, head_sha, actor, project,
        provenance_source, task_ids_found))


def _mark_task_merged_impl(task_id: str, merged_sha: str, pr_number: Optional[int] = None,
                           pr_url: str = "", branch: str = "", head_sha: str = "",
                           actor: str = "github-webhook",
                           project: str = DEFAULT_PROJECT,
                           provenance_source: str = "",
                           task_ids_found: Any = None) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return {"error": "task not found", "task_id": task_id}
        current = _load_git_state(c, task_id)
        task = _task_row(row)
        semantic_gate = semantic_completion_gate(task, current.get("evidence") or {})
        if not semantic_gate.get("ok"):
            c.execute("UPDATE tasks SET status='Blocked', updated_at=? WHERE task_id=?",
                      (now, task_id))
            git_state = _upsert_git_state(c, task_id, {
                "branch": branch or None,
                "head_sha": head_sha or None,
                "pushed_at": now if head_sha else None,
                "pr_number": pr_number,
                "pr_url": pr_url or None,
                "merged_sha": merged_sha,
                "merged_at": now,
                "in_main_content": True,
                "evidence": {
                    "merged_sha": merged_sha,
                    "pr_number": pr_number,
                    "pr_url": pr_url,
                    "branch": branch,
                    "head_sha": head_sha,
                    **({"source": provenance_source} if provenance_source else {}),
                    **({"task_ids_found": task_ids_found} if task_ids_found else {}),
                },
            })
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (task_id, actor, "git.pr_merged_semantic_blocked",
                       json.dumps({"merged_sha": merged_sha, "pr_number": pr_number,
                                   "pr_url": pr_url, "semantic_gate": semantic_gate},
                                  sort_keys=True), now))
            return {"task_id": task_id, "status": "Blocked", "git_state": git_state,
                    "semantic_gate": semantic_gate, "merged": True}
        merge_evidence = {
            **(current.get("evidence") or {}),
            "merged_sha": merged_sha,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "branch": branch,
            "head_sha": head_sha,
            **({"source": provenance_source} if provenance_source else {}),
            **({"task_ids_found": task_ids_found} if task_ids_found else {}),
        }
        done_gate = merge_done_gate(task, merge_evidence)
        target_status = "Done" if done_gate.get("ok") else "In Review"
        same_merge = (
            row["status"] == target_status and
            current.get("merged_sha") == merged_sha and
            (pr_number is None or current.get("pr_number") == pr_number) and
            (not pr_url or current.get("pr_url") == pr_url) and
            (not branch or current.get("branch") == branch) and
            (not head_sha or current.get("head_sha") == head_sha)
        )
        if same_merge:
            return {"task_id": task_id, "status": target_status,
                    "git_state": current, "idempotent": True,
                    "merge_done_gate": done_gate}
        c.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                  (target_status, now, task_id))
        git_state = _upsert_git_state(c, task_id, {
            "branch": branch or None,
            "head_sha": head_sha or None,
            "pushed_at": now if head_sha else None,
            "pr_number": pr_number,
            "pr_url": pr_url or None,
            "merged_sha": merged_sha,
            "merged_at": now,
            "in_main_content": True,
            "evidence": {
                "merged_sha": merged_sha,
                "pr_number": pr_number,
                "pr_url": pr_url,
                "branch": branch,
                "head_sha": head_sha,
                **({"source": provenance_source} if provenance_source else {}),
                **({"task_ids_found": task_ids_found} if task_ids_found else {}),
                **({"merged_evidence_only": True} if not done_gate.get("ok") else {}),
            },
        })
        activity_kind = ("git.pr_merged" if done_gate.get("ok")
                         else "git.pr_merged_evidence")
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, activity_kind,
                   json.dumps({"merged_sha": merged_sha, "pr_number": pr_number,
                               "pr_url": pr_url, "merge_done_gate": done_gate},
                              sort_keys=True), now))
        if target_status == "Done":
            _heal_dependency_blocked_tasks_in(
                c, completed_task_id=task_id, actor="switchboard/dependency-lifecycle",
                now=now)
    return {"task_id": task_id, "status": target_status, "git_state": git_state,
            "merge_done_gate": done_gate, "merged": True}


def mark_task_default_branch_commit(task_id: str, commit_sha: str,
                                    branch: str = "master", subject: str = "",
                                    actor: str = "default-branch-backfill",
                                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Manual/bootstrap provenance repair for a task already on the default branch.

    ADR-0006 retired the *automated* default-branch backfill path (the push-webhook
    and reconcile no longer call this); every default-branch commit is a merged PR the
    orphan sweep stamps. This low-level primitive remains as a manual escape hatch for
    pre-flow/bootstrap commits, with no automated caller. It only marks In Review tasks Done.
    """
    if not commit_sha:
        return {"error": "commit_sha required", "task_id": task_id}
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return {"error": "task not found", "task_id": task_id}
        if row["status"] == "Done":
            return {"skipped": True, "reason": "already_done", "task_id": task_id}
        if row["status"] != "In Review":
            return {"skipped": True, "reason": "status_not_in_review",
                    "task_id": task_id, "status": row["status"]}
        current = _load_git_state(c, task_id)
        semantic_gate = semantic_completion_gate(_task_row(row), current.get("evidence") or {})
        if not semantic_gate.get("ok"):
            c.execute("UPDATE tasks SET status='Blocked', updated_at=? WHERE task_id=?",
                      (now, task_id))
            git_state = _upsert_git_state(c, task_id, {
                "branch": branch or None,
                "head_sha": commit_sha,
                "pushed_at": now,
                "merged_sha": commit_sha,
                "merged_at": now,
                "in_main_content": True,
                "evidence": {"source": "default_branch_backfill", "commit_sha": commit_sha,
                             "branch": branch, "subject": subject},
            })
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (task_id, actor, "git.default_branch_semantic_blocked",
                       json.dumps({"commit_sha": commit_sha, "semantic_gate": semantic_gate},
                                  sort_keys=True), now))
            return {"task_id": task_id, "status": "Blocked", "git_state": git_state,
                    "semantic_gate": semantic_gate, "merged": True}
        c.execute("UPDATE tasks SET status='Done', updated_at=? WHERE task_id=?",
                  (now, task_id))
        evidence = {"source": "default_branch_backfill", "commit_sha": commit_sha,
                    "branch": branch, "subject": subject}
        git_state = _upsert_git_state(c, task_id, {
            "branch": branch or None,
            "head_sha": commit_sha,
            "pushed_at": now,
            "merged_sha": commit_sha,
            "merged_at": now,
            "in_main_content": True,
            "evidence": evidence,
        })
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "git.default_branch_backfilled",
                   json.dumps(evidence, sort_keys=True), now))
        _heal_dependency_blocked_tasks_in(
            c, completed_task_id=task_id, actor="switchboard/dependency-lifecycle",
            now=now)
    return {"task_id": task_id, "status": "Done", "git_state": git_state}


def mark_task_offline_done(task_id: str, evidence: Any = None,
                           artifact_url: str = "", evidence_hash: str = "",
                           verifier: str = "", reviewed_at: Optional[float] = None,
                           actor: str = "switchboard/operator",
                           project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Verify a non-PR/offline task as Done with explicit operator evidence.

    Agents still complete claims to In Review. This path is intentionally separate: a
    verifier/system actor reviews evidence and stamps a non-code provenance record so
    Done means "verified outcome" instead of "agent asked nicely."
    """
    now = time.time()
    evidence_obj = _parse_evidence(evidence)
    artifact_url = (artifact_url or evidence_obj.get("artifact_url") or "").strip()
    evidence_hash = (evidence_hash or evidence_obj.get("evidence_hash") or "").strip()
    verifier = (verifier or evidence_obj.get("verifier") or actor or "").strip()
    if not evidence_obj and not artifact_url and not evidence_hash:
        return {"error": "offline evidence required", "task_id": task_id}
    if evidence_hash and not _valid_evidence_hash(evidence_hash):
        return {
            "error": "invalid_evidence_hash",
            "task_id": task_id,
            "message": "evidence_hash must be a 64-character SHA-256 hex digest, optionally prefixed with sha256:",
        }
    if not evidence_hash and evidence_obj:
        evidence_hash = hashlib.sha256(
            json.dumps(evidence_obj, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    try:
        reviewed = float(reviewed_at) if reviewed_at not in (None, "") else now
    except (TypeError, ValueError):
        return {"error": "reviewed_at must be a unix timestamp", "task_id": task_id}
    offline_payload = {
        "provenance_type": "offline_evidence",
        "evidence": evidence_obj,
        "artifact_url": artifact_url or None,
        "evidence_hash": evidence_hash or None,
        "verifier": verifier,
        "reviewed_at": reviewed,
        "source": "offline_verifier",
    }
    with _conn(project) as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return {"error": "task not found", "task_id": task_id}
        current = _load_git_state(c, task_id)
        if row["status"] == "Done":
            existing_offline = _offline_evidence_from_state(current)
            if existing_offline:
                if existing_offline == offline_payload:
                    return {"task_id": task_id, "status": "Done", "git_state": current,
                            "provenance": _provenance_summary(current), "idempotent": True}
                corrected_payload = {
                    **offline_payload,
                    "corrects": existing_offline,
                    "corrected_at": now,
                }
                git_state = _upsert_git_state(c, task_id, {
                    "evidence": {"offline_evidence": corrected_payload},
                })
                c.execute(
                    "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                    (task_id, actor, "task.offline_evidence_corrected",
                     json.dumps({"previous": existing_offline, "current": corrected_payload},
                                sort_keys=True), now),
                )
                return {"task_id": task_id, "status": "Done", "git_state": git_state,
                        "provenance": _provenance_summary(git_state), "corrected": True}
            if current.get("merged_sha"):
                return {"skipped": True, "reason": "already_done_with_git_provenance",
                        "task_id": task_id, "git_state": current}
        if row["status"] != "In Review":
            return {"error": "offline_done_requires_in_review", "task_id": task_id,
                    "status": row["status"],
                    "message": "Offline Done verification requires the task to be In Review first."}
        semantic_gate = semantic_completion_gate(_task_row(row), evidence_obj)
        if not semantic_gate.get("ok"):
            return {"error": "semantic_completion_failed", "task_id": task_id,
                    "status": row["status"], "semantic_gate": semantic_gate,
                    "message": semantic_gate.get("message")}
        c.execute("UPDATE tasks SET status='Done', updated_at=? WHERE task_id=?", (now, task_id))
        git_state = _upsert_git_state(c, task_id, {
            "evidence": {"offline_evidence": offline_payload},
        })
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "task.offline_verified",
                   json.dumps(offline_payload, sort_keys=True), now))
        _heal_dependency_blocked_tasks_in(
            c, completed_task_id=task_id, actor="switchboard/dependency-lifecycle",
            now=now)
    return {"task_id": task_id, "status": "Done", "git_state": git_state,
            "provenance": _provenance_summary(git_state)}


def github_webhook_deliveries(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Local webhook-delivery evidence for a project's board (UI-15 Verify button).

    Any activity row written by the 'github-webhook' actor proves GitHub actually
    reached this board through the pinned ?project= payload URL, so the association
    panel 'flips green on first delivery' without needing GitHub API credentials —
    the same board-internal signal reconcile trusts over remote scraping.
    """
    with _conn(project) as c:
        row = c.execute(
            "SELECT COUNT(*) AS n, MAX(created_at) AS last FROM activity WHERE actor=?",
            ("github-webhook",),
        ).fetchone()
        count = int(row["n"] or 0) if row else 0
        last_at = float(row["last"]) if row and row["last"] else None
        last_kind = None
        if count:
            latest = c.execute(
                "SELECT kind FROM activity WHERE actor=? ORDER BY id DESC LIMIT 1",
                ("github-webhook",),
            ).fetchone()
            last_kind = latest["kind"] if latest else None
    return {"delivered": count > 0, "delivery_count": count,
            "last_delivery_at": last_at, "last_delivery_event": last_kind}


def update_canonical_main_sha(sha: str, actor: str = "github-webhook",
                              project: str = DEFAULT_PROJECT) -> None:
    if not sha:
        return
    _store_facade().set_meta("canonical_main_sha", sha, project=project)
    _store_facade().append_activity("git.main_advanced", actor, {"canonical_main_sha": sha},
                    task_id=None, project=project)


def _repo_root() -> str:
    configured = (os.environ.get("PM_REPO_PATH") or "").strip()
    if configured:
        return os.path.abspath(configured)
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    )


def _git_ok(args: List[str], timeout: float = 5) -> bool:
    try:
        return subprocess.run(["git", *args], cwd=_repo_root(),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout).returncode == 0
    except Exception:
        return False


def _git_fetch_origin(timeout: float = 45) -> bool:
    """Best-effort `git fetch origin` so canonical-main ancestry checks are not blinded
    by a stale checkout. Deploy is the only thing that fetches the box's checkout, but
    canonical main advances on every merge — so between deploys the recorded
    canonical_main_sha is routinely absent from the local object database, permanently
    blocking the per-task merged_sha reachability checks. A single bounded fetch before
    giving up keeps the git-reachability backstop live without a redeploy."""
    return _store_facade()._git_ok(["fetch", "--quiet", "origin"], timeout=timeout)


def _git_checks_available() -> bool:
    return _store_facade()._git_ok(["rev-parse", "--is-inside-work-tree"])


def _github_repo_from_git_url(url: str) -> str:
    clean = (url or "").strip()
    if not clean:
        return ""
    match = re.search(r"github\.com[:/]([^/\s:]+)/([^/\s]+)", clean, re.I)
    if not match:
        return ""
    repo = f"{match.group(1)}/{match.group(2)}"
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo.strip()


def _github_repo_from_pr_url(url: str) -> str:
    match = GITHUB_PR_URL_RE.search((url or "").strip())
    return match.group(1) if match else ""


def _normalize_repo_slug(repo: str) -> str:
    clean = _github_repo_from_git_url(repo) or (repo or "").strip()
    if clean.endswith(".git"):
        clean = clean[:-4]
    return clean.lower()


def _local_github_repo() -> str:
    try:
        remote = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=_repo_root(),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
    except Exception:
        return ""
    return _github_repo_from_git_url(remote)


def _github_pr(repo: str, pr_number: int, token: str = "") -> Optional[Dict[str, Any]]:
    if not repo or not pr_number:
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/pulls/{int(pr_number)}")
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _github_prs_graphql(pr_keys: List[Tuple[str, int]], token: str = "") -> Dict[Tuple[str, int], Dict[str, Any]]:
    """Fetch many PR records with one GitHub GraphQL request.

    Returns REST-shaped PR dictionaries so reconcile's provenance logic stays shared
    with the unauthenticated REST fallback and existing tests.
    """
    if not token or not pr_keys:
        return {}
    query_parts = []
    alias_to_key: Dict[str, Tuple[str, int]] = {}
    for idx, (repo, pr_number) in enumerate(pr_keys):
        if not repo or "/" not in repo or not pr_number:
            continue
        owner, name = repo.split("/", 1)
        alias = f"pr_{idx}"
        alias_to_key[alias] = (repo, int(pr_number))
        query_parts.append(
            f"""{alias}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{
              pullRequest(number: {int(pr_number)}) {{
                number
                url
                title
                merged
                mergedAt
                mergeCommit {{ oid }}
                baseRefName
                baseRepository {{ defaultBranchRef {{ name }} }}
                headRefName
                headRefOid
              }}
            }}"""
        )
    if not query_parts:
        return {}
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": "query ReconcilePullRequests {\n" + "\n".join(query_parts) + "\n}"}).encode(),
        method="POST",
    )
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            payload = json.loads(r.read().decode())
    except Exception:
        return {}
    if payload.get("errors") or not isinstance(payload.get("data"), dict):
        return {}
    fetched: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for alias, key in alias_to_key.items():
        node = ((payload["data"].get(alias) or {}).get("pullRequest") or {})
        if not node:
            continue
        fetched[key] = {
            "number": node.get("number"),
            "html_url": node.get("url") or "",
            "title": node.get("title") or "",
            "merged_at": node.get("mergedAt") if node.get("merged") else None,
            "merge_commit_sha": (node.get("mergeCommit") or {}).get("oid") or "",
            "base": {
                "ref": node.get("baseRefName") or "",
                "repo": {
                    "default_branch": (
                        (node.get("baseRepository") or {}).get("defaultBranchRef") or {}
                    ).get("name") or ""
                },
            },
            "head": {
                "ref": node.get("headRefName") or "",
                "sha": node.get("headRefOid") or "",
            },
        }
    return fetched


def _fetch_github_prs(pr_keys: List[Tuple[str, int]], token: str = "") -> Tuple[
        Dict[Tuple[str, int], Optional[Dict[str, Any]]], Dict[str, Any]]:
    ordered_keys = sorted({(repo, int(pr_number)) for repo, pr_number in pr_keys if repo and pr_number})
    checks: Dict[str, Any] = {"github_pr_fetches": len(ordered_keys)}
    if not ordered_keys:
        return {}, checks
    rest_helper_is_original = getattr(_github_pr, "__module__", __name__) == __name__
    use_graphql = (
        token
        and rest_helper_is_original
        and os.environ.get("PM_RECON_GITHUB_GRAPHQL", "1").strip().lower()
        not in ("0", "false", "no", "off")
    )
    fetched = _store_facade()._github_prs_graphql(ordered_keys, token=token) if use_graphql else {}
    missing = [key for key in ordered_keys if key not in fetched]
    if fetched:
        checks["github_pr_fetch_mode"] = "graphql"
        checks["github_pr_graphql_queries"] = 1
        checks["github_pr_graphql_fetches"] = len(fetched)
    if missing:
        checks["github_pr_rest_fallback_fetches"] = len(missing)
        concurrency = min(16, max(1, int(os.environ.get(
            "PM_RECON_GITHUB_CONCURRENCY", "8"))))
        with ThreadPoolExecutor(max_workers=min(concurrency, len(missing))) as pool:
            fallback = dict(zip(
                missing,
                pool.map(lambda key: _store_facade()._github_pr(key[0], key[1], token=token), missing),
            ))
        fetched.update(fallback)
        checks["github_pr_concurrency"] = min(concurrency, len(missing))
        if "github_pr_fetch_mode" not in checks:
            checks["github_pr_fetch_mode"] = "rest"
        else:
            checks["github_pr_fetch_mode"] = "graphql_with_rest_fallback"
    return fetched, checks


def _github_token() -> str:
    return (
        os.environ.get("PM_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("SWITCHBOARD_CI_GITHUB_TOKEN")
        or ""
    ).strip()


def _github_merged_prs(repo: str, token: str = "", limit: int = 30) -> List[Dict[str, Any]]:
    """Most recently updated closed PRs on the repo, merged ones only (newest first)."""
    if not repo or limit <= 0:
        return []
    per_page = max(1, min(int(limit), 100))
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls"
        f"?state=closed&sort=updated&direction=desc&per_page={per_page}")
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            page = json.loads(r.read().decode())
    except Exception:
        return []
    if not isinstance(page, list):
        return []
    return [pr for pr in page if pr.get("merged_at")]


def _retire_branches_enabled() -> bool:
    """Feature flag: retire (archive+delete) a PR's head branch after merge. Off by
    default so it ships dark and is enabled per deployment once the GitHub token has
    contents:write on the target canonical repo(s)."""
    return (os.environ.get("PM_RETIRE_MERGED_BRANCHES") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _github_write(method: str, repo: str, path: str, token: str = "",
                  data: Optional[Dict[str, Any]] = None):
    """Authenticated GitHub write (POST/DELETE). Returns (status_code, body_or_error).
    Mirrors the read helpers above; kept tiny and dependency-free for testability."""
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/{path}", data=body, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode() or ""
            return (getattr(r, "status", None) or r.getcode()), (json.loads(raw) if raw else None)
    except Exception as e:  # HTTPError exposes .code/.read(); other errors -> code 0
        code = getattr(e, "code", 0) or 0
        try:
            detail = e.read().decode()[:300]
        except Exception:
            detail = str(e)
        return code, {"error": detail or "error"}


def retire_merged_branch(repo: str, branch: str, head_sha: str = "",
                         project: str = "", actor: str = "github-webhook") -> Dict[str, Any]:
    """Archive-then-delete a merged PR head branch so merged branches stop piling up (BUG-29).

    OFF unless PM_RETIRE_MERGED_BRANCHES is set. ALWAYS archives before deleting: creates
    refs/tags/archive/<branch> at the branch head, and only deletes refs/heads/<branch> when
    that tag exists -- so the branch is always recoverable (`git checkout -b <branch>
    archive/<branch>`) and is NEVER deleted when archiving fails. Fail-visible: GitHub errors
    are returned in the result (surfaced in the webhook response), never masked as success."""
    if not _retire_branches_enabled():
        return {"retired": False, "reason": "disabled"}
    if not repo or "/" not in repo or not branch:
        return {"retired": False, "reason": "missing_repo_or_branch"}
    if not head_sha:
        return {"retired": False, "reason": "no_head_sha_cannot_archive",
                "repo": repo, "branch": branch}
    token = _store_facade()._github_token()
    if not token:
        return {"retired": False, "reason": "no_github_token"}
    out: Dict[str, Any] = {"repo": repo, "branch": branch}
    acode, ainfo = _store_facade()._github_write(
        "POST", repo, "git/refs", token,
        {"ref": f"refs/tags/archive/{branch}", "sha": head_sha})
    out["archived"] = acode in (200, 201, 422)  # 422 == archive tag already exists
    out["archive_status"] = acode
    if not out["archived"]:
        out["retired"] = False
        out["error"] = f"archive_failed:{acode}"
        out["detail"] = ainfo
        return out  # never delete a branch we could not archive
    dcode, dinfo = _store_facade()._github_write("DELETE", repo, f"git/refs/heads/{branch}", token)
    if dcode in (200, 204, 404, 422):  # 404/422 == branch already gone
        out["deleted"] = True
        out["already_gone"] = dcode in (404, 422)
    else:
        out["deleted"] = False
        out["error"] = f"delete_failed:{dcode}"
        out["detail"] = dinfo
    out["retired"] = bool(out.get("deleted"))
    return out


def _activity_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return " ".join(_activity_text(v) for v in payload.values())
    if isinstance(payload, list):
        return " ".join(_activity_text(v) for v in payload)
    if payload is None:
        return ""
    return str(payload)


def _pr_references_task(pr: Dict[str, Any], task_id: str) -> bool:
    """True when a merged PR explicitly names the task in its branch ref or title.

    Guards the In Progress auto-promote path against mis-association: an In Progress
    task's own activity may reference another task's PR (coordination chatter), and we
    must never auto-stamp Done off that. We only promote when the PR itself carries the
    task id, matching the branch/commit naming convention (cursor/<TASK-ID>-slug,
    "<TASK-ID>: subject"). In Review/Done keep their existing behaviour (the agent
    explicitly asserted the PR by advancing lifecycle), so this check does not apply there.
    """
    if not task_id:
        return False
    token = re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(task_id) + r"(?![A-Za-z0-9])", re.I)
    head_ref = (pr.get("head") or {}).get("ref") or ""
    title = pr.get("title") or ""
    return bool(token.search(head_ref) or token.search(title))


def _has_immutable_canonical_merge(task: Dict[str, Any], state: Dict[str, Any]) -> bool:
    """True once canonical merge provenance cannot change anymore."""
    return bool(
        task.get("status") == "Done"
        and state.get("merged_sha")
        and state.get("in_main_content")
        and state.get("provenance_type") in {
            "github_pr_merged", "default_branch_commit"
        }
    )


def _needs_live_pr_recheck(task: Dict[str, Any], state: Dict[str, Any]) -> bool:
    """Whether reconcile still needs GitHub's mutable PR view for this task.

    A canonical merge is immutable.  Once the webhook/reconcile path has recorded
    the merge SHA, proven it is in main, and labelled the provenance as a GitHub
    merge, polling that same PR every 15 minutes can no longer change the task's
    truth.  Keep live checks for every incomplete or non-terminal state so open,
    missing, manually edited, and offline provenance still fail visibly.
    """
    if not state.get("pr_number"):
        return False
    return not _has_immutable_canonical_merge(task, state)


def _external_reconcile_findings(tasks: List[Dict[str, Any]],
                                 git_states: Dict[str, Dict[str, Any]],
                                 canonical_main_sha: str,
                                 project: str = DEFAULT_PROJECT,
                                 run_discovery_backstops: bool = True) -> Tuple[
                                     List[Dict[str, Any]],
                                     Dict[str, Any],
                                     List[Dict[str, Any]],
                                 ]:
    findings: List[Dict[str, Any]] = []
    backfilled: List[Dict[str, Any]] = []
    checks: Dict[str, Any] = {
        "git_reachability": "not_configured",
        "github_prs": "not_configured",
    }
    repo = _store_facade().get_project_github_repo(project)
    if canonical_main_sha and _store_facade()._git_checks_available():
        local_repo = _local_github_repo()
        project_repo = _normalize_repo_slug(repo)
        local_repo_norm = _normalize_repo_slug(local_repo)
        if project_repo and not local_repo_norm:
            checks["git_reachability"] = "skipped_local_repo_unknown"
            checks["git_reachability_detail"] = (
                "Local git checkout remote could not be mapped to a GitHub repo; "
                "project-scoped GitHub checks still run when configured."
            )
            checks["project_repo"] = repo
        elif project_repo and local_repo_norm and project_repo != local_repo_norm:
            checks["git_reachability"] = "skipped_repo_mismatch"
            checks["git_reachability_detail"] = (
                "Local git checkout repo does not match the selected project's GitHub repo; "
                "skipping cat-file/merge-base to avoid cross-project false positives."
            )
            checks["local_repo"] = local_repo
            checks["project_repo"] = repo
        else:
            checks["git_reachability"] = "checked"
            if local_repo:
                checks["local_repo"] = local_repo
            main_ref = canonical_main_sha
            if not _store_facade()._git_ok(["cat-file", "-e", f"{main_ref}^{{commit}}"]):
                # Stale checkout: the box only fetches on deploy, but canonical main
                # advances on every merge, so the recorded sha is routinely absent
                # locally. Try one bounded fetch before declaring the backstop blind.
                if _store_facade()._git_fetch_origin():
                    checks["canonical_main_fetch"] = "refreshed"
            if not _store_facade()._git_ok(["cat-file", "-e", f"{main_ref}^{{commit}}"]):
                checks["git_reachability"] = "blocked_missing_canonical_main"
                checks["canonical_main_sha"] = main_ref
                findings.append({
                    "severity": "high",
                    "task_id": None,
                    "code": "canonical_main_sha_not_found",
                    "detail": (
                        "Canonical main SHA is not present in the local git object database "
                        "even after a refresh fetch; check the box's git remote/credentials."
                    ),
                })
            else:
                skipped_immutable_git = 0
                for task in tasks:
                    task_id = task["task_id"]
                    state = git_states.get(task_id, {})
                    state_repo = _github_repo_from_pr_url(state.get("pr_url") or "")
                    state_role = _store_facade().get_project_repo_role(state_repo, project) if state_repo else {}
                    if state_repo and not state_role.get("canonical"):
                        continue
                    # The webhook/reconcile merge stamp already proved this exact
                    # canonical merge is in main. Re-running cat-file + merge-base
                    # for every historical PR on every 15-minute cycle needlessly
                    # spawns hundreds of git processes and charges packfile cache to
                    # the small VM's reconcile cgroup.
                    if _has_immutable_canonical_merge(task, state):
                        skipped_immutable_git += 1
                        continue
                    for field, severity in (("head_sha", "medium"), ("merged_sha", "high")):
                        if (field == "head_sha" and task.get("status") == "Done"
                                and state.get("merged_sha")):
                            continue
                        if (field == "head_sha" and state.get("pr_number")
                                and task.get("status") in ("In Review", "Done")):
                            # Production checkouts do not need to fetch every PR head. GitHub PR
                            # state below is the source of truth for open/review heads; local git
                            # reachability remains authoritative for merged/default-branch SHAs.
                            continue
                        sha = state.get(field)
                        if not sha:
                            continue
                        if not _store_facade()._git_ok(["cat-file", "-e", f"{sha}^{{commit}}"]):
                            findings.append({"severity": severity, "task_id": task_id,
                                             "code": f"{field}_not_found",
                                             "detail": f"Recorded {field} is not present in the local git object database."})
                            continue
                        if field == "merged_sha" and not _store_facade()._git_ok(["merge-base", "--is-ancestor", sha, main_ref]):
                            findings.append({"severity": "high", "task_id": task_id,
                                             "code": "merged_sha_not_on_canonical_main",
                                             "detail": "Recorded merged_sha is not reachable from canonical main."})
                checks["git_checks_skipped_immutable"] = skipped_immutable_git

    token = _store_facade()._github_token()
    recorded_pr_tasks = [
        t for t in tasks if git_states.get(t["task_id"], {}).get("pr_number")
    ]
    pr_tasks = [
        t for t in recorded_pr_tasks
        if _needs_live_pr_recheck(t, git_states.get(t["task_id"], {}))
    ]
    checks["github_prs_skipped_immutable"] = len(recorded_pr_tasks) - len(pr_tasks)
    if repo:
        checks["github_repo"] = repo
        checks["github_prs"] = "checked" if token else "checked_unauthenticated"
    if repo and not pr_tasks:
        checks["github_prs"] = "configured_no_prs"
    pr_repos: List[str] = []
    if repo and pr_tasks:
        pr_repos = sorted({
            _github_repo_from_pr_url(git_states.get(t["task_id"], {}).get("pr_url") or "") or repo
            for t in pr_tasks
        })
    if pr_repos:
        checks["github_pr_repos"] = pr_repos
        # Prefer one GraphQL round-trip for all mutable PRs; fall back to bounded
        # REST reads for unauthenticated runs, partial GraphQL results, or tests.
        pr_keys = [
            (
                _github_repo_from_pr_url(
                    git_states.get(t["task_id"], {}).get("pr_url") or ""
                ) or repo,
                int(git_states.get(t["task_id"], {}).get("pr_number") or 0),
            )
            for t in pr_tasks
        ]
        fetched_prs, fetch_checks = _store_facade()._fetch_github_prs(pr_keys, token=token)
        checks.update(fetch_checks)
        for task in pr_tasks:
            state = git_states.get(task["task_id"], {})
            pr_repo = _github_repo_from_pr_url(state.get("pr_url") or "") or repo
            role_info = _store_facade().get_project_repo_role(pr_repo, project)
            pr = fetched_prs.get((pr_repo, int(state.get("pr_number") or 0)))
            if not pr:
                findings.append({"severity": "medium", "task_id": task["task_id"],
                                 "code": "pr_state_unavailable",
                                 "detail": f"Could not fetch recorded PR state from GitHub repo {pr_repo}."})
                continue
            merged = bool(pr.get("merged_at"))
            if not role_info.get("canonical"):
                findings.append({
                    "severity": "high" if merged or task.get("status") == "Done" else "medium",
                    "task_id": task["task_id"],
                    "code": "repo_role_cannot_mark_done",
                    "detail": (
                        f"Recorded PR is in repo role {role_info.get('role') or 'unknown'} "
                        f"({pr_repo}); only the project canonical repo can mark code work Done."
                    ),
                    "repo_role": role_info.get("role") or "unknown",
                    "repo": pr_repo,
                    "failure_class": "failed_gate",
                })
                continue
            if task.get("status") == "Done" and not merged:
                findings.append({"severity": "high", "task_id": task["task_id"],
                                 "code": "done_pr_not_merged",
                                 "detail": "Task is Done but the recorded GitHub PR is not merged."})
            merge_sha = pr.get("merge_commit_sha")
            base_ref = ((pr.get("base") or {}).get("ref") or "").strip()
            default_ref = (pr.get("base") or {}).get("repo", {}).get("default_branch") or ""
            default_branch_merge = bool(base_ref and default_ref and base_ref == default_ref)
            task_status = task.get("status")
            # In Progress tasks only reach here when a PR reference was hydrated from their
            # own activity/git_state (the agent merged without ever running complete_claim to
            # reach In Review). Auto-promote them ONLY when the PR actually merged into the
            # project's canonical default branch AND the PR itself names the task — never off a
            # feature/integration branch and never from a mis-attributed PR reference.
            stamp_eligible = (
                (task_status in ("In Review", "Done")
                 and (task_status != "Done" or not state.get("merged_sha")))
                or (task_status == "In Progress"
                    and default_branch_merge
                    and _pr_references_task(pr, task["task_id"]))
            )
            if merged and merge_sha and stamp_eligible:
                if default_branch_merge:
                    update_canonical_main_sha(merge_sha, "reconcile", project)
                stamped = _store_facade().mark_task_merged(
                    task["task_id"], merge_sha,
                    pr_number=int(state.get("pr_number") or 0) or None,
                    pr_url=state.get("pr_url") or pr.get("html_url") or "",
                    branch=((pr.get("head") or {}).get("ref") or state.get("branch") or ""),
                    head_sha=((pr.get("head") or {}).get("sha") or state.get("head_sha") or ""),
                    actor="reconcile",
                    project=project,
                )
                if not stamped.get("error"):
                    backfilled.append({
                        "task_id": task["task_id"],
                        "pr_number": state.get("pr_number"),
                        "merged_sha": merge_sha,
                    })
                    git_states[task["task_id"]] = stamped.get("git_state") or state
                    task["status"] = "Done"
                    state = git_states[task["task_id"]]
            if merged and state.get("merged_sha") and merge_sha and state["merged_sha"] != merge_sha:
                findings.append({"severity": "medium", "task_id": task["task_id"],
                                 "code": "merged_sha_mismatch",
                                 "detail": "Recorded merged_sha differs from GitHub PR merge_commit_sha."})

    if run_discovery_backstops:
        orphan_findings, orphan_backfilled, orphan_checks = _orphan_merge_discovery_findings(
            tasks, git_states, project=project, repo=repo, token=token)
        findings.extend(orphan_findings)
        backfilled.extend(orphan_backfilled)
        checks.update(orphan_checks)

        open_findings, open_advanced, open_checks = _open_pr_backstop_findings(
            tasks, git_states, project=project, repo=repo, token=token)
        findings.extend(open_findings)
        backfilled.extend(open_advanced)
        checks.update(open_checks)
    else:
        checks["orphan_merge_discovery"] = "deferred_to_full_reconcile"
        checks["open_pr_discovery"] = "deferred_to_full_reconcile"
    return findings, checks, backfilled


def _orphan_merge_discovery_findings(
    tasks: List[Dict[str, Any]],
    git_states: Dict[str, Dict[str, Any]],
    *,
    project: str,
    repo: str,
    token: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    import orphan_merge_discovery

    lookback_days = int(os.environ.get("PM_ORPHAN_MERGE_LOOKBACK_DAYS", "30") or "30")
    now = time.time()
    with _conn(project) as c:
        active_claims = {
            row["task_id"]: dict(row)
            for row in c.execute(
                "SELECT id, task_id, agent_id FROM task_claims "
                "WHERE status='active' AND expires_at>?",
                (now,),
            ).fetchall()
        }

    def _mark_merged(task_id: str, merged_sha: str, **kwargs: Any) -> Dict[str, Any]:
        return _store_facade().mark_task_merged(task_id, merged_sha, **kwargs)

    return orphan_merge_discovery.discover_orphan_merges(
        tasks,
        git_states,
        project=project,
        repo=repo or "",
        token=token,
        lookback_days=lookback_days,
        active_claims=active_claims,
        role_checker=lambda repo_slug: _store_facade().get_project_repo_role(repo_slug, project=project),
        mark_merged_fn=_mark_merged,
        append_activity_fn=_store_facade().append_activity,
        now=now,
    )


def _open_pr_backstop_findings(
    tasks: List[Dict[str, Any]],
    git_states: Dict[str, Dict[str, Any]],
    *,
    project: str,
    repo: str,
    token: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """BUG-28: advance pre-review tasks whose open-PR webhook was dropped. The
    open-PR twin of the merged orphan sweep; together they form reconcile's
    task-id-matched PR-discovery backstop (ADR-0006)."""
    import orphan_merge_discovery

    lookback_days = int(os.environ.get("PM_ORPHAN_MERGE_LOOKBACK_DAYS", "30") or "30")
    now = time.time()
    with _conn(project) as c:
        active_claims = {
            row["task_id"]: dict(row)
            for row in c.execute(
                "SELECT id, task_id, agent_id FROM task_claims "
                "WHERE status='active' AND expires_at>?",
                (now,),
            ).fetchall()
        }

    def _mark_pr_opened(task_id: str, pr_number: int, **kwargs: Any) -> Dict[str, Any]:
        return _store_facade().mark_task_pr_opened(task_id, pr_number, **kwargs)

    return orphan_merge_discovery.discover_open_prs(
        tasks,
        git_states,
        project=project,
        repo=repo or "",
        token=token,
        lookback_days=lookback_days,
        active_claims=active_claims,
        role_checker=lambda repo_slug: _store_facade().get_project_repo_role(repo_slug, project=project),
        mark_pr_opened_fn=_mark_pr_opened,
        append_activity_fn=_store_facade().append_activity,
        now=now,
    )


def _reconcile_signature(findings: List[Dict[str, Any]]) -> str:
    material = [{
        "severity": f.get("severity") or "",
        "task_id": f.get("task_id") or "",
        "code": f.get("code") or "",
        "failure_class": f.get("failure_class") or "",
        "detail": f.get("detail") or "",
    } for f in sorted(findings, key=lambda x: (
        x.get("task_id") or "", x.get("code") or "", x.get("severity") or ""))]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def _format_reconcile_alert(project: str, findings: List[Dict[str, Any]],
                            signature: str, limit: int = 12) -> str:
    lines = [
        f"Reconcile alert for project `{project}`: {len(findings)} actionable finding(s).",
        f"signature={signature}",
    ]
    for f in findings[:limit]:
        task = f.get("task_id") or "board"
        failure_class = f.get("failure_class") or "failed_gate"
        lines.append(
            f"- [{f.get('severity')}] {task} {f.get('code')} "
            f"({failure_class}): {f.get('detail')}"
        )
    if len(findings) > limit:
        lines.append(f"- ... {len(findings) - limit} more; run reconcile(project={project!r}) for full detail.")
    lines.append("Treat this as a Switchboard-owned drift interrupt: fix provenance, release stale claims, or document the exception.")
    return "\n".join(lines)


def _reconcile_cursor_key() -> str:
    return "reconcile.activity_cursor"


def _reconcile_activity_batch(
    c: sqlite3.Connection, since_cursor: int, limit: int,
) -> Tuple[set, int, bool]:
    """Consume at most ``limit`` historical activity rows from one indexed page."""
    bounded = max(1, min(int(limit), 1000))
    rows = c.execute(
        "SELECT id, task_id FROM activity WHERE id>? ORDER BY id LIMIT ?",
        (int(since_cursor), bounded + 1),
    ).fetchall()
    consumed = rows[:bounded]
    next_cursor = int(consumed[-1]["id"]) if consumed else int(since_cursor)
    task_ids = {str(row["task_id"]) for row in consumed if row["task_id"]}
    return task_ids, next_cursor, len(rows) > bounded


def _reconcile_task_page(
    c: sqlite3.Connection, after_task_id: str, limit: int,
) -> Tuple[List[sqlite3.Row], str, bool]:
    """Round-robin through task history without loading the whole table."""
    bounded = max(1, min(int(limit), 1000))
    rows = c.execute(
        "SELECT * FROM tasks WHERE task_id>? ORDER BY task_id LIMIT ?",
        (str(after_task_id or ""), bounded + 1),
    ).fetchall()
    if not rows and after_task_id:
        rows = c.execute(
            "SELECT * FROM tasks ORDER BY task_id LIMIT ?", (bounded + 1,),
        ).fetchall()
    consumed = rows[:bounded]
    next_cursor = str(consumed[-1]["task_id"]) if consumed else ""
    return consumed, next_cursor, len(rows) > bounded


def reconcile(project: str = DEFAULT_PROJECT, incremental: bool = False,
              activity_limit: int = 200, task_limit: int = 200,
              evidence_limit: int = 1000) -> Dict[str, Any]:
    """Local drift report for board provenance.

    Board-internal checks always run. When a canonical main SHA and local git checkout are
    available, reconcile also verifies recorded SHAs against git reachability. If GitHub repo
    config is present, PR records are checked through the GitHub API. Scheduled callers can
    set incremental=True to consume indexed activity/task pages with hard bounds. Full mode is
    explicit because it may scan history and run GitHub orphan-discovery backstops.
    """
    now = time.time()
    agreement = _store_facade().get_working_agreement(project)
    findings: List[Dict[str, Any]] = []
    tasks: List[Dict[str, Any]] = []
    git_states: Dict[str, Dict[str, Any]] = {}
    repo = _store_facade().get_project_github_repo(project)
    previous_cursor = int(_store_facade().get_meta(_reconcile_cursor_key(), 0, project=project) or 0) if incremental else 0
    previous_task_cursor = str(_store_facade().get_meta(
        "reconcile.task_cursor", "", project=project) or "") if incremental else ""
    changed_task_ids: set = set()
    checked_task_ids: set = set()
    activity_has_more = False
    task_has_more = False
    next_task_cursor = previous_task_cursor
    with _conn(project) as c:
        if incremental:
            changed_task_ids, cursor, activity_has_more = _reconcile_activity_batch(
                c, previous_cursor, activity_limit)
            bounded_tasks = max(1, min(int(task_limit), 1000))
            changed_ids = sorted(changed_task_ids)[:bounded_tasks]
            rows_by_id: Dict[str, sqlite3.Row] = {}
            if changed_ids:
                placeholders = ",".join("?" for _ in changed_ids)
                for row in c.execute(
                        f"SELECT * FROM tasks WHERE task_id IN ({placeholders})", changed_ids).fetchall():
                    rows_by_id[str(row["task_id"])] = row
            remaining = max(0, bounded_tasks - len(rows_by_id))
            page_rows: List[sqlite3.Row] = []
            if remaining:
                page_rows, next_task_cursor, task_has_more = _reconcile_task_page(
                    c, previous_task_cursor, remaining)
                for row in page_rows:
                    if len(rows_by_id) >= bounded_tasks:
                        break
                    rows_by_id.setdefault(str(row["task_id"]), row)
            rows = list(rows_by_id.values())
        else:
            rows = c.execute("SELECT * FROM tasks ORDER BY sort_order, task_id").fetchall()
            cursor = int(c.execute(
                "SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0])
        for row in rows:
            task = _task_row(row)
            git_state = _load_git_state(c, task["task_id"])
            tasks.append(task)
            status = task.get("status")
            # PR-evidence hydration retired (ADR-0006): pr_number is now derived from a
            # recorded pr_url at write time (_upsert_git_state), and the sweep/open-PR
            # backstop recover dropped-webhook tasks by matching the PR's task-id — so
            # there is nothing to scrape from activity here.
            git_states[task["task_id"]] = git_state
            checked_task_ids.add(task["task_id"])
            if (status == "Done" and not _has_done_provenance(git_state)
                    and not (repo and git_state.get("pr_number"))):
                findings.append({"severity": "high", "task_id": task["task_id"],
                                 "code": "done_without_merged_sha",
                                 "detail": "Task is Done but has no recorded merge/default-branch or offline evidence provenance."})
            if status == "In Review" and not (git_state.get("branch") or git_state.get("pr_url")):
                findings.append({"severity": "medium", "task_id": task["task_id"],
                                 "code": "review_without_provenance",
                                 "detail": "Task is In Review but lacks branch/PR evidence."})
            if (status == "In Progress" and not git_state.get("head_sha")
                    and not git_state.get("pr_number")):
                # A recorded pr_number means this task has PR evidence pending merge-provenance
                # evaluation below (open PR, or a merge about to be auto-stamped) — the PR checks
                # own its provenance state, so don't also flag it as "no pushed head".
                findings.append({"severity": "low", "task_id": task["task_id"],
                                 "code": "progress_without_pushed_head",
                                 "detail": "Task is In Progress with no reported pushed head SHA."})
            _upsert_git_state(c, task["task_id"], {"last_reconciled_at": now})
        stale_task_claims = c.execute(
            "SELECT id, task_id, agent_id, expires_at FROM task_claims "
            "WHERE status='active' AND expires_at<=? ORDER BY expires_at LIMIT ?",
            (now, max(1, min(int(task_limit), 1000)) if incremental else 1000000),
        ).fetchall()
        for claim in stale_task_claims:
            findings.append({"severity": "medium", "task_id": claim["task_id"],
                             "code": "stale_task_claim",
                             "detail": f"Active task claim {claim['id']} by {claim['agent_id']} expired without completion or abandon."})
        stale_file_leases = c.execute(
            "SELECT id, task_id, agent_id, claimed_at, ttl_minutes FROM file_leases "
            "WHERE released_at IS NULL ORDER BY claimed_at LIMIT ?",
            (max(1, min(int(task_limit), 1000)) if incremental else 1000000,),
        ).fetchall()
        for lease in stale_file_leases:
            expires_at = float(lease["claimed_at"] or 0) + int(lease["ttl_minutes"] or 0) * 60
            if expires_at <= now:
                findings.append({"severity": "medium", "task_id": lease["task_id"],
                                 "code": "stale_file_lease",
                                 "detail": f"File lease {lease['id']} by {lease['agent_id']} expired without release."})
        stale_resource_leases = c.execute(
            "SELECT id, task_id, agent_id, resource_type, claimed_at, ttl_seconds FROM resource_leases "
            "WHERE released_at IS NULL ORDER BY claimed_at LIMIT ?",
            (max(1, min(int(task_limit), 1000)) if incremental else 1000000,),
        ).fetchall()
        for lease in stale_resource_leases:
            expires_at = float(lease["claimed_at"] or 0) + int(lease["ttl_seconds"] or 0)
            if expires_at <= now:
                findings.append({"severity": "medium", "task_id": lease["task_id"],
                                 "code": "stale_resource_lease",
                                 "detail": f"{lease['resource_type']} lease {lease['id']} by {lease['agent_id']} expired without release."})
        evidence_kwargs = ({
            "task_ids": sorted(checked_task_ids),
            "limit": max(1, min(int(evidence_limit), 5000)),
        } if incremental else {})
        # Snapshot claim reports now, but emit findings only after merge backfill so a
        # successful same-pass Done stamp can suppress false claim_evidence_missing for
        # PR heads the control-plane clone has not fetched (BUG-117).
        evidence_reports = list(
            _store_facade()._evidence_claim_reports(c, **evidence_kwargs)
        )
    external_findings, external_checks, backfilled = _external_reconcile_findings(
        tasks, git_states, agreement.get("canonical_main_sha") or "", project=project,
        run_discovery_backstops=not incremental)
    findings.extend(external_findings)
    publication_findings, publication_checks = _store_facade()._publication_reconcile_findings(
        tasks, git_states, project=project)
    findings.extend(publication_findings)
    external_checks.update(publication_checks)
    tasks_by_id = {task["task_id"]: task for task in tasks}
    for report in evidence_reports:
        if report.get("status") == "pass":
            continue
        task_id = report.get("task_id")
        task = tasks_by_id.get(task_id) if task_id else None
        if (task and task.get("status") == "Done"
                and _has_done_provenance(git_states.get(task_id, {}))):
            continue
        artifacts = ", ".join(report.get("claim", {}).get("artifacts") or [])
        evidence_values = []
        declared = report.get("declared_evidence") or {}
        for key in ("paths", "urls", "refs"):
            evidence_values.extend(declared.get(key) or [])
        detail = report.get("detail") or "Claim evidence could not be verified."
        if artifacts:
            detail += f" Claimed artifact(s): {artifacts}."
        if evidence_values:
            detail += f" Declared evidence: {', '.join(evidence_values)}."
        findings.append({
            "severity": report.get("severity") or "medium",
            "task_id": report.get("task_id"),
            "code": report.get("code") or "claim_without_evidence",
            "failure_class": report.get("failure_class") or "missing_data",
            "detail": detail,
            "evidence_claim": report,
        })
    if not (agreement.get("canonical_main_sha") or _store_facade().get_meta("canonical_main_sha", None, project=project)):
        findings.append({"severity": "medium", "task_id": None,
                         "code": "missing_canonical_main_sha",
                         "detail": "No canonical main SHA recorded yet; wait for a default-branch push webhook or set meta."})
    findings = [_annotate_reconcile_finding(f) for f in findings]
    _store_facade().append_activity("reconcile.completed", "reconcile",
                    {"findings": len(findings), "backfilled": backfilled},
                    task_id=None, project=project)
    if incremental:
        _store_facade().set_meta(_reconcile_cursor_key(), cursor, project=project)
        _store_facade().set_meta("reconcile.task_cursor", next_task_cursor, project=project)
        external_checks["incremental"] = True
        external_checks["since_activity_cursor"] = previous_cursor
        external_checks["activity_batch_limit"] = max(1, min(int(activity_limit), 1000))
        external_checks["activity_has_more"] = activity_has_more
        external_checks["changed_task_count"] = len(changed_task_ids)
        external_checks["board_task_checks"] = len(checked_task_ids)
        external_checks["task_batch_limit"] = max(1, min(int(task_limit), 1000))
        external_checks["task_cursor"] = next_task_cursor
        external_checks["task_has_more"] = task_has_more
        external_checks["evidence_batch_limit"] = max(1, min(int(evidence_limit), 5000))
    return {"project": project, "ok": not findings, "findings": findings,
            "activity_cursor": cursor, "checked_at": now,
            "external_checks": external_checks, "backfilled": backfilled}


def close_stale_reconcile_alert_inbox(project: str = DEFAULT_PROJECT,
                                      actor: str = "switchboard/reconcile",
                                      reason: str = "bus_hygiene_auto_close",
                                      now: Optional[float] = None) -> Dict[str, Any]:
    """Bulk-close unacked reconcile_alert messages that still require ack.

    Reconcile drift is informational: it is recorded in activity and the operator
    reconcile panel, not the ack-required agent inbox. This migration path auto-acks
    legacy reconcile_alert backlog entries and resolves their ack_deadline monitors.
    """
    now = time.time() if now is None else float(now)
    closed_ids: List[int] = []
    monitor_ids: List[str] = []
    with _conn(project) as c:
        rows = c.execute(
            "SELECT id FROM agent_messages WHERE requires_ack=1 AND acked_at IS NULL "
            "AND signal='reconcile_alert' ORDER BY id",
        ).fetchall()
        for row in rows:
            message_id = int(row["id"])
            ack_response = f"auto-closed ({reason})"
            cur = c.execute(
                "UPDATE agent_messages SET acked_at=?, ack_response=? "
                "WHERE id=? AND acked_at IS NULL",
                (now, ack_response, message_id),
            )
            if cur.rowcount == 0:
                continue
            closed_ids.append(message_id)
            mon = _store_facade()._load_monitor_for_message(c, message_id)
            if mon and mon.get("status") in ("pending", "fired"):
                monitor_ids.append(mon["id"])
                c.execute(
                    "UPDATE coordination_monitors SET status='resolved', resolved_at=?, "
                    "updated_at=?, last_checked_at=?, result_json=? WHERE id=?",
                    (now, now, now,
                     json.dumps({"acked_at": now, "ack_response": ack_response,
                                 "reason": reason, "auto_closed": True},
                                sort_keys=True),
                     mon["id"]),
                )
            c.execute(
                "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (None, actor, "message.acked",
                 json.dumps({"message_id": message_id, "response": ack_response,
                             "signal": "reconcile_alert", "auto_closed": True,
                             "reason": reason}, sort_keys=True),
                 now),
            )
        if closed_ids:
            c.execute(
                "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (None, actor, "reconcile.alert_inbox_closed",
                 json.dumps({"closed_count": len(closed_ids),
                             "message_ids": closed_ids,
                             "monitor_ids": monitor_ids,
                             "reason": reason}, sort_keys=True),
                 now),
            )
    return {"project": project, "closed_count": len(closed_ids),
            "message_ids": closed_ids, "monitor_ids": monitor_ids,
            "reason": reason}


def run_reconcile_alerts(project: str = DEFAULT_PROJECT,
                         alert_to: str = "switchboard/operator",
                         actor: str = "switchboard/reconcile",
                         min_severity: str = "medium",
                         dedupe_window_s: int = 3600,
                         now: Optional[float] = None,
                         incremental: bool = True,
                         requires_ack: bool = False,
                         close_stale_inbox: bool = True) -> Dict[str, Any]:
    """Run reconcile and send a deduped directed alert for actionable findings.

    Reconcile drift surfaces through activity (`reconcile.alert`) and the operator
    reconcile panel. By default alerts are fire-and-forget (requires_ack=false) so
    coordinator/agent ack traffic stays visible in list_pending_acks.

    The dedupe key is project + severity floor + finding signature + time bucket, so a
    persistent unresolved issue alerts at most once per bucket while a new drift shape alerts
    immediately.
    """
    now = time.time() if now is None else float(now)
    alert_to = (alert_to or "switchboard/operator").strip()
    min_severity = (min_severity or "medium").strip().lower()
    floor = _severity_value(min_severity)
    if floor <= 0:
        min_severity = "medium"
        floor = _severity_value(min_severity)
    dedupe_window_s = max(60, int(dedupe_window_s or 3600))
    inbox_closed = (close_stale_reconcile_alert_inbox(project=project, actor=actor, now=now)
                    if close_stale_inbox else
                    {"closed_count": 0, "message_ids": [], "monitor_ids": []})
    report = reconcile(project=project, incremental=incremental)
    findings = [f for f in report["findings"]
                if _severity_value(str(f.get("severity") or "")) >= floor]
    if not findings:
        return {"project": project, "ok": True, "alert_sent": False, "deduped": False,
                "finding_count": 0, "min_severity": min_severity,
                "requires_ack": requires_ack,
                "inbox_closed": inbox_closed,
                "checked_at": report["checked_at"], "external_checks": report["external_checks"]}

    signature = _reconcile_signature(findings)
    window = int(now // dedupe_window_s)
    idem_key = f"reconcile-alert:{project}:{min_severity}:{alert_to}:{window}:{signature}"
    payload = {"project": project, "alert_to": alert_to, "min_severity": min_severity,
               "dedupe_window_s": dedupe_window_s, "signature": signature,
               "finding_count": len(findings)}
    with _conn(project) as c:
        hit = _store_facade()._idem_hit(c, "reconcile_alert", idem_key, actor, payload)
    if hit is not None:
        if "error" in hit:
            return hit
        out = dict(hit)
        out["alert_sent"] = False
        out["deduped"] = True
        return out

    message = _format_reconcile_alert(project, findings, signature)
    msg = _store_facade().send_agent_message(
        from_agent=actor,
        to_agent=alert_to,
        task_id=None,
        message=message,
        requires_ack=requires_ack,
        signal="reconcile_alert",
        priority=90,
        idem_key=f"{idem_key}:message",
        project=project,
    )
    response = {"project": project, "ok": False, "alert_sent": True,
                "deduped": False, "message_id": msg["id"],
                "finding_count": len(findings), "min_severity": min_severity,
                "requires_ack": requires_ack,
                "inbox_closed": inbox_closed,
                "signature": signature, "dedupe_window_s": dedupe_window_s,
                "checked_at": report["checked_at"],
                "external_checks": report["external_checks"],
                "findings": findings}
    with _conn(project) as c:
        _store_facade()._idem_store(c, "reconcile_alert", idem_key, actor, payload, response)
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "reconcile.alert",
                   json.dumps({k: v for k, v in response.items() if k != "findings"},
                              sort_keys=True), now))
    return response


class StoreProvenanceRepository:
    """SQL-backed provenance / reconcile repository (ARCH-MS-34)."""

    def mark_task_pr_opened(self, task_id: str, **kwargs) -> dict[str, Any]:
        return _store_facade().mark_task_pr_opened(task_id, **kwargs)

    def mark_task_merged(self, task_id: str, merged_sha: str, **kwargs) -> dict[str, Any]:
        return _store_facade().mark_task_merged(task_id, merged_sha, **kwargs)

    def mark_task_default_branch_commit(self, task_id: str, **kwargs) -> dict[str, Any]:
        return mark_task_default_branch_commit(task_id, **kwargs)

    def mark_task_offline_done(self, task_id: str, **kwargs) -> dict[str, Any]:
        return mark_task_offline_done(task_id, **kwargs)

    def github_webhook_deliveries(self, **kwargs) -> dict[str, Any]:
        return github_webhook_deliveries(**kwargs)

    def update_canonical_main_sha(self, sha: str, **kwargs) -> dict[str, Any]:
        return update_canonical_main_sha(sha, **kwargs)

    def retire_merged_branch(self, **kwargs) -> dict[str, Any]:
        return retire_merged_branch(**kwargs)

    def reconcile(self, **kwargs) -> dict[str, Any]:
        return reconcile(**kwargs)

    def run_reconcile_alerts(self, **kwargs) -> dict[str, Any]:
        return run_reconcile_alerts(**kwargs)

    def close_stale_reconcile_alert_inbox(self, **kwargs) -> dict[str, Any]:
        return close_stale_reconcile_alert_inbox(**kwargs)


def default_provenance_repository() -> StoreProvenanceRepository:
    return StoreProvenanceRepository()


__all__ = [
    "StoreProvenanceRepository",
    "default_provenance_repository",
    "SEVERITY_VALUE",
    "_severity_value",
    "_reconcile_failure_class",
    "_annotate_reconcile_finding",
    "_git_state_row",
    "_load_git_state",
    "_git_states_by_task",
    "_provenance_by_task",
    "_parse_evidence",
    "_upsert_git_state",
    "_same_pr_reference",
    "_preserve_provider_pr_evidence",
    "mark_task_pr_opened",
    "_mark_task_pr_opened_impl",
    "mark_task_merged",
    "_mark_task_merged_impl",
    "mark_task_default_branch_commit",
    "mark_task_offline_done",
    "github_webhook_deliveries",
    "update_canonical_main_sha",
    "_git_ok",
    "_git_fetch_origin",
    "_git_checks_available",
    "_github_repo_from_git_url",
    "_github_repo_from_pr_url",
    "_normalize_repo_slug",
    "_local_github_repo",
    "_github_pr",
    "_github_prs_graphql",
    "_fetch_github_prs",
    "_github_token",
    "_github_merged_prs",
    "_retire_branches_enabled",
    "_github_write",
    "retire_merged_branch",
    "_activity_text",
    "_pr_references_task",
    "_has_immutable_canonical_merge",
    "_needs_live_pr_recheck",
    "_external_reconcile_findings",
    "_orphan_merge_discovery_findings",
    "_open_pr_backstop_findings",
    "_reconcile_signature",
    "_format_reconcile_alert",
    "_reconcile_cursor_key",
    "_reconcile_changed_task_ids",
    "reconcile",
    "close_stale_reconcile_alert_inbox",
    "run_reconcile_alerts",
]
