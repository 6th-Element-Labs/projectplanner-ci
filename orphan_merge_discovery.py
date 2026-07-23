"""PR-based provenance discovery for reconcile.

Two backstops that recover tasks whose GitHub webhook was dropped (transient DB
lock, missing hook, or an agent that merged without recording evidence), by
matching PRs to tasks on the task-id in the branch/title/closing refs:

- merged-PR orphan sweep (RECON-11): tasks with empty git_state whose work
  already merged on the canonical repo -> stamped Done.
- open-PR backstop (BUG-28): pre-review tasks with empty git_state that have an
  open canonical PR whose `pr_opened` event was dropped -> advanced to In Review.

Together they are the two halves of reconcile's PR-discovery backstop. They match
tasks by the task-id in the PR directly (no scraping), which lets ADR-0006 retire
the older evidence-scraping recovery paths: the default-branch backfill is retired
alongside this change; the PR-evidence hydration path is retired in a follow-up.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import task_id_parser

SCHEMA = "switchboard.orphan_merge_discovery.v1"
OPEN_PR_SCHEMA = "switchboard.open_pr_backstop.v1"
DEFAULT_LOOKBACK_DAYS = 30
# Blocked is eligible so remediation-parked tasks still recover Done when the
# merge webhook is dropped (SIMPLIFY-22 / completion-run route=remediation).
ELIGIBLE_STATUSES = frozenset({"Not Started", "In Progress", "In Review", "Blocked"})
SKIP_STATUSES = frozenset({"Done", "Cancelled", "Canceled"})
# Open PRs advance pre-review tasks only; In Review/Done already have evidence.
OPEN_PR_ELIGIBLE_STATUSES = frozenset({"Not Started", "In Progress"})


def _empty_git_state(state: Mapping[str, Any]) -> bool:
    return not any(
        state.get(field)
        for field in ("pr_number", "merged_sha", "pr_url", "branch", "head_sha")
    )


def _default_branch_merge(pr: Mapping[str, Any]) -> bool:
    base_ref = ((pr.get("base") or {}).get("ref") or "").strip()
    default_ref = ((pr.get("base") or {}).get("repo") or {}).get("default_branch") or ""
    return bool(base_ref and default_ref and base_ref == default_ref)


def _repo_from_pr_url(url: str) -> str:
    match = re.search(r"github\.com/([^/\s]+/[^/\s]+)", url or "", re.I)
    return match.group(1) if match else ""


def _pr_summary(pr: Mapping[str, Any], repo: str) -> Dict[str, Any]:
    pr_repo = _repo_from_pr_url(str(pr.get("html_url") or "")) or repo
    return {
        "pr_number": int(pr.get("number") or 0),
        "pr_url": pr.get("html_url") or "",
        "repo": pr_repo,
        "title": pr.get("title") or "",
        "head_branch": (pr.get("head") or {}).get("ref") or "",
        "head_sha": (pr.get("head") or {}).get("sha") or "",
        "merged_sha": pr.get("merge_commit_sha") or "",
        "merged_at": pr.get("merged_at") or "",
        "task_ids": task_id_parser.task_ids_for_pr(pr),
    }


def fetch_recent_merged_prs(
    repo: str,
    *,
    token: str = "",
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    now: Optional[float] = None,
    per_page: int = 100,
    max_pages: int = 5,
    request_fn: Optional[Callable[[str, str], Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """List merged PRs on the canonical repo within the lookback window."""
    now = time.time() if now is None else float(now)
    since_ts = now - max(1, int(lookback_days)) * 86400
    request_fn = request_fn or _github_request
    merged: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {
        "repo": repo,
        "lookback_days": int(lookback_days),
        "since_ts": since_ts,
        "pages_fetched": 0,
        "prs_scanned": 0,
    }
    for page in range(1, max_pages + 1):
        url = (
            f"https://api.github.com/repos/{repo}/pulls"
            f"?state=closed&sort=updated&direction=desc&per_page={int(per_page)}&page={page}"
        )
        try:
            payload = request_fn(url, token)
        except urllib.error.HTTPError as exc:
            meta["error"] = f"http_{exc.code}"
            meta["auth_required"] = exc.code in (401, 403)
            raise
        except Exception as exc:
            meta["error"] = str(exc)
            raise
        if not isinstance(payload, list):
            meta["error"] = "unexpected_payload"
            break
        meta["pages_fetched"] = page
        if not payload:
            break
        stop = False
        for pr in payload:
            meta["prs_scanned"] += 1
            merged_at = pr.get("merged_at")
            if not merged_at:
                continue
            try:
                merged_ts = _parse_github_ts(merged_at)
            except Exception:
                merged_ts = now
            if merged_ts < since_ts:
                stop = True
                continue
            if _default_branch_merge(pr) and pr.get("merge_commit_sha"):
                merged.append(dict(pr))
        if stop or len(payload) < per_page:
            break
    meta["merged_pr_count"] = len(merged)
    return merged, meta


def _parse_github_ts(value: str) -> float:
    from datetime import datetime, timezone
    clean = (value or "").replace("Z", "+00:00")
    return datetime.fromisoformat(clean).timestamp()


def _github_request(url: str, token: str = "") -> Any:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode())


def build_task_pr_index(
    merged_prs: Sequence[Mapping[str, Any]],
    repo: str,
    *,
    commit_messages_for: Optional[Callable[[int], str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Map task_id -> candidate merged PR summaries."""
    index: Dict[str, List[Dict[str, Any]]] = {}
    for pr in merged_prs:
        commit_messages = commit_messages_for(int(pr.get("number") or 0)) if commit_messages_for else ""
        task_ids = task_id_parser.task_ids_for_pr(pr, commit_messages=commit_messages)
        summary = _pr_summary(pr, repo)
        summary["task_ids"] = task_ids
        for task_id in task_ids:
            index.setdefault(task_id.upper(), []).append(summary)
    return index


