"""Pull-model CI dispatch relay (CI-3 / CI-6).

On PR open/update the canonical webhook handler fires one authenticated
``repository_dispatch`` to the public CI repo carrying ``{pr, head_sha}``.
Stateless — no git, no disk on the Plan VM — so it cannot reproduce the
2026-07-12 bare-mirror failure class.

Feature-flagged via ``SWITCHBOARD_CI_PULL_MODEL`` until CI-6 flip is complete.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

SCHEMA = "switchboard.ci_verify_dispatch.v1"
DEFAULT_CI_REPO = "6th-Element-Labs/projectplanner-ci"
DEFAULT_CANONICAL_REPO = "6th-Element-Labs/projectplanner"
DEFAULT_EVENT_TYPE = "verify-pr"
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class CiVerifyDispatchError(RuntimeError):
    """Operator-facing dispatch failure (invalid input, missing token, API error)."""


def is_pull_model_enabled() -> bool:
    return (os.environ.get("SWITCHBOARD_CI_PULL_MODEL") or "").strip().lower() in (
        "1", "true", "yes", "on")


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


def _token(explicit: str = "") -> str:
    return (
        explicit
        or os.environ.get("SWITCHBOARD_CI_DISPATCH_TOKEN")
        or os.environ.get("SWITCHBOARD_CI_GITHUB_TOKEN")
        or os.environ.get("PM_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    ).strip()


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


def dispatch_verify(
    pr_number: int,
    *,
    head_sha: str = "",
    repo: str = "",
    ci_repo_name: str = "",
    token: str = "",
    event_type: str = "",
    dry_run: bool = False,
    strict_explicit: bool = False,
) -> Dict[str, Any]:
    """Fire ``repository_dispatch`` on the public CI repo for one PR head."""
    pr = int(pr_number)
    source_repo = canonical_repo(repo)
    target = ci_repo(ci_repo_name)
    tok = _token(token)
    if not tok:
        raise CiVerifyDispatchError("A GitHub token is required to dispatch pull-model CI.")
    sha, sha_source, stale_webhook_sha = resolve_head_sha(
        pr, head_sha, repo=source_repo, token=tok, strict_explicit=strict_explicit)
    verify_commit_exists(sha, repo=source_repo, token=tok)
    evt = (event_type or os.environ.get("SWITCHBOARD_CI_VERIFY_EVENT")
           or DEFAULT_EVENT_TYPE).strip()
    owner, name = target.split("/", 1)
    payload = {
        "event_type": evt,
        "client_payload": {
            "schema": SCHEMA,
            "pr": pr,
            "head_sha": sha,
            "repo": source_repo,
        },
    }
    result = {
        "schema": SCHEMA,
        "dispatched": False,
        "dry_run": dry_run,
        "ci_repo": target,
        "canonical_repo": source_repo,
        "pr": pr,
        "head_sha": sha,
        "head_sha_source": sha_source,
        "stale_webhook_sha": stale_webhook_sha,
        "event_type": evt,
    }
    if dry_run:
        result["message"] = "validated only — no repository_dispatch sent"
        return result
    _github_request(
        "POST",
        f"repos/{owner}/{name}/dispatches",
        token=tok,
        body=payload,
    )
    result["dispatched"] = True
    result["message"] = f"repository_dispatch {evt!r} sent to {target}"
    return result


def try_dispatch_verify(
    pr_number: int,
    *,
    head_sha: str = "",
    repo: str = "",
    ci_repo_name: str = "",
    token: str = "",
    event_type: str = "",
) -> Dict[str, Any]:
    """Best-effort dispatch for webhook handlers — never raises."""
    if not is_pull_model_enabled():
        return {
            "dispatched": False,
            "skip_reason": "pull_model_disabled",
            "pr": int(pr_number),
        }
    try:
        out = dispatch_verify(
            pr_number,
            head_sha=head_sha,
            repo=repo,
            ci_repo_name=ci_repo_name,
            token=token,
            event_type=event_type,
            dry_run=False,
        )
        out["skip_reason"] = None
        return out
    except Exception as exc:
        return {
            "dispatched": False,
            "skip_reason": str(exc),
            "pr": int(pr_number),
            "head_sha": (head_sha or "").strip() or None,
        }


def _cli(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dispatch pull-model CI verify (repository_dispatch → projectplanner-ci).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python ci_verify_dispatch.py --pr 400 --dry-run\n"
            "  python ci_verify_dispatch.py --pr 400 --dispatch\n"
            "  python jobs.py dispatch_ci -- --pr 400 --dispatch\n"
        ),
    )
    parser.add_argument("--pr", type=int, required=True, help="Canonical PR number")
    parser.add_argument("--head-sha", default="", help="40-char commit SHA (resolved from GitHub if omitted)")
    parser.add_argument("--repo", default="", help=f"Canonical repo (default {DEFAULT_CANONICAL_REPO})")
    parser.add_argument("--ci-repo", default="", help=f"Public CI repo (default {DEFAULT_CI_REPO})")
    parser.add_argument("--event-type", default="", help=f"repository_dispatch event (default {DEFAULT_EVENT_TYPE})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate token + SHA + payload only; do not POST")
    parser.add_argument("--dispatch", action="store_true",
                        help="Actually send repository_dispatch (default without this flag is dry-run)")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON only")
    args = parser.parse_args(argv)

    dry_run = args.dry_run or not args.dispatch
    try:
        result = dispatch_verify(
            args.pr,
            head_sha=args.head_sha,
            repo=args.repo,
            ci_repo_name=args.ci_repo,
            token="",
            event_type=args.event_type,
            dry_run=dry_run,
            strict_explicit=bool((args.head_sha or "").strip()),
        )
    except CiVerifyDispatchError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    if not is_pull_model_enabled() and not args.json:
        print("note: SWITCHBOARD_CI_PULL_MODEL is off — dispatch still works for manual operator use")

    if args.json:
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0

    mode = "dry-run" if dry_run else "dispatched"
    print(f"ok ({mode})")
    print(f"  pr            {result['pr']}")
    print(f"  head_sha      {result['head_sha']} ({result['head_sha_source']})")
    if result.get("stale_webhook_sha"):
        print(f"  stale_webhook ignored {result['stale_webhook_sha']}")
    print(f"  canonical     {result['canonical_repo']}")
    print(f"  ci_repo       {result['ci_repo']}")
    print(f"  event_type    {result['event_type']}")
    print(f"  {result.get('message', '')}")
    if dry_run:
        print("\nRe-run with --dispatch to fire repository_dispatch.")
    return 0


def main(argv: Optional[list] = None) -> int:
    return _cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
