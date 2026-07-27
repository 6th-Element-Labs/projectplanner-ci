"""Canonical GitHub coordinate helpers for trusted scratchpad CI.

The old private-checkout ``repository_dispatch`` route is retired. This
compatibility module now only resolves and validates canonical PR SHAs for
``ci_scratchpad_dispatch``.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

SCHEMA = "switchboard.ci_verify_dispatch.v1"
DEFAULT_CI_REPO = "6th-Element-Labs/projectplanner-ci"
DEFAULT_CANONICAL_REPO = "6th-Element-Labs/projectplanner"
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class CiVerifyDispatchError(RuntimeError):
    """Operator-facing dispatch failure (invalid input, missing token, API error)."""


def ci_repo(explicit: str = "") -> str:
    return (
        explicit
        or os.environ.get("SWITCHBOARD_CI_VERIFY_REPO")
        or DEFAULT_CI_REPO
    ).strip()


def canonical_repo(explicit: str = "") -> str:
    return (
        explicit
        or os.environ.get("SWITCHBOARD_CI_REPO")
        or os.environ.get("PM_GITHUB_REPO_SWITCHBOARD")
        or os.environ.get("PM_GITHUB_REPO")
        or DEFAULT_CANONICAL_REPO
    ).strip()


def _token(explicit: str = "", repo: str = "") -> str:
    """Dispatch credential. A dedicated dispatch PAT still wins — it may be scoped to
    a different repo than the App install — then the App token, then the PAT chain."""
    dispatch = (explicit or os.environ.get("SWITCHBOARD_CI_DISPATCH_TOKEN") or "").strip()
    if dispatch:
        return dispatch
    import github_app_auth
    return github_app_auth.resolve_token(
        repo=repo,
        env_order=("SWITCHBOARD_CI_GITHUB_TOKEN", "PM_GITHUB_TOKEN", "GITHUB_TOKEN"))


def normalize_commit_sha(sha: str) -> str:
    """Return lowercase 40-hex SHA or raise CiVerifyDispatchError."""
    cleaned = (sha or "").strip().lower()
    if not cleaned:
        raise CiVerifyDispatchError(
            "head_sha is required — pass --head-sha or resolve it from the PR via GitHub API."
        )
    if not GIT_SHA_RE.fullmatch(cleaned):
        raise CiVerifyDispatchError(
            f"invalid head_sha {sha!r}: must be exactly 40 lowercase hex characters "
            f"(GitHub commit SHA). Test fixtures like 'mhead' or 'chead25' must never "
            f"be dispatched to production CI."
        )
    return cleaned


def _github_request(method: str, path: str, *, token: str,
                    body: Optional[Dict[str, Any]] = None) -> Any:
    url = path if path.startswith("https://") else f"https://api.github.com/{path.lstrip('/')}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise CiVerifyDispatchError(
            f"GitHub API {method} {url} failed: HTTP {exc.code} {detail}"
        ) from exc


def fetch_pr_head_sha(pr_number: int, *, repo: str = "", token: str = "") -> str:
    """Resolve PR head SHA from the canonical repo."""
    source_repo = canonical_repo(repo)
    tok = _token(token)
    if not tok:
        raise CiVerifyDispatchError("A GitHub token is required to resolve PR head SHA.")
    owner, name = source_repo.split("/", 1)
    pr = _github_request("GET", f"repos/{owner}/{name}/pulls/{int(pr_number)}", token=tok)
    head_sha = ((pr.get("head") or {}).get("sha") or "").strip()
    if not head_sha:
        raise CiVerifyDispatchError(
            f"PR #{pr_number} on {source_repo} returned no head.sha from GitHub."
        )
    return normalize_commit_sha(head_sha)


def fetch_pr_merge_base_sha(pr_number: int, head_sha: str, *,
                            repo: str = "", token: str = "") -> str:
    """Resolve the PR's canonical merge base for public impacted-test selection.

    The current base branch tip may have advanced beyond the PR and therefore may
    not be reachable from the mirrored head commit. GitHub's compare response
    supplies the actual merge-base object, which is reachable in the pushed
    history and can be verified without exposing a private checkout credential.
    """
    source_repo = canonical_repo(repo)
    tok = _token(token)
    if not tok:
        raise CiVerifyDispatchError("A GitHub token is required to resolve PR merge base.")
    owner, name = source_repo.split("/", 1)
    pr = _github_request("GET", f"repos/{owner}/{name}/pulls/{int(pr_number)}", token=tok)
    base_sha = ((pr.get("base") or {}).get("sha") or "").strip()
    if not base_sha:
        raise CiVerifyDispatchError(
            f"PR #{pr_number} on {source_repo} returned no base.sha from GitHub."
        )
    compare = _github_request(
        "GET",
        f"repos/{owner}/{name}/compare/{normalize_commit_sha(base_sha)}..."
        f"{normalize_commit_sha(head_sha)}",
        token=tok,
    )
    merge_base = ((compare.get("merge_base_commit") or {}).get("sha") or "").strip()
    if not merge_base:
        raise CiVerifyDispatchError(
            f"PR #{pr_number} on {source_repo} returned no merge_base_commit.sha."
        )
    return normalize_commit_sha(merge_base)


def verify_commit_exists(sha: str, *, repo: str = "", token: str = "") -> None:
    """Raise if the commit is not reachable on the canonical repo."""
    source_repo = canonical_repo(repo)
    tok = _token(token)
    if not tok:
        raise CiVerifyDispatchError("A GitHub token is required to verify commit existence.")
    owner, name = source_repo.split("/", 1)
    _github_request("GET", f"repos/{owner}/{name}/commits/{sha}", token=tok)


def resolve_head_sha(
    pr_number: int,
    head_sha: str,
    *,
    repo: str = "",
    token: str = "",
    strict_explicit: bool = False,
) -> Tuple[str, str, Optional[str]]:
    """Return (sha, source_label, stale_webhook_sha).

    The live PR head from GitHub is authoritative. Webhook payloads can carry a
    superseded head_sha after rebase/synchronize races; never dispatch those.
    """
    live = fetch_pr_head_sha(pr_number, repo=repo, token=token)
    webhook = (head_sha or "").strip().lower()
    stale = None
    if webhook:
        try:
            webhook = normalize_commit_sha(webhook)
        except CiVerifyDispatchError:
            if strict_explicit:
                raise
            stale = (head_sha or "").strip() or None
            return live, "github_pr_api", stale
        if webhook != live:
            stale = webhook
    return live, "github_pr_api", stale
