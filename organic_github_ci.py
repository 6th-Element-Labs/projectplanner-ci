"""Ingest organic GitHub checks as exact-head external CI evidence.

Mirror-dispatched CI and GitHub-native CI share the review gate, but not their
identity or lifecycle.  This module writes native evidence directly, keyed by
GitHub's source identity, so it cannot collide with ``external_ci_mirror`` rows.
"""
from __future__ import annotations

import json
import time
import urllib.request
import uuid
from typing import Any, Dict, Iterable, List, Optional

import store


def _status(status: str, conclusion: str = "") -> str:
    state = (status or "").lower()
    end = (conclusion or "").lower()
    if state in {"queued", "requested", "waiting", "pending", "in_progress"}:
        return "running"
    if end in {"success", "neutral", "skipped"} or state == "success":
        return "success"
    if end in {"cancelled"}:
        return "cancelled"
    if end or state in {"failure", "error"}:
        return "failure"
    return "running"


def _task_ids_for_sha(project: str, sha: str) -> List[str]:
    if not sha:
        return []
    store.init_db(project)
    with store._conn(project) as c:
        rows = c.execute(
            "SELECT task_id FROM task_git_state WHERE lower(head_sha)=lower(?)",
            (sha,),
        ).fetchall()
    return [row["task_id"] for row in rows]


def _task_ids(payload: Dict[str, Any], sha: str, project: str) -> List[str]:
    prs: Iterable[Dict[str, Any]] = (
        (payload.get("check_run") or {}).get("pull_requests")
        or (payload.get("check_suite") or {}).get("pull_requests")
        or []
    )
    found: List[str] = []
    for pr in prs:
        found.extend(store.task_ids_for_pr(pr) if hasattr(store, "task_ids_for_pr") else [])
    # Check payload PR stubs usually omit title/body/head. Exact SHA is authoritative.
    return list(dict.fromkeys(found + _task_ids_for_sha(project, sha)))


def invalidate_prior_head(task_id: str, current_sha: str, project: str) -> int:
    """Invalidate only organic evidence for older heads; mirror rows are preserved."""
    now = time.time()
    changed = 0
    with store._conn(project) as c:
        rows = c.execute(
            "SELECT run_id, result_json FROM external_ci_runs "
            "WHERE task_id=? AND source_sha<>? AND effect_key LIKE 'github-organic:%'",
            (task_id, current_sha.lower()),
        ).fetchall()
        for row in rows:
            result = store._json_obj(row["result_json"], {})
            if result.get("invalidated_by_head_sha") == current_sha.lower():
                continue
            result["invalidated_by_head_sha"] = current_sha.lower()
            c.execute(
                "UPDATE external_ci_runs SET result_json=?, updated_at=? WHERE run_id=?",
                (json.dumps(result, sort_keys=True), now, row["run_id"]),
            )
            changed += 1
    return changed


