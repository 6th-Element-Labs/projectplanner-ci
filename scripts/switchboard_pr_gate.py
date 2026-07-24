#!/usr/bin/env python3
"""Switchboard PR gates — claim provenance and merge authorization statuses.

VM verification (`Switchboard CI / VM gate`) runs on projectplanner-ci via the
pull-model verify workflow. This runner reads the production board and posts both
`Switchboard / claim gate` and `Switchboard / merge authorization` on each open
fleet PR head SHA. No git, worktrees, venvs, or external_ci_mirror calls live here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

# This script lives in scripts/, so Python's script-dir sys.path entry does not
# cover repo-root modules; without this the systemd claim-gate unit dies on import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pr_provenance_gate  # noqa: E402
import store  # noqa: E402


DEFAULT_REPO = "6th-Element-Labs/projectplanner"
DEFAULT_CLAIM_CONTEXT = "Switchboard / claim gate"
DEFAULT_MERGE_CONTEXT = "Switchboard / merge authorization"


class GateError(RuntimeError):
    pass


def _repo() -> str:
    return (
        os.environ.get("SWITCHBOARD_CI_REPO")
        or os.environ.get("PM_GITHUB_REPO_SWITCHBOARD")
        or os.environ.get("PM_GITHUB_REPO")
        or os.environ.get("GITHUB_REPOSITORY")
        or DEFAULT_REPO
    ).strip()


def _token() -> str:
    return (
        os.environ.get("SWITCHBOARD_CI_GITHUB_TOKEN")
        or os.environ.get("PM_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    ).strip()


def _github_request(method: str, path: str, *, token: str, body: Optional[Dict[str, Any]] = None) -> Any:
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
        raise GateError(f"GitHub API {method} {url} failed: HTTP {exc.code} {detail}") from exc


def _status_description(text: str) -> str:
    text = " ".join(text.split())
    return text[:140]


def latest_status(repo: str, sha: str, context: str, *, token: str) -> Optional[Dict[str, Any]]:
    """Most recent commit-status dict for (sha, context), or None."""
    try:
        rows = _github_request(
            "GET", f"repos/{repo}/commits/{sha}/statuses?per_page=100", token=token)
    except GateError:
        return None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and (row.get("context") or "") == context:
            return row
    return None


def post_status(repo: str, sha: str, state: str, *, context: str, description: str,
                target_url: str = "", token: str) -> Any:
    if not token:
        raise GateError("A GitHub token is required to post PR gate status.")
    desc = _status_description(description)
    # GitHub caps a (commit, context) at 1000 statuses; skip unchanged re-posts.
    current = latest_status(repo, sha, context, token=token)
    if current and (current.get("state") or "") == state and (current.get("description") or "") == desc:
        return {"skipped": "unchanged", "state": state, "context": context, "sha": sha}
    payload: Dict[str, Any] = {
        "state": state,
        "context": context,
        "description": desc,
    }
    if target_url:
        payload["target_url"] = target_url
    return _github_request("POST", f"repos/{repo}/statuses/{sha}", token=token, body=payload)


def list_open_prs(repo: str, *, token: str) -> List[Dict[str, Any]]:
    return _github_request("GET", f"repos/{repo}/pulls?state=open&per_page=100", token=token)


def get_pr(repo: str, number: int, *, token: str) -> Dict[str, Any]:
    return _github_request("GET", f"repos/{repo}/pulls/{int(number)}", token=token)


def list_pr_files(repo: str, number: int, *, token: str) -> List[str]:
    """Changed file paths on a PR (first page) — used for the docs-only exemption."""
    try:
        rows = _github_request(
            "GET", f"repos/{repo}/pulls/{int(number)}/files?per_page=100", token=token)
    except GateError:
        return []
    return [str(row.get("filename") or "") for row in rows or [] if row.get("filename")]


def run_claim_gate_for_pr(pr: Dict[str, Any], *, repo: str, token: str,
                          context: str, mode: str) -> Optional[Dict[str, Any]]:
    """SESSION-12 provenance gate: post a commit status that checks whether a fleet PR
    is backed by a claimed task / Work Session. Reads the production board (this
    process' store), never the PR worktree. mode is resolved per-repo by the caller
    (from repo_topology.roles.canonical.claim_gate)."""
    if mode == "off":
        return None
    number = int(pr["number"])
    sha = pr["head"]["sha"]
    pr_url = pr.get("html_url", f"https://github.com/{repo}/pull/{number}")
    changed_paths = list_pr_files(repo, number, token=token)
    verdict = pr_provenance_gate.evaluate_pr_provenance(
        pr, repo=repo, mode=mode, changed_paths=changed_paths)
    post_status(repo, sha, verdict["state"], context=context,
                description=verdict["context_description"], target_url=pr_url, token=token)
    return {"repo": repo, "pr": number, "sha": sha, "context": context,
            "state": verdict["state"], "reason": verdict.get("reason"),
            "would_block": verdict.get("would_block"), "mode": mode}


def run_merge_authorization_for_pr(
    pr: Dict[str, Any],
    *,
    repo: str,
    token: str,
    context: str = DEFAULT_MERGE_CONTEXT,
) -> Dict[str, Any]:
    """Post the branch-protection projection of Switchboard's real merge gate."""
    number = int(pr["number"])
    sha = str((pr.get("head") or {}).get("sha") or "")
    pr_url = str(pr.get("html_url") or f"https://github.com/{repo}/pull/{number}")
    changed_paths = list_pr_files(repo, number, token=token)
    provenance = pr_provenance_gate.evaluate_pr_provenance(
        pr, repo=repo, mode="enforce", changed_paths=changed_paths,
    )

    if provenance.get("exempt"):
        state = "success"
        reason = str(provenance.get("reason") or "exempt")
        description = str(
            provenance.get("context_description") or "Exempt from fleet merge authorization"
        )
        gate_results: List[Dict[str, Any]] = []
    else:
        resolved = list(provenance.get("resolved") or [])
        gate_results = []
        for item in resolved:
            task_id = str(item.get("task_id") or "")
            project = str(item.get("project") or "")
            gate_results.append(store.merge_gate(
                {
                    "task_id": task_id,
                    "pr_number": number,
                    "pr_url": pr_url,
                    "repo": repo,
                    "head_sha": sha,
                    "evidence": {
                        "github_pr": pr,
                        "changed_files": changed_paths,
                    },
                },
                actor="switchboard-ci/merge-authorization",
                project=project,
            ))

        blocked = [
            finding
            for gate_result in gate_results
            for finding in (gate_result.get("findings") or [])
            if finding.get("blocking")
        ]
        if not resolved:
            state = "failure"
            reason = str(provenance.get("reason") or "task_not_resolved")
            description = str(
                provenance.get("context_description")
                or "PR is not bound to a Switchboard task"
            )
        elif blocked:
            state = "failure"
            reason = str(blocked[0].get("code") or "merge_gate_blocked")
            description = str(
                blocked[0].get("message") or "Switchboard merge gate blocked"
            )
        else:
            state = "success"
            reason = "exact_head_merge_gate_passed"
            description = "Exact-head CI, review, and merge gate passed"

    post_status(
        repo,
        sha,
        state,
        context=context,
        description=description,
        target_url=pr_url,
        token=token,
    )
    return {
        "repo": repo,
        "pr": number,
        "sha": sha,
        "context": context,
        "state": state,
        "reason": reason,
        "task_ids": [item.get("task_id") for item in provenance.get("resolved") or []],
        "gate_count": len(gate_results),
    }


def _claim_gate_targets(args: argparse.Namespace, primary_repo: str, token: str):
    """Yield (repo, pr, mode) to claim-gate. For an explicit --pr set, only the named
    PRs on the primary repo. Otherwise every project's canonical repo (registry-driven)."""
    skip_drafts = os.environ.get("SWITCHBOARD_CI_SKIP_DRAFTS", "1") != "0"
    if args.pr:
        mode = pr_provenance_gate.resolve_mode(primary_repo, primary_repo)
        for number in args.pr:
            try:
                pr = get_pr(primary_repo, number, token=token)
            except Exception as exc:
                print(json.dumps({"repo": primary_repo, "pr": number, "context": "claim",
                                  "state": "error", "error": str(exc),
                                  "skipped": "pr_lookup_failed"}, sort_keys=True))
                continue
            yield primary_repo, pr, mode
        return
    repos = store.list_canonical_repos()
    ordered = [primary_repo] + [r for r in sorted(repos) if r != primary_repo]
    seen = set()
    for repo in ordered:
        if not repo or repo in seen:
            continue
        seen.add(repo)
        mode = pr_provenance_gate.resolve_mode(repo, primary_repo)
        try:
            prs = list_open_prs(repo, token=token)
        except GateError as exc:
            print(json.dumps({"repo": repo, "context": "claim", "state": "error",
                              "error": str(exc)}, sort_keys=True))
            continue
        for pr in prs:
            if pr.get("draft") and skip_drafts:
                continue
            yield repo, pr, mode


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Post Switchboard / claim gate commit statuses for open fleet PRs.")
    parser.add_argument("--pr", type=int, action="append",
                        help="PR number to claim-gate on the primary repo. Repeatable.")
    parser.add_argument("--once-open-prs", action="store_true",
                        help="Poll open PRs once. Kept explicit for systemd timer readability.")
    parser.add_argument("--repo", default=_repo())
    parser.add_argument("--claim-context",
                        default=os.environ.get("SWITCHBOARD_CI_CLAIM_STATUS_CONTEXT",
                                               DEFAULT_CLAIM_CONTEXT),
                        help="Commit-status context for the SESSION-12 provenance/claim gate. "
                             "Per-repo mode comes from project repo_topology.roles.canonical.claim_gate "
                             "(off|warn|enforce; default warn).")
    parser.add_argument("--merge-context",
                        default=os.environ.get(
                            "SWITCHBOARD_MERGE_STATUS_CONTEXT",
                            DEFAULT_MERGE_CONTEXT,
                        ),
                        help="Required commit-status context backed by merge_gate.")
    parser.add_argument("--no-claim-gate", action="store_true",
                        default=os.environ.get("SWITCHBOARD_CI_NO_CLAIM_GATE", "").lower()
                        in ("1", "true", "yes"),
                        help="Disable only the legacy claim status; merge authorization remains active.")
    parser.add_argument("--fail-on-red", action="store_true",
                        help="Return nonzero when any PR gate posts failure. Manual use only; "
                             "systemd timers should stay green when they successfully post red statuses.")
    args = parser.parse_args(argv)

    token = _token()
    if not token:
        print("ERROR: set PM_GITHUB_TOKEN, GITHUB_TOKEN, or SWITCHBOARD_CI_GITHUB_TOKEN.",
              file=sys.stderr)
        return 2

    results = []
    for repo, pr, mode in _claim_gate_targets(args, args.repo, token):
        if not args.no_claim_gate:
            try:
                claim_result = run_claim_gate_for_pr(
                    pr, repo=repo, token=token, context=args.claim_context, mode=mode)
                if claim_result:
                    print(json.dumps(claim_result, sort_keys=True))
                    results.append(claim_result)
            except Exception as exc:  # pragma: no cover - defensive
                err = {"repo": repo, "pr": pr.get("number"), "context": args.claim_context,
                       "state": "error", "error": str(exc)}
                print(json.dumps(err, sort_keys=True))
                results.append(err)
        try:
            merge_result = run_merge_authorization_for_pr(
                pr, repo=repo, token=token, context=args.merge_context)
            print(json.dumps(merge_result, sort_keys=True))
            results.append(merge_result)
        except Exception as exc:  # pragma: no cover - defensive
            err = {"repo": repo, "pr": pr.get("number"), "context": args.merge_context,
                   "state": "error", "error": str(exc)}
            print(json.dumps(err, sort_keys=True))
            results.append(err)

    failed = [r for r in results if r.get("state") not in ("success", None)]
    return 1 if args.fail_on_red and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
