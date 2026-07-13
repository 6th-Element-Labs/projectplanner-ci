"""Scratchpad CI dispatch (CI-12).

On canonical PR open/sync, push the exact head SHA to a disposable ``ci/**`` branch on
``projectplanner-ci``. The push-triggered ``verify`` workflow (CI-14) runs the suite and
posts ``Switchboard CI / VM gate`` on the private canonical SHA.

Replaces pull-model ``repository_dispatch`` as the primary projectplanner verification path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import ci_verify_dispatch as cvd
import external_ci_mirror
import store

SCHEMA = "switchboard.ci_scratchpad_dispatch.v1"
DEFAULT_WORKFLOW = "verify"


def is_scratchpad_enabled() -> bool:
    """Scratchpad is primary when enabled; pull-model is fallback only when off."""
    value = (os.environ.get("SWITCHBOARD_CI_SCRATCHPAD") or "1").strip().lower()
    return value in ("1", "true", "yes", "on")


def source_checkout_path() -> str:
    return (
        os.environ.get("SWITCHBOARD_CI_SOURCE_PATH")
        or os.environ.get("PM_WORK_SESSION_SOURCE_PATH")
        or str(Path(__file__).resolve().parent)
    )


def mirror_branch_for_pr(pr_number: int, source_sha: str) -> str:
    return store.default_external_ci_mirror_branch(f"pr-{int(pr_number)}", source_sha)


def dispatch_scratchpad(
    pr_number: int,
    *,
    head_sha: str = "",
    repo: str = "",
    project: str = "switchboard",
    source_path: str = "",
    dry_run: bool = False,
    strict_explicit: bool = False,
) -> Dict[str, Any]:
    """Push one PR head SHA to the public scratchpad and record an external_ci_run."""
    pr = int(pr_number)
    source_repo = cvd.canonical_repo(repo)
    tok = cvd._token("")
    if not tok:
        raise cvd.CiVerifyDispatchError(
            "A GitHub token is required to resolve PR head SHA for scratchpad CI."
        )
    sha, sha_source, stale_webhook_sha = cvd.resolve_head_sha(
        pr, head_sha, repo=source_repo, token=tok, strict_explicit=strict_explicit)
    cvd.verify_commit_exists(sha, repo=source_repo, token=tok)
    checkout = (source_path or source_checkout_path()).strip()
    mirror_branch = mirror_branch_for_pr(pr, sha)
    result: Dict[str, Any] = {
        "schema": SCHEMA,
        "dispatched": False,
        "dry_run": dry_run,
        "canonical_repo": source_repo,
        "ci_repo": cvd.ci_repo(),
        "pr": pr,
        "head_sha": sha,
        "head_sha_source": sha_source,
        "stale_webhook_sha": stale_webhook_sha,
        "mirror_branch": mirror_branch,
        "workflow": DEFAULT_WORKFLOW,
        "source_path": checkout,
    }
    if dry_run:
        result["message"] = "validated only — no scratchpad mirror push sent"
        return result
    if not os.path.isdir(checkout):
        raise cvd.CiVerifyDispatchError(
            f"scratchpad source checkout missing or not a directory: {checkout}"
        )
    request = {
        "source_project": project,
        "source_repo": source_repo,
        "source_sha": sha,
        "mirror_repo": cvd.ci_repo(),
        "mirror_branch": mirror_branch,
        "workflow": DEFAULT_WORKFLOW,
        "push_triggered": True,
        "poll_after_push": False,
        "request": {
            "pr": pr,
            "schema": SCHEMA,
            "push_triggered": True,
        },
    }
    mirror = external_ci_mirror.request_external_ci_mirror_run(
        request, checkout, actor="github-webhook", project=project)
    result["mirror"] = mirror
    result["run_id"] = mirror.get("run_id")
    result["dispatched"] = bool(mirror.get("ok")) and not mirror.get("error")
    if mirror.get("error"):
        result["error"] = mirror["error"]
        result["message"] = str(mirror["error"])
    else:
        result["message"] = (
            f"scratchpad push sent to {cvd.ci_repo()}:{mirror_branch} "
            f"(run_id={mirror.get('run_id')})"
        )
    return result


def try_dispatch_scratchpad(
    pr_number: int,
    *,
    head_sha: str = "",
    repo: str = "",
    project: str = "switchboard",
    source_path: str = "",
) -> Dict[str, Any]:
    """Best-effort scratchpad dispatch for webhook handlers — never raises."""
    if not is_scratchpad_enabled():
        return {
            "dispatched": False,
            "skip_reason": "scratchpad_disabled",
            "pr": int(pr_number),
        }
    try:
        out = dispatch_scratchpad(
            pr_number,
            head_sha=head_sha,
            repo=repo,
            project=project,
            source_path=source_path,
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
        description="Dispatch scratchpad CI (push ci/** branch on projectplanner-ci).",
    )
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head-sha", default="")
    parser.add_argument("--repo", default="")
    parser.add_argument("--project", default="switchboard")
    parser.add_argument("--source-path", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dispatch", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    dry_run = args.dry_run or not args.dispatch
    try:
        result = dispatch_scratchpad(
            args.pr,
            head_sha=args.head_sha,
            repo=args.repo,
            project=args.project,
            source_path=args.source_path,
            dry_run=dry_run,
            strict_explicit=bool((args.head_sha or "").strip()),
        )
    except cvd.CiVerifyDispatchError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    mode = "dry-run" if dry_run else "dispatched"
    print(f"ok ({mode})")
    print(f"  pr            {result['pr']}")
    print(f"  head_sha      {result['head_sha']} ({result['head_sha_source']})")
    if result.get("stale_webhook_sha"):
        print(f"  stale_webhook ignored {result['stale_webhook_sha']}")
    print(f"  mirror_branch {result['mirror_branch']}")
    print(f"  {result.get('message', '')}")
    return 0


def main(argv: Optional[list] = None) -> int:
    return _cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