def record(*, project: str, repo: str, sha: str, kind: str, source_id: str,
           context: str, status: str, conclusion: str = "", url: str = "",
           task_ids: Optional[List[str]] = None, raw: Optional[Dict[str, Any]] = None,
           actor: str = "github-webhook") -> List[Dict[str, Any]]:
    """Idempotently upsert one GitHub-native check for every matching task."""
    sha = (sha or "").lower()
    if not sha or not source_id:
        return []
    tasks = task_ids if task_ids is not None else _task_ids_for_sha(project, sha)
    now = time.time()
    outputs: List[Dict[str, Any]] = []
    for task_id in list(dict.fromkeys(tasks)):
        invalidate_prior_head(task_id, sha, project)
        effect_key = f"github-organic:{repo.lower()}:{kind}:{source_id}:{sha}:{task_id}"
        normalized = _status(status, conclusion)
        result = {
            "schema": "switchboard.github_organic_ci.v1",
            "source": "github",
            "source_kind": kind,
            "source_id": str(source_id),
            "status_context": context,
            "raw": raw or {},
        }
        with store._conn(project) as c:
            existing = c.execute(
                "SELECT run_id FROM external_ci_runs WHERE effect_key=?", (effect_key,)
            ).fetchone()
            if existing:
                run_id = existing["run_id"]
                c.execute(
                    "UPDATE external_ci_runs SET status=?, conclusion=?, run_url=?, "
                    "result_json=?, completed_at=?, updated_at=? WHERE run_id=?",
                    (normalized, conclusion or None, url or None,
                     json.dumps(result, sort_keys=True),
                     now if normalized in {"success", "failure", "cancelled", "error"} else None,
                     now, run_id),
                )
                idempotent = True
            else:
                run_id = "ecir-gh-" + uuid.uuid4().hex[:16]
                request = {"source": "github", "source_kind": kind,
                           "source_id": str(source_id), "status_context": context}
                c.execute(
                    """INSERT INTO external_ci_runs
                       (run_id,source_project,source_repo,source_branch,source_sha,
                        mirror_repo,mirror_branch,workflow,status_context,status,conclusion,
                        run_url,logs_url,artifacts_json,failure_class,failure_reason,task_id,
                        claim_id,agent_id,actor,principal_id,effect_key,request_json,result_json,
                        requested_at,mirrored_at,triggered_at,completed_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, project, repo, None, sha, repo, "", context, context,
                     normalized, conclusion or None, url or None, None, "[]",
                     "workflow_failed" if normalized == "failure" else None, None,
                     task_id, None, None, actor, None, effect_key,
                     json.dumps(request, sort_keys=True), json.dumps(result, sort_keys=True),
                     now, now, now, now if normalized in {"success", "failure", "cancelled", "error"} else None, now),
                )
                idempotent = False
        outputs.append({"run_id": run_id, "task_id": task_id, "status": normalized,
                        "source_sha": sha, "idempotent": idempotent})
    return outputs


def handle_webhook(event: str, payload: Dict[str, Any], project: str) -> Dict[str, Any]:
    repo = (payload.get("repository") or {}).get("full_name") or ""
    if event == "check_run":
        item = payload.get("check_run") or {}
        sha = item.get("head_sha") or ""
        rows = record(project=project, repo=repo, sha=sha, kind=event,
                      source_id=str(item.get("id") or ""), context=item.get("name") or "check_run",
                      status=item.get("status") or "", conclusion=item.get("conclusion") or "",
                      url=item.get("html_url") or "", task_ids=_task_ids(payload, sha, project), raw=item)
    elif event == "check_suite":
        item = payload.get("check_suite") or {}
        sha = item.get("head_sha") or ""
        app = item.get("app") or {}
        rows = record(project=project, repo=repo, sha=sha, kind=event,
                      source_id=str(item.get("id") or ""), context=app.get("name") or "check_suite",
                      status=item.get("status") or "", conclusion=item.get("conclusion") or "",
                      url=item.get("url") or "", task_ids=_task_ids(payload, sha, project), raw=item)
    elif event == "status":
        sha = payload.get("sha") or ""
        context = payload.get("context") or "commit-status"
        rows = record(project=project, repo=repo, sha=sha, kind=event,
                      source_id=str(payload.get("id") or context), context=context,
                      status=payload.get("state") or "", conclusion=payload.get("state") or "",
                      url=payload.get("target_url") or "", raw=payload)
    else:
        return {"action": "ignored", "event": event}
    return {"action": "organic_ci_recorded", "event": event, "runs": rows}


def poll_pr_checks(project: str, repo: str, task_id: str, sha: str,
                   token: str = "") -> Dict[str, Any]:
    """Recovery poll for missed check/status webhooks on one exact PR head."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    endpoints = (("check_run", f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs"),
                 ("status", f"https://api.github.com/repos/{repo}/commits/{sha}/status"))
    recorded: List[Dict[str, Any]] = []
    errors: List[str] = []
    for kind, url in endpoints:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as response:
                data = json.loads(response.read().decode())
            items = data.get("check_runs", []) if kind == "check_run" else data.get("statuses", [])
            for item in items:
                recorded.extend(record(
                    project=project, repo=repo, sha=sha, kind=kind,
                    source_id=str(item.get("id") or item.get("context") or ""),
                    context=item.get("name") or item.get("context") or kind,
                    status=item.get("status") or item.get("state") or "",
                    conclusion=item.get("conclusion") or item.get("state") or "",
                    url=item.get("html_url") or item.get("target_url") or "",
                    task_ids=[task_id], raw=item, actor="reconcile/github-poll"))
        except Exception as exc:
            errors.append(f"{kind}: {exc}")
    return {"task_id": task_id, "sha": sha, "recorded": recorded, "errors": errors}
