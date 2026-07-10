"""GitHub webhook to Switchboard provenance lifecycle.

This module is intentionally FastAPI-free so replay/idempotency behavior can be tested
without importing the web app.
"""
import re
from typing import Any, Dict, List

import store
import task_id_parser

# Re-export parser helpers for existing imports/tests.
extract_task_ids = task_id_parser.extract_task_ids
closing_task_ids = task_id_parser.closing_task_ids


def _project_for_repo(full_name: str) -> str:
    repo = (full_name or "").strip().lower()
    if not repo:
        return ""
    role_matches = []
    canonical_matches = []
    for project_id in store.project_ids():
        try:
            store.init_db(project_id)
            role = store.get_project_repo_role(repo, project=project_id)
        except Exception:
            continue
        if role.get("matched"):
            role_matches.append({"project": project_id, **role})
        if role.get("canonical"):
            canonical_matches.append(project_id)
    if len(canonical_matches) == 1:
        return canonical_matches[0]
    if len(canonical_matches) > 1:
        return "__ambiguous_repo_role__"
    if not role_matches:
        return ""
    if len(role_matches) == 1:
        return role_matches[0]["project"]
    # Shared evidence repos such as public-CI can belong to many projects. In that
    # case the webhook must use explicit ?project=... instead of guessing.
    return "__ambiguous_repo_role__"


def resolve_project(payload: Dict[str, Any], requested_project: str = "") -> str:
    """Resolve webhook project safely.

    Explicit query-string project wins. Otherwise route by the repository configured on
    each board (including dynamic projects such as Vulkan). The hard-coded aliases below
    preserve older deployments even before their project metadata is initialized.
    """
    explicit = (requested_project or "").strip()
    if explicit:
        return explicit
    repo = (payload.get("repository") or {})
    full_name = (repo.get("full_name") or "").lower()
    name = (repo.get("name") or "").lower()
    configured_project = _project_for_repo(full_name)
    if configured_project == "__ambiguous_repo_role__":
        return ""
    if configured_project:
        return configured_project
    if full_name.endswith("/projectplanner") or full_name.endswith("/switchboard") or name in {
        "projectplanner",
        "switchboard",
    }:
        return "switchboard"
    if full_name.endswith("/helm") or name == "helm":
        return "helm"
    return store.DEFAULT_PROJECT


def _repo_role(repo: str, project: str) -> Dict[str, Any]:
    return store.get_project_repo_role(repo, project=project)


def _role_skip(task_id: str, role_info: Dict[str, Any], reason: str) -> Dict[str, str]:
    return {
        "task_id": task_id,
        "reason": reason,
        "repo_role": role_info.get("role") or "unknown",
        "repo": role_info.get("repo") or "",
    }


def _record_repo_role_rejection(task_id: str, role_info: Dict[str, Any],
                                event: str, pr_number: Any = None,
                                pr_url: str = "", project: str = "") -> None:
    if not task_id or not store.get_task(task_id, project):
        return
    store.append_activity(
        "git.repo_role_rejected",
        "github-webhook",
        {
            "event": event,
            "repo": role_info.get("repo"),
            "repo_role": role_info.get("role"),
            "canonical_required": True,
            "reason": "only canonical repo webhooks can change code Done provenance",
            "pr_number": pr_number,
            "pr_url": pr_url,
        },
        task_id=task_id,
        project=project,
    )


def task_ids_for_pr(pr: Dict[str, Any]) -> List[str]:
    return task_id_parser.task_ids_for_pr(pr)


def handle_push(payload: Dict[str, Any], project: str) -> Dict[str, Any]:
    """Push to default branch: refresh canonical main SHA + notify active lease holders."""
    ref = payload.get("ref", "")
    default = payload.get("repository", {}).get("default_branch", "main")
    if ref != f"refs/heads/{default}":
        return {"action": "ignored", "reason": f"push to {ref!r}, not default branch"}

    repo = payload.get("repository", {}).get("full_name", "?")
    role_info = _repo_role(repo, project)
    if not role_info.get("canonical"):
        store.append_activity(
            "git.repo_role_rejected",
            "github-webhook",
            {
                "event": "push",
                "repo": repo,
                "repo_role": role_info.get("role"),
                "canonical_required": True,
                "reason": "push webhook is not from the project canonical repo",
            },
            task_id=None,
            project=project,
        )
        return {
            "action": "ignored",
            "reason": "repo_role_cannot_mark_done",
            "repo": repo,
            "repo_role": role_info.get("role"),
            "canonical_required": True,
        }

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

    # Default-branch backfill retired (ADR-0006): every default-branch commit is a
    # merged PR that reconcile's orphan sweep already stamps; direct-to-main pushes
    # are policy-forbidden. Push only advances canonical main + notifies leaseholders.
    return {"action": "push_processed", "repo": repo, "sha": head_sha,
            "changed_files": len(changed_files), "notified_agents": notified}


def handle_pr(payload: Dict[str, Any], project: str) -> Dict[str, Any]:
    """PR lifecycle: open -> In Review; merge -> Done with merged_sha."""
    pr = payload.get("pull_request") or {}
    action = payload.get("action")
    if action not in ("opened", "reopened", "ready_for_review", "synchronize", "closed"):
        return {"action": "ignored", "reason": f"unsupported PR action {action!r}"}

    repo = payload.get("repository", {}).get("full_name", "?")
    role_info = _repo_role(repo, project)
    default = payload.get("repository", {}).get("default_branch", "main")
    base = (pr.get("base") or {}).get("ref", "")
    pr_num = pr.get("number")
    task_ids = task_ids_for_pr(pr)
    branch = (pr.get("head") or {}).get("ref", "")
    head_sha = (pr.get("head") or {}).get("sha", "")
    pr_url = pr.get("html_url", "")
    if not role_info.get("canonical"):
        skipped = []
        for task_id in task_ids:
            _record_repo_role_rejection(
                task_id, role_info, f"pull_request.{action}",
                pr_number=pr_num, pr_url=pr_url, project=project)
            skipped.append(_role_skip(task_id, role_info, "repo_role_cannot_mark_done"))
        return {
            "action": "ignored",
            "reason": "repo_role_cannot_mark_done",
            "repo": repo,
            "repo_role": role_info.get("role"),
            "canonical_required": True,
            "task_ids": task_ids,
            "skipped_tasks": skipped,
        }

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

    # BUG-29: retire (archive+delete) the merged head branch so branches don't pile up.
    # Same-repo, non-default branches only; no-op unless PM_RETIRE_MERGED_BRANCHES is set.
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name") or ""
    branch_retired = None
    if branch and branch != default and head_repo == repo:
        branch_retired = store.retire_merged_branch(repo, branch, head_sha, project)

    return {"action": "pr_processed", "repo": repo, "pr": pr_num,
            "merged_sha": merged_sha, "auto_closed_tasks": closed,
            "skipped_tasks": skipped, "branch_retired": branch_retired}
