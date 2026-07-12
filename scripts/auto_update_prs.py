#!/usr/bin/env python3
"""HARDEN-68 (CI-1) — auto-update open PRs so a fleet of agents never hand-merges master.

The merge-war problem: `master` advances every few minutes, so an agent's PR falls behind and
its gate ran against a stale base; the agent then hand-merges master and re-pushes (this session
did that ~6×). This bot does it for them: every run it finds open PRs that are **behind** `master`
and **not conflicting**, and calls GitHub's *update-branch* (merge base → head). That gives each PR
a fresh head tested against current `master`, and — paired with auto-merge (`gh pr merge --auto`)
— the PR lands the moment its gate is green, with zero agent intervention.

It deliberately does NOT touch conflicting PRs (update-branch can't resolve a conflict) — those
need a real merge/resolve, which is the agent's job. This is Lever 4 of ADR-0010; it delivers most
of a merge queue's benefit without the `merge_group` gate wiring (HARDEN-70).

Selection is a pure function (`select_prs_to_update`) so it is unit-tested with no network.
Run:  python scripts/auto_update_prs.py [--repo owner/name] [--base master] [--max 20] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any, Dict, List

DEFAULT_REPO = "6th-Element-Labs/projectplanner"
DEFAULT_BASE = "master"
# A conservative cap so one run can't hammer the API or stampede the gate on a huge backlog.
DEFAULT_MAX = 20

# GitHub mergeStateStatus values that mean "the head is strictly behind the base and can be
# fast-forward-updated cleanly." DIRTY = conflicts (skip); BEHIND = behind base (update).
# UNKNOWN = GitHub is still computing mergeability — treat as "not safe yet" and wait a cycle,
# so we never update-branch a PR that turns out to be conflicting (behind_by is a pure
# commit-graph distance and can't detect a conflict on its own).
_BEHIND = "BEHIND"
_CONFLICTING = "DIRTY"
_UNRESOLVED = ("DIRTY", "UNKNOWN")


def select_prs_to_update(prs: List[Dict[str, Any]], *, base_branch: str = DEFAULT_BASE,
                         max_updates: int = DEFAULT_MAX) -> List[int]:
    """Return the PR numbers to update-branch, most-landable first.

    A PR qualifies when it targets ``base_branch``, is open and not a draft, is **behind** the base
    (``behind_by > 0`` or ``mergeStateStatus == BEHIND``), and is **not** conflicting. Auto-merge-
    armed PRs are ordered first — updating them is what actually lands them. Capped at
    ``max_updates`` so a large backlog is drained a slice at a time, not in one stampede.
    """
    eligible = []
    for pr in prs:
        if pr.get("isDraft"):
            continue
        if (pr.get("baseRefName") or base_branch) != base_branch:
            continue
        state = (pr.get("mergeStateStatus") or "").upper()
        if state in _UNRESOLVED or pr.get("conflicting"):
            continue  # conflicting, or mergeability not yet known — never risk a conflicting update
        behind = int(pr.get("behind_by") or 0) > 0 or state == _BEHIND
        if not behind:
            continue  # already current — a no-op update would burn a gate run for nothing
        eligible.append(pr)
    # Auto-merge-armed PRs first (they land as soon as they're current), then lowest number.
    eligible.sort(key=lambda p: (0 if p.get("autoMerge") else 1, int(p.get("number") or 0)))
    return [int(p["number"]) for p in eligible[:max_updates]]


def _gh_json(args: List[str]) -> Any:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout) if out.stdout.strip() else None


def _fetch_open_prs(repo: str, base: str) -> List[Dict[str, Any]]:
    prs = _gh_json([
        "pr", "list", "--repo", repo, "--state", "open", "--base", base, "--limit", "200",
        "--json", "number,isDraft,baseRefName,headRefName,headRefOid,mergeStateStatus,autoMergeRequest",
    ]) or []
    for pr in prs:
        pr["autoMerge"] = bool(pr.get("autoMergeRequest"))
        # mergeStateStatus BEHIND is only reliable under strict branch protection; compute
        # behind_by directly so this works with strict:false too.
        try:
            cmp = _gh_json(["api", f"repos/{repo}/compare/{base}...{pr['headRefName']}"]) or {}
            pr["behind_by"] = int(cmp.get("behind_by") or 0)
            pr["conflicting"] = (pr.get("mergeStateStatus") or "").upper() == _CONFLICTING
        except subprocess.CalledProcessError:
            pr["behind_by"] = 0  # cross-fork or gone branch — skip rather than guess
    return prs


def _update_branch(repo: str, number: int) -> bool:
    try:
        subprocess.run(["gh", "api", "-X", "PUT", f"repos/{repo}/pulls/{number}/update-branch"],
                       capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError as exc:
        # 422 "merge conflict" / "already up to date" are benign races — log and continue.
        sys.stderr.write(f"update-branch PR #{number} skipped: {exc.stderr.strip()[:160]}\n")
        return False


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Keep open PRs current with the base branch.")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--max", type=int, default=DEFAULT_MAX)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    prs = _fetch_open_prs(args.repo, args.base)
    targets = select_prs_to_update(prs, base_branch=args.base, max_updates=args.max)
    print(f"auto_update_prs: {len(prs)} open → {len(targets)} behind & clean: {targets}")
    if args.dry_run:
        print("(dry-run — no branches updated)")
        return 0
    updated = sum(1 for n in targets if _update_branch(args.repo, n))
    print(f"auto_update_prs: updated {updated}/{len(targets)} PR branch(es)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
