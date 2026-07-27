"""Task-scoped reconciliation of authoritative GitHub merge provenance."""
from __future__ import annotations

from collections.abc import Callable, Mapping
import re
from typing import Any

LoadSubject = Callable[[str, str], tuple[dict[str, Any], dict[str, Any]] | None]
CanonicalRepoFor = Callable[[str], str]
FetchPullRequest = Callable[[str, int], Mapping[str, Any] | None]
MarkMerged = Callable[..., dict[str, Any]]

_PR_URL_RE = re.compile(
    r"https?://github\.com/([^/\s]+/[^/\s]+)/pull/\d+", re.IGNORECASE)


def _repo_from_pr_url(url: str) -> str:
    match = _PR_URL_RE.search(str(url or ""))
    return match.group(1).removesuffix(".git") if match else ""


def _normalize_repo(repo: str) -> str:
    return str(repo or "").strip().removesuffix(".git").lower()


def execute(
    task_id: str, *, project: str, actor: str = "reconcile/task",
    load_subject: LoadSubject,
    canonical_repo_for: CanonicalRepoFor,
    fetch_pull_request: FetchPullRequest,
    mark_merged: MarkMerged,
) -> dict[str, Any]:
    """Read one task and its exact recorded PR, then stamp observed merge truth.

    The fresh GitHub response owns the merge decision. Stored task status and head
    SHA are intentionally not eligibility gates because both may lag a merged PR.
    A historical PR merge is task provenance, not proof of the current default
    branch tip, so this command never changes ``canonical_main_sha``.
    """
    normalized_task_id = str(task_id or "").strip().upper()
    if not normalized_task_id:
        return {"error": "task_id_required", "task_id": normalized_task_id}

    subject = load_subject(normalized_task_id, project)
    if not subject:
        return {"error": "task_not_found", "task_id": normalized_task_id}
    task, state = subject

    pr_number = int(state.get("pr_number") or 0)
    if pr_number <= 0:
        return {
            "error": "recorded_pr_required",
            "task_id": normalized_task_id,
            "status": task.get("status"),
        }

    canonical_repo = str(canonical_repo_for(project) or "").strip()
    recorded_repo = _repo_from_pr_url(str(state.get("pr_url") or ""))
    pr_repo = recorded_repo or canonical_repo
    if not canonical_repo or not pr_repo:
        return {
            "error": "canonical_github_repo_required",
            "task_id": normalized_task_id,
            "pr_number": pr_number,
        }
    if _normalize_repo(pr_repo) != _normalize_repo(canonical_repo):
        return {
            "error": "recorded_pr_not_canonical",
            "task_id": normalized_task_id,
            "pr_number": pr_number,
            "recorded_repo": pr_repo,
            "canonical_repo": canonical_repo,
        }

    pr = fetch_pull_request(pr_repo, pr_number)
    if not pr:
        return {
            "error": "pr_state_unavailable",
            "failure_class": "broken_connection",
            "task_id": normalized_task_id,
            "pr_number": pr_number,
            "repo": pr_repo,
        }

    base = pr.get("base") or {}
    base_ref = str(base.get("ref") or "").strip()
    default_ref = str((base.get("repo") or {}).get("default_branch") or "").strip()
    merged_sha = str(pr.get("merge_commit_sha") or "").strip().lower()
    merged = bool(pr.get("merged_at") and merged_sha)
    if not merged or not base_ref or base_ref != default_ref:
        return {
            "schema": "switchboard.task_merge_reconcile.v1",
            "task_id": normalized_task_id,
            "pr_number": pr_number,
            "observed": "not_default_branch_merged",
            "merged": merged,
            "base_branch": base_ref or None,
            "default_branch": default_ref or None,
            "changed": False,
        }

    stamped = mark_merged(
        normalized_task_id,
        merged_sha,
        pr_number=pr_number,
        pr_url=str(state.get("pr_url") or pr.get("html_url") or ""),
        branch=str((pr.get("head") or {}).get("ref") or state.get("branch") or ""),
        head_sha=str((pr.get("head") or {}).get("sha") or ""),
        actor=actor,
        project=project,
        provenance_source="github_pr_merged_task_reconcile",
        base_branch=base_ref,
    )
    return {
        "schema": "switchboard.task_merge_reconcile.v1",
        "task_id": normalized_task_id,
        "pr_number": pr_number,
        "observed": "default_branch_merged",
        **stamped,
    }


__all__ = ["execute"]
