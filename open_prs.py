"""Open-PR status board for the operator fleet dock (spec:
docs/superpowers/specs/2026-07-23-fleet-dock-pr-tab-design.md).

Lists every open PR on a project's canonical repo with hover-card-parity status:
CI (commit statuses + check runs on the head SHA), mergeable state, review state,
merge-queue position, diff stats, and the board-task join. Rows are classified
server-side under attention rule C ("anything blocking a merge") so the dock
renders without re-deriving GitHub semantics.

All GitHub fetchers are injectable for tests; every enrichment call is
best-effort — a missing token or a failed call degrades to fewer badges, never
an error payload (the dock polls this on a timer).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional

import task_id_parser

OPEN_PRS_SCHEMA = "switchboard.open_prs.v1"
# One GitHub sweep per project per bucket; the read cache serves everything between.
CACHE_BUCKET_SECONDS = int(os.environ.get("PM_OPEN_PRS_CACHE_S", "60") or 60)
STALL_AFTER_SECONDS = 24 * 3600


def _token() -> str:
    for name in ("PM_GITHUB_TOKEN", "GITHUB_TOKEN", "SWITCHBOARD_CI_GITHUB_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _github_request(url: str, token: str = "") -> Any:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode())


def _github_graphql(query: str, token: str) -> Any:
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql", data=body, method="POST")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode())


def _parse_github_ts(value: str) -> float:
    from datetime import datetime
    clean = (value or "").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(clean).timestamp()
    except Exception:
        return 0.0


def fetch_merge_queue_positions(repo: str, token: str,
                                graphql_fn: Optional[Callable[[str, str], Any]] = None,
                                ) -> Dict[int, int]:
    """PR number -> 1-based merge-queue position. Best-effort: {} on any failure."""
    graphql_fn = graphql_fn or _github_graphql
    owner, _, name = repo.partition("/")
    if not owner or not name or not token:
        return {}
    query = (
        'query { repository(owner: "%s", name: "%s") { mergeQueue { '
        'entries(first: 50) { nodes { position pullRequest { number } } } } } }'
        % (owner, name))
    try:
        payload = graphql_fn(query, token) or {}
        nodes = (((payload.get("data") or {}).get("repository") or {})
                 .get("mergeQueue") or {}).get("entries", {}).get("nodes") or []
        return {int((n.get("pullRequest") or {}).get("number") or 0): int(n.get("position") or 0)
                for n in nodes if (n.get("pullRequest") or {}).get("number")}
    except Exception:
        return {}


def ci_state_for_sha(repo: str, sha: str, token: str,
                     request_fn: Optional[Callable[[str, str], Any]] = None,
                     ) -> Dict[str, Any]:
    """Fold combined commit statuses + check runs into one CI verdict.

    Returns {"state": "success"|"failure"|"pending"|"none", "failing": [context...]}.
    The VM gate posts a commit *status* while GitHub Actions post check *runs*, so
    both surfaces must be read (HARDEN-67 taught us they disagree in interesting ways).
    """
    request_fn = request_fn or _github_request
    states: List[str] = []
    failing: List[str] = []
    try:
        combined = request_fn(
            f"https://api.github.com/repos/{repo}/commits/{sha}/status", token) or {}
        for row in combined.get("statuses") or []:
            state = str(row.get("state") or "")
            states.append(state)
            if state in ("failure", "error"):
                failing.append(str(row.get("context") or "status"))
    except Exception:
        pass
    try:
        runs = request_fn(
            f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs?per_page=100",
            token) or {}
        for run in runs.get("check_runs") or []:
            if run.get("status") != "completed":
                states.append("pending")
                continue
            conclusion = str(run.get("conclusion") or "")
            if conclusion in ("failure", "timed_out", "cancelled"):
                states.append("failure")
                failing.append(str(run.get("name") or "check"))
            elif conclusion in ("success", "neutral", "skipped"):
                states.append("success")
    except Exception:
        pass
    if any(s in ("failure", "error") for s in states):
        state = "failure"
    elif any(s == "pending" for s in states):
        state = "pending"
    elif states:
        state = "success"
    else:
        state = "none"
    return {"state": state, "failing": failing[:3]}


def classify(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Attention rule C: blocked = anything blocking a merge.

    Red CI, merge conflicts, or a PR whose checks are green yet GitHub still
    reports the merge blocked (missing required review / stuck) all count.
    Draft is presentation state, not a mask: red exact-head CI and an active
    remediation route still block completion while the PR remains draft.
    """
    ci = str(row.get("ci_state") or "none")
    mergeable = str(row.get("mergeable_state") or "")
    projection = row.get("completion_projection")
    projection = projection if isinstance(projection, Mapping) else {}
    if ci == "failure":
        failing = row.get("ci_failing") or []
        return {"blocked": True,
                "blocked_reason": f"{failing[0]} failed" if failing else "checks failed"}
    if mergeable == "dirty":
        return {"blocked": True, "blocked_reason": "merge conflicts"}
    if mergeable == "blocked" and ci == "success":
        return {"blocked": True, "blocked_reason": "green but blocked"}
    if projection.get("route") in {"remediation", "human"}:
        return {"blocked": True,
                "blocked_reason": str(projection.get("reason_code") or projection.get("route"))}
    return {"blocked": False, "blocked_reason": ""}


