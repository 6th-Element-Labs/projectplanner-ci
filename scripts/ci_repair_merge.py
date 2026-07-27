#!/usr/bin/env python3
"""Audited human half of the CI-repair lane.

The GitHub App first mirrors and verifies the exact PR head through the ordinary
``verify.yml`` workflow:

    python jobs.py dispatch_scratchpad --pr 123 --head-sha <sha> \
      --purpose ci_repair --actor <operator> --reason "<why>" --dispatch --json

This script then proves that exact run and status, records the repair audit on
the PR, and optionally performs the administrator squash merge. It bypasses
the queue, never the test suite.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any
from urllib.parse import urlparse


CONTEXT = "Switchboard CI / VM gate"
DEFAULT_REPO = "6th-Element-Labs/projectplanner"
CI_REPO = "6th-Element-Labs/projectplanner-ci"
RUN_PATH = re.compile(r"^/6th-Element-Labs/projectplanner-ci/actions/runs/(\d+)$")


def gh(*args: str) -> Any:
    proc = subprocess.run(
        ["gh", *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout or "gh failed").strip())
    text = (proc.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def run_id_from_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    match = RUN_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or not match:
        raise ValueError(
            "run URL must be an exact projectplanner-ci Actions run URL")
    return match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify, audit, and optionally admin-merge an exact-SHA CI repair.")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--actor", default="")
    parser.add_argument("--merge", action="store_true")
    args = parser.parse_args()

    sha = args.sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        parser.error("--sha must be exactly 40 lowercase hexadecimal characters")
    reason = args.reason.strip()
    if not reason:
        parser.error("--reason must not be empty")
    run_id = run_id_from_url(args.run_url)

    pr = gh(
        "pr", "view", str(args.pr), "--repo", args.repo,
        "--json",
        "state,headRefOid,baseRefName,mergeable,mergeStateStatus,url",
    )
    if pr.get("state") != "OPEN":
        raise RuntimeError(f"PR #{args.pr} is not open")
    if pr.get("headRefOid") != sha:
        raise RuntimeError(
            f"PR head changed: expected {sha}, found {pr.get('headRefOid')}")
    if pr.get("mergeable") == "CONFLICTING":
        raise RuntimeError("PR is conflicting; repair the branch before CI-repair merge")

    base_ref = gh(
        "api", f"repos/{args.repo}/git/ref/heads/{pr['baseRefName']}")
    base_sha = ((base_ref or {}).get("object") or {}).get("sha")
    comparison = gh(
        "api", f"repos/{args.repo}/compare/{base_sha}...{sha}")
    if comparison.get("merge_base_commit", {}).get("sha") != base_sha:
        raise RuntimeError(
            "PR branch is not current with the base branch; update it and rerun exact-SHA CI")

    provider = gh(
        "run", "view", run_id, "--repo", CI_REPO,
        "--json", "conclusion,status,displayTitle,url")
    if provider.get("status") != "completed" or provider.get("conclusion") != "success":
        raise RuntimeError(f"CI run {run_id} is not completed successfully")
    if sha not in str(provider.get("displayTitle") or ""):
        raise RuntimeError("CI run title does not identify the requested exact SHA")
    if provider.get("url") != args.run_url:
        raise RuntimeError("CI run URL does not match the provider readback")

    combined = gh("api", f"repos/{args.repo}/commits/{sha}/status")
    statuses = [
        row for row in (combined.get("statuses") or [])
        if row.get("context") == CONTEXT
    ]
    status = statuses[0] if statuses else {}
    if status.get("state") != "success" or status.get("target_url") != args.run_url:
        raise RuntimeError(
            f"{CONTEXT} is not green on the exact SHA with the supplied run URL")

    # Re-read after all remote verification so neither base nor head can move
    # unnoticed between the tested object and the administrator merge.
    fresh_pr = gh(
        "pr", "view", str(args.pr), "--repo", args.repo,
        "--json", "state,headRefOid,baseRefName")
    fresh_base = gh(
        "api", f"repos/{args.repo}/git/ref/heads/{fresh_pr['baseRefName']}")
    if fresh_pr.get("state") != "OPEN" or fresh_pr.get("headRefOid") != sha:
        raise RuntimeError("PR changed while the CI-repair receipt was being verified")
    if ((fresh_base or {}).get("object") or {}).get("sha") != base_sha:
        raise RuntimeError("base branch moved; update the PR and rerun exact-SHA CI")

    actor = args.actor.strip()
    if not actor:
        actor = (gh("api", "user") or {}).get("login") or "unknown"
    body = "\n".join([
        "### ci-repair",
        "",
        "Audited administrator repair lane; queue bypassed, test suite not bypassed.",
        "",
        f"- Exact PR head: `{sha}`",
        f"- Exact base: `{base_sha}`",
        f"- Green workflow: {args.run_url}",
        f"- Operator: `{actor}`",
        f"- Reason: {reason}",
    ])
    gh(
        "label", "create", "ci-repair", "--repo", args.repo,
        "--color", "B60205",
        "--description", "Audited exact-SHA CI repair and administrator merge",
        "--force",
    )
    gh("pr", "edit", str(args.pr), "--repo", args.repo, "--add-label", "ci-repair")
    gh("pr", "comment", str(args.pr), "--repo", args.repo, "--body", body)

    result = {
        "schema": "switchboard.ci_repair.v1",
        "pr": args.pr,
        "sha": sha,
        "base_sha": base_sha,
        "run_url": args.run_url,
        "actor": actor,
        "reason": reason,
        "audited": True,
        "merged": False,
    }
    if args.merge:
        gh(
            "pr", "merge", str(args.pr), "--repo", args.repo,
            "--admin", "--squash", "--match-head-commit", sha,
        )
        merged = gh(
            "pr", "view", str(args.pr), "--repo", args.repo,
            "--json", "state,mergedAt,mergeCommit,url")
        if merged.get("state") != "MERGED":
            raise RuntimeError("administrator merge command returned but PR is not merged")
        result["merged"] = True
        result["merged_at"] = merged.get("mergedAt")
        result["merge_sha"] = (merged.get("mergeCommit") or {}).get("oid")

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"ci-repair refused: {exc}", file=sys.stderr)
        raise SystemExit(1)
