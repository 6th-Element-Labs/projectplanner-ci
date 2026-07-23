"""Merged-PR deployment status for the Fleet dock.

The running production SHA comes from the same state file as ``/health/version``.
GitHub supplies recently merged PRs and the commit history rooted at that SHA.
A PR is "deployed" only when its merge commit is present in that running history.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Mapping, Optional

import deploy_staleness
import open_prs
import store


SCHEMA = "switchboard.deployments.v1"
CACHE_SECONDS = 15
TERMINAL_STATUSES = {"Done", "Cancelled"}


def _deployment_tasks(project: str) -> Dict[str, Mapping[str, Any]]:
    rows = store.list_tasks(workstream="DEPLOY", project=project)
    found: Dict[str, Mapping[str, Any]] = {}
    for task in rows:
        title = str(task.get("title") or "")
        marker = "[deploy "
        start = title.lower().find(marker)
        if start < 0:
            continue
        end = title.find("]", start)
        if end < 0:
            continue
        sha = title[start + len(marker):end].strip().lower()
        if sha:
            found[sha] = task
    return found


def build_deployments(
        project: str,
        *,
        repo: str = "",
        token: str = "",
        now: Optional[float] = None,
        list_fn: Optional[Callable[[str, str], Any]] = None,
        commits_fn: Optional[Callable[[str, str, str], Any]] = None,
        canonical_fn: Optional[Callable[[str, str, str], Any]] = None,
        health_fn: Optional[Callable[[], Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    now = time.time() if now is None else float(now)
    repo = repo or (store.get_project_github_repo(project) or "")
    token = token or open_prs._token()
    health = dict((health_fn or deploy_staleness.health_view)() or {})
    base = {
        "schema": SCHEMA,
        "project": project,
        "repo": repo,
        "generated_at": now,
        "production": health,
        "canonical_sha": "",
        "deployments": [],
        "undeployed_count": 0,
    }
    if project != "switchboard":
        return {**base, "unavailable": "no_production_target"}
    if not repo:
        return {**base, "unavailable": "no_canonical_repo"}
    if not token:
        return {**base, "unavailable": "no_github_token"}
    running_sha = str(health.get("running_sha") or "").lower()
    if not running_sha:
        return {**base, "unavailable": "production_sha_unknown"}

    list_fn = list_fn or (lambda r, t: open_prs._github_request(
        f"https://api.github.com/repos/{r}/pulls?state=closed&sort=updated"
        "&direction=desc&per_page=30", t))
    commits_fn = commits_fn or (lambda r, sha, t: open_prs._github_request(
        f"https://api.github.com/repos/{r}/commits?sha={sha}&per_page=100", t))
    canonical_ref = str(health.get("canonical_ref") or "origin/master").split("/")[-1]
    canonical_fn = canonical_fn or (lambda r, ref, t: open_prs._github_request(
        f"https://api.github.com/repos/{r}/commits/{ref}", t))
    try:
        pulls = list_fn(repo, token) or []
        commits = commits_fn(repo, running_sha, token) or []
        canonical = canonical_fn(repo, canonical_ref, token) or {}
    except Exception as exc:
        return {**base, "unavailable": f"github_error: {exc}"}

    deployed_shas = {
        str(item.get("sha") or "").lower()
        for item in commits if isinstance(item, Mapping)
    }
    deployed_shas.add(running_sha)
    queued = _deployment_tasks(project)
    canonical_sha = str(canonical.get("sha") or "").lower()
    if len(canonical_sha) != 40:
        return {**base, "unavailable": "canonical_sha_unknown"}
    rows: List[Dict[str, Any]] = []
    for pr in pulls:
        if not isinstance(pr, Mapping) or not pr.get("merged_at"):
            continue
        merge_sha = str(pr.get("merge_commit_sha") or "").lower()
        if not merge_sha:
            continue
        deployed = merge_sha in deployed_shas
        request_task = next(
            (task for sha, task in queued.items()
             if canonical_sha.startswith(sha) or sha.startswith(canonical_sha)),
            None,
        )
        request_status = str((request_task or {}).get("status") or "")
        status = "deployed" if deployed else (
            "deploying" if request_status == "In Progress"
            else "queued" if request_task and request_status not in TERMINAL_STATUSES
            else "failed" if health.get("last_deploy_ok") is False
            else "undeployed"
        )
        rows.append({
            "number": int(pr.get("number") or 0),
            "title": str(pr.get("title") or ""),
            "url": str(pr.get("html_url") or ""),
            "author": str((pr.get("user") or {}).get("login") or ""),
            "merged_at": open_prs._parse_github_ts(str(pr.get("merged_at") or "")),
            "merge_sha": merge_sha,
            "status": status,
            "deployed": deployed,
            "deploy_task_id": str((request_task or {}).get("task_id") or ""),
            "deploy_task_status": request_status,
            "target_sha": running_sha if deployed else canonical_sha,
        })
    rows.sort(key=lambda row: row["merged_at"], reverse=True)
    return {
        **base,
        "canonical_sha": canonical_sha,
        "deployments": rows,
        "undeployed_count": sum(1 for row in rows if not row["deployed"]),
    }


def deployments_payload(project: str) -> Dict[str, Any]:
    from read_cache import ttl_read_cache
    bucket = int(time.time() // CACHE_SECONDS)
    return ttl_read_cache(
        "deployments", project, bucket,
        lambda: build_deployments(project), ttl=CACHE_SECONDS)
