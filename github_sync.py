"""GitHub webhook to Switchboard provenance lifecycle.

This module is intentionally FastAPI-free so replay/idempotency behavior can be tested
without importing the web app.
"""
import re
from typing import Any, Dict, List

import store

_CLOSES_RE = re.compile(r"\b(?:closes?|fixes?|resolves?)\s+([A-Z][A-Z0-9]+-\d+)\b", re.I)
_TASKID_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b", re.I)


def _dedupe_upper(ids: List[str]) -> List[str]:
    return list(dict.fromkeys((i or "").upper() for i in ids if i))


def extract_task_ids(text: str) -> List[str]:
    return _dedupe_upper(_TASKID_RE.findall(text or ""))


def closing_task_ids(text: str) -> List[str]:
    return _dedupe_upper([m.group(1) for m in _CLOSES_RE.finditer(text or "")])


def resolve_project(payload: Dict[str, Any], requested_project: str = "") -> str:
    """Resolve webhook project safely.

    Explicit query-string project wins. When GitHub sends a projectplanner/Switchboard repo
    webhook without ?project=..., route it to the Switchboard board instead of Helm.
    """
    explicit = (requested_project or "").strip()
    if explicit:
        return explicit
    repo = (payload.get("repository") or {})
    full_name = (repo.get("full_name") or "").lower()
    name = (repo.get("name") or "").lower()
    if full_name.endswith("/projectplanner") or full_name.endswith("/switchboard") or name in {
        "projectplanner",
        "switchboard",
    }:
        return "switchboard"
    if full_name.endswith("/helm") or name == "helm":
        return "helm"
    return store.DEFAULT_PROJECT


def task_ids_for_pr(pr: Dict[str, Any]) -> List[str]:
    title = str(pr.get("title") or "")
    body = str(pr.get("body") or "")
    branch = str((pr.get("head") or {}).get("ref") or "")
    explicit_closes = closing_task_ids(f"{title}\n{body}")
    branch_or_title = extract_task_ids(f"{title}\n{branch}")
    return _dedupe_upper(explicit_closes + branch_or_title)


def handle_push(payload: Dict[str, Any], project: str) -> Dict[str, Any]:
    """Push to default branch: refresh canonical main SHA + notify active lease holders."""
    ref = payload.get("ref", "")
    default = payload.get("repository", {}).get("default_branch", "main")
    if ref != f"refs/heads/{default}":
        return {"action": "ignored", "reason": f"push to {ref!r}, not default branch"}

    repo = payload.get("repository", {}).get("full_name", "?")
    commits = payload.get("commits") or []
    head_sha = payload.get("after", "")
    store.update_canonical_main_sha(head_sha, "github-webhook", project)

    changed_files: List[str] = []
    for c in commits:
        for key in ("added", "modified", "removed"):
            changed_files.extend(c.get(key) or [])
    changed_files = list(dict.fromkeys(changed_files))

    notified: List[str] = []
    if changed_files:
        held_records = store.check_files(changed_files, project)
        by_holder: Dict[str, List[str]] = {}
        for rec in held_records:
            holder = rec["held_by"]
            by_holder.setdefault(holder, []).append(rec["file"])
        for holder, their_files in by_holder.items():
            store.send_agent_message(
                "github-webhook", holder,
                f"main advanced on {repo} @ {head_sha}. "
                f"Files you hold a lease on were changed: {', '.join(their_files[:10])}. "
                "Rebase or release your lease before merging.",
                requires_ack=False, project=project,
            )
            notified.append(holder)

    direct_backfill = store.backfill_default_branch_commits(
        commits, default, "github-webhook", project
    )
    return {"action": "push_processed", "repo": repo, "sha": head_sha,
            "changed_files": len(changed_files), "notified_agents": notified,
            **direct_backfill}


def handle_pr(payload: Dict[str, Any], project: str) -> Dict[str, Any]:
    """PR lifecycle: open -> In Review; merge -> Done with merged_sha."""
    pr = payload.get("pull_request") or {}
    action = payload.get("action")
    if action not in ("opened", "reopened", "ready_for_review", "synchronize", "closed"):
        return {"action": "ignored", "reason": f"unsupported PR action {action!r}"}

    repo = payload.get("repository", {}).get("full_name", "?")
    default = payload.get("repository", {}).get("default_branch", "main")
    base = (pr.get("base") or {}).get("ref", "")
    pr_num = pr.get("number")
    task_ids = task_ids_for_pr(pr)
    branch = (pr.get("head") or {}).get("ref", "")
    head_sha = (pr.get("head") or {}).get("sha", "")
    pr_url = pr.get("html_url", "")

    if action in ("opened", "reopened", "ready_for_review", "synchronize"):
        touched: List[str] = []
        skipped: List[Dict[str, str]] = []
        for task_id in task_ids:
            res = store.mark_task_pr_opened(
                task_id, pr_num, pr_url, branch, head_sha, "github-webhook", project
            )
            if res.get("error"):
                skipped.append({"task_id": task_id, "reason": res["error"]})
            else:
                touched.append(task_id)
        return {"action": "pr_review_recorded", "repo": repo, "pr": pr_num,
                "in_review_tasks": touched, "skipped_tasks": skipped}

    if not pr.get("merged"):
        return {"action": "ignored", "reason": "closed PR was not merged", "pr": pr_num}

    merged_sha = pr.get("merge_commit_sha") or ""
    if not merged_sha:
        return {"action": "ignored", "reason": "missing merge_commit_sha", "pr": pr_num}
    if merged_sha and base == default:
        store.update_canonical_main_sha(merged_sha, "github-webhook", project)

    closed: List[str] = []
    skipped = []
    for task_id in task_ids:
        t = store.get_task(task_id, project)
        if not t:
            skipped.append({"task_id": task_id, "reason": "task_not_found"})
            continue
        if t.get("status") in ("Cancelled", "Canceled"):
            skipped.append({"task_id": task_id, "reason": "cancelled"})
            continue
        res = store.mark_task_merged(
            task_id, merged_sha, pr_num, pr_url, branch, head_sha, "github-webhook", project
        )
        if res.get("error"):
            skipped.append({"task_id": task_id, "reason": res["error"]})
        else:
            closed.append(task_id)

    return {"action": "pr_processed", "repo": repo, "pr": pr_num,
            "merged_sha": merged_sha, "auto_closed_tasks": closed,
            "skipped_tasks": skipped}