def discover_orphan_merges(
    tasks: Sequence[Mapping[str, Any]],
    git_states: Mapping[str, Mapping[str, Any]],
    *,
    project: str,
    repo: str,
    token: str = "",
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    active_claims: Optional[Mapping[str, Mapping[str, Any]]] = None,
    role_checker: Optional[Callable[[str], Mapping[str, Any]]] = None,
    fetch_merged_prs_fn: Optional[Callable[..., Tuple[List[Dict[str, Any]], Dict[str, Any]]]] = None,
    commit_messages_for: Optional[Callable[[int], str]] = None,
    mark_merged_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    append_activity_fn: Optional[Callable[..., None]] = None,
    now: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """Scan canonical merged PRs and repair orphan tasks with empty git_state."""
    now = time.time() if now is None else float(now)
    findings: List[Dict[str, Any]] = []
    backfilled: List[Dict[str, Any]] = []
    checks: Dict[str, Any] = {
        "orphan_merge_discovery": "not_configured",
        "schema": SCHEMA,
    }
    active_claims = active_claims or {}
    role_checker = role_checker or (lambda _repo: {"canonical": True, "role": "canonical"})
    fetch_merged_prs_fn = fetch_merged_prs_fn or fetch_recent_merged_prs

    if not (repo or "").strip():
        checks["orphan_merge_discovery"] = "skipped_no_repo"
        return findings, backfilled, checks

    repo = repo.strip()
    role = role_checker(repo)
    if not role.get("canonical"):
        checks["orphan_merge_discovery"] = "skipped_non_canonical_repo"
        return findings, backfilled, checks

    if not token:
        checks["orphan_merge_discovery"] = "skipped_no_token"
        findings.append({
            "severity": "medium",
            "task_id": None,
            "code": "orphan_merge_discovery_skipped_no_token",
            "detail": (
                "Orphan merged-PR discovery requires a GitHub token for the canonical repo; "
                "set PM_GITHUB_TOKEN, GITHUB_TOKEN, or SWITCHBOARD_CI_GITHUB_TOKEN."
            ),
            "failure_class": "missing_data",
        })
        return findings, backfilled, checks

    try:
        merged_prs, fetch_meta = fetch_merged_prs_fn(
            repo, token=token, lookback_days=lookback_days, now=now,
        )
    except urllib.error.HTTPError as exc:
        checks["orphan_merge_discovery"] = "failed"
        checks["fetch_error"] = f"http_{exc.code}"
        if exc.code in (401, 403):
            findings.append({
                "severity": "medium",
                "task_id": None,
                "code": "orphan_merge_discovery_skipped_no_token",
                "detail": (
                    f"GitHub rejected orphan merged-PR discovery for {repo} "
                    f"(HTTP {exc.code}); token missing or insufficient."
                ),
                "failure_class": "absent_permission",
            })
        else:
            findings.append({
                "severity": "medium",
                "task_id": None,
                "code": "orphan_merge_discovery_fetch_failed",
                "detail": f"Could not list merged PRs for {repo}: HTTP {exc.code}.",
                "failure_class": "broken_connection",
            })
        return findings, backfilled, checks
    except Exception as exc:
        checks["orphan_merge_discovery"] = "failed"
        checks["fetch_error"] = str(exc)
        findings.append({
            "severity": "medium",
            "task_id": None,
            "code": "orphan_merge_discovery_fetch_failed",
            "detail": f"Could not list merged PRs for {repo}: {exc}",
            "failure_class": "broken_connection",
        })
        return findings, backfilled, checks

    checks.update(fetch_meta)
    checks["orphan_merge_discovery"] = "checked"
    index = build_task_pr_index(merged_prs, repo, commit_messages_for=commit_messages_for)

    for task in tasks:
        task_id = str(task.get("task_id") or "")
        status = task.get("status") or ""
        if not task_id or status in SKIP_STATUSES:
            continue
        if status not in ELIGIBLE_STATUSES:
            continue
        state = dict(git_states.get(task_id) or {})
        if not _empty_git_state(state):
            continue

        candidates = list(index.get(task_id.upper()) or [])
        eligible: List[Dict[str, Any]] = []
        for candidate in candidates:
            pr_repo = str(candidate.get("repo") or repo)
            role_info = role_checker(pr_repo)
            if not role_info.get("canonical"):
                findings.append({
                    "severity": "low" if role_info.get("role") in ("public_ci", "public") else "medium",
                    "task_id": task_id,
                    "code": "orphan_merge_wrong_repo_role",
                    "detail": (
                        f"Merged PR #{candidate.get('pr_number')} is in repo role "
                        f"{role_info.get('role') or 'unknown'}; canonical completion ignored."
                    ),
                    "repo_role": role_info.get("role") or "unknown",
                    "pr_number": candidate.get("pr_number"),
                    "failure_class": "failed_gate",
                })
                continue
            eligible.append(candidate)

        if not eligible:
            continue
        if len(eligible) > 1:
            findings.append({
                "severity": "high",
                "task_id": task_id,
                "code": "orphan_merge_ambiguous",
                "detail": (
                    f"Multiple canonical merged PRs mention {task_id}; "
                    "manual repair required."
                ),
                "candidates": eligible,
                "failure_class": "missing_data",
            })
            continue

        pr = eligible[0]
        claim = active_claims.get(task_id)
        if claim:
            findings.append({
                "severity": "high",
                "task_id": task_id,
                "code": "orphan_merge_active_claim",
                "detail": (
                    f"Repairing orphan merge for {task_id} while active claim "
                    f"{claim.get('id')} is held by {claim.get('agent_id')}."
                ),
                "claim_id": claim.get("id"),
                "agent_id": claim.get("agent_id"),
                "failure_class": "failed_gate",
            })

        if not mark_merged_fn:
            continue

        stamped = mark_merged_fn(
            task_id,
            pr.get("merged_sha") or "",
            pr_number=int(pr.get("pr_number") or 0) or None,
            pr_url=pr.get("pr_url") or "",
            branch=pr.get("head_branch") or "",
            head_sha=pr.get("head_sha") or "",
            provenance_source="orphan_merge_discovery",
            task_ids_found=pr.get("task_ids") or [task_id],
            actor="reconcile/orphan_merge_discovery",
            project=project,
        )
        if stamped.get("error"):
            findings.append({
                "severity": "high",
                "task_id": task_id,
                "code": "orphan_merge_stamp_failed",
                "detail": stamped.get("error") or "mark_task_merged failed",
                "failure_class": "failed_gate",
            })
            continue

        if append_activity_fn:
            append_activity_fn(
                "git.orphan_merge_discovered",
                "reconcile/orphan_merge_discovery",
                {
                    "task_id": task_id,
                    "source": "orphan_merge_discovery",
                    "pr_number": pr.get("pr_number"),
                    "pr_url": pr.get("pr_url"),
                    "head_branch": pr.get("head_branch"),
                    "head_sha": pr.get("head_sha"),
                    "merged_sha": pr.get("merged_sha"),
                    "task_ids_found": pr.get("task_ids") or [task_id],
                    "active_claim_id": (claim or {}).get("id"),
                },
                task_id=task_id,
                project=project,
            )

        backfilled.append({
            "task_id": task_id,
            "source": "orphan_merge_discovery",
            "pr_number": pr.get("pr_number"),
            "merged_sha": pr.get("merged_sha"),
        })
        if stamped.get("git_state") and isinstance(git_states, dict):
            git_states[task_id] = stamped["git_state"]
        if isinstance(task, dict):
            task["status"] = "Done"

    return findings, backfilled, checks


# --- open-PR backstop (BUG-28) -------------------------------------------------

def fetch_recent_open_prs(
    repo: str,
    *,
    token: str = "",
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    now: Optional[float] = None,
    per_page: int = 100,
    max_pages: int = 5,
    request_fn: Optional[Callable[[str, str], Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """List open PRs on the canonical repo updated within the lookback window."""
    now = time.time() if now is None else float(now)
    since_ts = now - max(1, int(lookback_days)) * 86400
    request_fn = request_fn or _github_request
    open_prs: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {
        "repo": repo, "lookback_days": int(lookback_days),
        "since_ts": since_ts, "pages_fetched": 0, "prs_scanned": 0,
    }
    for page in range(1, max_pages + 1):
        url = (
            f"https://api.github.com/repos/{repo}/pulls"
            f"?state=open&sort=updated&direction=desc&per_page={int(per_page)}&page={page}"
        )
        try:
            payload = request_fn(url, token)
        except urllib.error.HTTPError as exc:
            meta["error"] = f"http_{exc.code}"
            meta["auth_required"] = exc.code in (401, 403)
            raise
        except Exception as exc:
            meta["error"] = str(exc)
            raise
        if not isinstance(payload, list):
            meta["error"] = "unexpected_payload"
            break
        meta["pages_fetched"] = page
        if not payload:
            break
        stop = False
        for pr in payload:
            meta["prs_scanned"] += 1
            if pr.get("draft"):
                continue
            updated_at = pr.get("updated_at") or pr.get("created_at")
            try:
                updated_ts = _parse_github_ts(updated_at) if updated_at else now
            except Exception:
                updated_ts = now
            if updated_ts < since_ts:
                stop = True
                continue
            if (pr.get("head") or {}).get("sha"):
                open_prs.append(dict(pr))
        if stop or len(payload) < per_page:
            break
    meta["open_pr_count"] = len(open_prs)
    return open_prs, meta


def discover_open_prs(
    tasks: Sequence[Mapping[str, Any]],
    git_states: Mapping[str, Mapping[str, Any]],
    *,
    project: str,
    repo: str,
    token: str = "",
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    active_claims: Optional[Mapping[str, Mapping[str, Any]]] = None,
    role_checker: Optional[Callable[[str], Mapping[str, Any]]] = None,
    fetch_open_prs_fn: Optional[Callable[..., Tuple[List[Dict[str, Any]], Dict[str, Any]]]] = None,
    mark_pr_opened_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    append_activity_fn: Optional[Callable[..., None]] = None,
    now: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """Advance pre-review tasks with empty git_state that have an open canonical PR
    whose `pr_opened` webhook was dropped (BUG-28). Mirrors discover_orphan_merges
    for the open-PR half the merged sweep cannot cover."""
    now = time.time() if now is None else float(now)
    findings: List[Dict[str, Any]] = []
    advanced: List[Dict[str, Any]] = []
    checks: Dict[str, Any] = {"open_pr_backstop": "not_configured", "schema": OPEN_PR_SCHEMA}
    active_claims = active_claims or {}
    role_checker = role_checker or (lambda _repo: {"canonical": True, "role": "canonical"})
    fetch_open_prs_fn = fetch_open_prs_fn or fetch_recent_open_prs

    repo = (repo or "").strip()
    if not repo:
        checks["open_pr_backstop"] = "skipped_no_repo"
        return findings, advanced, checks
    if not role_checker(repo).get("canonical"):
        checks["open_pr_backstop"] = "skipped_non_canonical_repo"
        return findings, advanced, checks
    if not token:
        checks["open_pr_backstop"] = "skipped_no_token"
        return findings, advanced, checks

    try:
        open_prs, fetch_meta = fetch_open_prs_fn(
            repo, token=token, lookback_days=lookback_days, now=now)
    except urllib.error.HTTPError as exc:
        checks["open_pr_backstop"] = "failed"
        checks["fetch_error"] = f"http_{exc.code}"
        return findings, advanced, checks
    except Exception as exc:
        checks["open_pr_backstop"] = "failed"
        checks["fetch_error"] = str(exc)
        return findings, advanced, checks

    checks.update(fetch_meta)
    checks["open_pr_backstop"] = "checked"
    index = build_task_pr_index(open_prs, repo)

    for task in tasks:
        task_id = str(task.get("task_id") or "")
        status = task.get("status") or ""
        if not task_id or status not in OPEN_PR_ELIGIBLE_STATUSES:
            continue
        if not _empty_git_state(dict(git_states.get(task_id) or {})):
            continue
        eligible = [c for c in (index.get(task_id.upper()) or [])
                    if role_checker(str(c.get("repo") or repo)).get("canonical")]
        if not eligible:
            continue
        if len(eligible) > 1:
            findings.append({
                "severity": "medium", "task_id": task_id,
                "code": "open_pr_backstop_ambiguous",
                "detail": f"Multiple canonical open PRs mention {task_id}; not auto-advancing.",
                "candidates": eligible, "failure_class": "missing_data"})
            continue
        pr = eligible[0]
        if not mark_pr_opened_fn:
            continue
        result = mark_pr_opened_fn(
            task_id, int(pr.get("pr_number") or 0),
            pr_url=pr.get("pr_url") or "", branch=pr.get("head_branch") or "",
            head_sha=pr.get("head_sha") or "",
            actor="reconcile/open_pr_backstop", project=project)
        if result.get("error") or result.get("skipped"):
            continue
        if result.get("idempotent"):
            continue
        if append_activity_fn:
            append_activity_fn(
                "git.open_pr_backstop_advanced", "reconcile/open_pr_backstop",
                {"task_id": task_id, "source": "open_pr_backstop",
                 "pr_number": pr.get("pr_number"), "pr_url": pr.get("pr_url"),
                 "head_branch": pr.get("head_branch"), "head_sha": pr.get("head_sha"),
                 "task_ids_found": pr.get("task_ids") or [task_id],
                 "active_claim_id": (active_claims.get(task_id) or {}).get("id")},
                task_id=task_id, project=project)
        advanced.append({"task_id": task_id, "source": "open_pr_backstop",
                         "pr_number": pr.get("pr_number"), "status": "In Review"})
        if result.get("git_state") and isinstance(git_states, dict):
            git_states[task_id] = result["git_state"]
        if isinstance(task, dict):
            task["status"] = "In Review"

    return findings, advanced, checks