def _board_join(pr: Mapping[str, Any], project: str,
                get_task_fn: Callable[..., Optional[Mapping[str, Any]]],
                ) -> Dict[str, Any]:
    """Join a PR to its board task(s) via branch/title parsing (same parser the
    merge webhook uses, so the dock and Done-stamping agree on ownership)."""
    tasks: List[Dict[str, Any]] = []
    selected_projection: Optional[Mapping[str, Any]] = None
    for task_id in task_id_parser.task_ids_for_pr(dict(pr)):
        try:
            task = get_task_fn(task_id, project=project)
        except Exception:
            task = None
        if task:
            try:
                from switchboard.application.queries import completion_projection
                completion_projection.attach_completion_projection(
                    task, project=project)
            except Exception:
                pass
            projection = task.get("completion_projection")
            tasks.append({
                "task_id": task_id,
                "status": str(task.get("status") or ""),
                "completion_projection": projection,
            })
            if selected_projection is None and isinstance(projection, Mapping):
                selected_projection = projection
    return {
        "tasks": tasks,
        "orphan": not tasks,
        "completion_projection": selected_projection,
    }


def build_open_prs(project: str, *,
                   repo: str = "",
                   token: str = "",
                   now: Optional[float] = None,
                   list_fn: Optional[Callable[[str, str], Any]] = None,
                   detail_fn: Optional[Callable[[str, int, str], Any]] = None,
                   ci_fn: Optional[Callable[[str, str, str], Dict[str, Any]]] = None,
                   queue_fn: Optional[Callable[[str, str], Dict[int, int]]] = None,
                   get_task_fn: Optional[Callable[..., Any]] = None,
                   ) -> Dict[str, Any]:
    """The dock payload: every open PR on the canonical repo, badge-ready."""
    now = time.time() if now is None else float(now)
    if not repo:
        try:
            import store
            repo = store.get_project_github_repo(project) or ""
        except Exception:
            repo = ""
    token = token or _token()
    if get_task_fn is None:
        import store
        get_task_fn = store.get_task
    base = {"schema": OPEN_PRS_SCHEMA, "project": project, "repo": repo,
            "generated_at": now, "prs": []}
    if not repo:
        return {**base, "unavailable": "no_canonical_repo"}
    if not token:
        return {**base, "unavailable": "no_github_token"}
    list_fn = list_fn or (lambda r, t: _github_request(
        f"https://api.github.com/repos/{r}/pulls?state=open&per_page=100", t))
    detail_fn = detail_fn or (lambda r, n, t: _github_request(
        f"https://api.github.com/repos/{r}/pulls/{int(n)}", t))
    ci_fn = ci_fn or (lambda r, sha, t: ci_state_for_sha(r, sha, t))
    queue_fn = queue_fn or (lambda r, t: fetch_merge_queue_positions(r, t))
    try:
        listed = list_fn(repo, token) or []
    except Exception as exc:
        return {**base, "unavailable": f"github_error: {exc}"}
    queue_positions = queue_fn(repo, token)
    rows: List[Dict[str, Any]] = []
    for pr in listed:
        if not isinstance(pr, Mapping):
            continue
        number = int(pr.get("number") or 0)
        head_sha = str((pr.get("head") or {}).get("sha") or "")
        updated_ts = _parse_github_ts(str(pr.get("updated_at") or pr.get("created_at") or ""))
        row: Dict[str, Any] = {
            "number": number,
            "title": str(pr.get("title") or ""),
            "url": str(pr.get("html_url") or f"https://github.com/{repo}/pull/{number}"),
            "draft": bool(pr.get("draft")),
            "author": str((pr.get("user") or {}).get("login") or ""),
            "head_sha": head_sha,
            "base_ref": str((pr.get("base") or {}).get("ref") or ""),
            "updated_at": updated_ts,
            "stalled": bool(updated_ts and now - updated_ts > STALL_AFTER_SECONDS),
            "auto_merge": bool(pr.get("auto_merge")),
            "queue_position": queue_positions.get(number, 0),
        }
        try:
            detail = detail_fn(repo, number, token) or {}
            row["mergeable_state"] = str(detail.get("mergeable_state") or "")
            row["additions"] = int(detail.get("additions") or 0)
            row["deletions"] = int(detail.get("deletions") or 0)
            row["changed_files"] = int(detail.get("changed_files") or 0)
        except Exception:
            row["mergeable_state"] = ""
        ci = ci_fn(repo, head_sha, token) if head_sha else {"state": "none", "failing": []}
        row["ci_state"] = ci.get("state", "none")
        row["ci_failing"] = ci.get("failing", [])
        row.update(_board_join(pr, project, get_task_fn))
        row.update(classify(row))
        rows.append(row)
    rows.sort(key=lambda r: (-int(r["blocked"]), -(r["updated_at"] or 0)))
    return {**base, "prs": rows,
            "blocked_count": sum(1 for r in rows if r["blocked"])}


def open_prs_payload(project: str) -> Dict[str, Any]:
    """Cached entry point for the route: at most one GitHub sweep per
    CACHE_BUCKET_SECONDS per project (stale-while-revalidate via read_cache)."""
    from read_cache import ttl_read_cache
    bucket = int(time.time() // max(1, CACHE_BUCKET_SECONDS))
    return ttl_read_cache(
        "open_prs", project, bucket, lambda: build_open_prs(project),
        ttl=CACHE_BUCKET_SECONDS)
