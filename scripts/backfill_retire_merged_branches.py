#!/usr/bin/env python3
"""BUG-29 backfill: archive-then-delete merged (and triaged abandoned) PR head branches.

Uses store.retire_merged_branch (refs/tags/archive/<branch> at head SHA, then delete
refs/heads/<branch>). PR records on GitHub are untouched.

Examples:
  PM_RETIRE_MERGED_BRANCHES=1 .venv/bin/python scripts/backfill_retire_merged_branches.py --dry-run
  PM_RETIRE_MERGED_BRANCHES=1 .venv/bin/python scripts/backfill_retire_merged_branches.py
  PM_RETIRE_MERGED_BRANCHES=1 .venv/bin/python scripts/backfill_retire_merged_branches.py \\
      --abandoned chore/add-cloud-mcp-json codex/PROJECT-HIERARCHY-model
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Dict, List, Optional, Set, Tuple

REPO = "6th-Element-Labs/projectplanner"
DEFAULT_BRANCH = "master"


def _gh_json(args: List[str]) -> object:
    out = subprocess.check_output(["gh", *args], text=True)
    if not out.strip():
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # gh --paginate may emit one JSON object per line.
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows


def remote_branches() -> Dict[str, str]:
    """branch -> head sha"""
    rows = _gh_json([
        "api", f"repos/{REPO}/branches?per_page=100", "--paginate",
        "--jq", ".[] | {name: .name, sha: .commit.sha}",
    ])
    out: Dict[str, str] = {}
    for row in rows:
        name = (row.get("name") or "").strip()
        sha = (row.get("sha") or "").strip()
        if name and sha and name != DEFAULT_BRANCH:
            out[name] = sha
    return out


def archive_tags() -> Set[str]:
    out: Set[str] = set()
    try:
        raw = subprocess.check_output(
            ["git", "ls-remote", "--tags", f"https://github.com/{REPO}.git",
             "refs/tags/archive/*"],
            text=True,
        )
    except subprocess.CalledProcessError:
        return out
    for line in raw.splitlines():
        if not line.strip():
            continue
        _sha, ref = line.split("\t", 1)
        out.add(ref.removeprefix("refs/tags/archive/"))
    return out


def pr_index() -> Dict[str, dict]:
    prs = _gh_json([
        "pr", "list", "--repo", REPO, "--state", "all", "--limit", "500",
        "--json", "number,state,mergedAt,headRefName,title",
    ])
    by: Dict[str, dict] = {}
    for pr in prs:
        head = (pr.get("headRefName") or "").strip()
        if not head:
            continue
        cur = by.get(head)
        if not cur or int(pr.get("number") or 0) > int(cur.get("number") or 0):
            by[head] = pr
    return by


def classify(branches: Dict[str, str], prs: Dict[str, dict],
             archive: Set[str], abandoned: Set[str]) -> Tuple[List[str], List[str], List[str]]:
    keep: List[str] = []
    retire_merged: List[str] = []
    retire_abandoned: List[str] = []
    for name in sorted(branches):
        pr = prs.get(name)
        if pr and pr.get("state") == "OPEN":
            keep.append(name)
            continue
        if name in abandoned:
            retire_abandoned.append(name)
            continue
        if pr and pr.get("mergedAt"):
            retire_merged.append(name)
            continue
        if pr and pr.get("state") == "CLOSED" and not pr.get("mergedAt"):
            # Closed without merge: only retire when explicitly listed in --abandoned.
            keep.append(name)
            continue
        if name not in prs:
            # Orphan branch with no PR: only retire when explicitly abandoned.
            keep.append(name)
    return keep, retire_merged, retire_abandoned


def _ensure_github_token() -> None:
    if os.environ.get("PM_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get(
            "SWITCHBOARD_CI_GITHUB_TOKEN"):
        return
    try:
        token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    if token:
        os.environ["GITHUB_TOKEN"] = token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--abandoned", nargs="*", default=[],
        help="Closed-unmerged or no-PR branches approved for archive+delete",
    )
    parser.add_argument(
        "--include-merged", action=argparse.BooleanOptionalAction, default=True,
        help="Retire branches whose latest PR was merged (default: on)",
    )
    args = parser.parse_args()

    if not (os.environ.get("PM_RETIRE_MERGED_BRANCHES") or "").strip():
        print("Set PM_RETIRE_MERGED_BRANCHES=1 before running (safety gate).", file=sys.stderr)
        return 2

    _ensure_github_token()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import store  # noqa: E402

    branches = remote_branches()
    prs = pr_index()
    archive = archive_tags()
    abandoned = set(args.abandoned)
    keep, retire_merged, retire_abandoned = classify(branches, prs, archive, abandoned)

    targets: List[Tuple[str, str]] = []
    if args.include_merged:
        for name in retire_merged:
            targets.append((name, "merged_pr"))
    for name in retire_abandoned:
        targets.append((name, "abandoned"))

    print(f"repo={args.repo} remote_branches={len(branches)} archive_tags={len(archive)}")
    print(f"keep_open_or_unlisted={len(keep)} retire_merged={len(retire_merged)} "
          f"retire_abandoned={len(retire_abandoned)}")
    if keep:
        print("keeping:", ", ".join(keep[:20]) + (" ..." if len(keep) > 20 else ""))

    ok = fail = 0
    for name, reason in targets:
        sha = branches[name]
        has_archive = name in archive
        print(f"{'DRY' if args.dry_run else 'RET'} {name} sha={sha[:7]} reason={reason} "
              f"archive_tag={'yes' if has_archive else 'no'}")
        if args.dry_run:
            ok += 1
            continue
        res = store.retire_merged_branch(args.repo, name, sha)
        if res.get("retired"):
            ok += 1
            print(f"  retired archived={res.get('archived')} deleted={res.get('deleted')} "
                  f"already_gone={res.get('already_gone')}")
        else:
            fail += 1
            print(f"  FAILED {res}", file=sys.stderr)

    print(f"done ok={ok} fail={fail} dry_run={args.dry_run}")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
