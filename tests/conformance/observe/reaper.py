"""Close PRs / delete branches / archive tasks tagged with one ``run_id``.

`gh`-backed by default (real cleanup once a sandbox repo exists). Every
finder/action is injectable so the unit test exercises the whole flow without
a network call, and the CLI degrades to a dry "what I would delete" report
when `gh` is not on PATH — never a hard failure (constraint: reaper must
still be useful in an environment without `gh`).

Task archival goes through the Switchboard MCP ``archive_task`` tool, which
this in-process script cannot call directly. Callers with MCP access inject
``archive_task`` as a callable; otherwise ``reap`` reports a ``to_archive``
list for an operator/agent to act on afterwards.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


GhRunner = Callable[[list[str]], dict[str, Any]]
ArchiveTaskFn = Callable[[str], dict[str, Any]]
ListRunRunnersFn = Callable[[str], list[dict[str, Any]]]
StopRunnerFn = Callable[[str], dict[str, Any]]

#: Branch naming convention `github_sandbox.py` uses when opening scenario PRs.
RUN_ID_BRANCH_PREFIX = "conformance/{run_id}/"


def gh_available() -> bool:
    return shutil.which("gh") is not None


def _default_gh_runner(args: list[str]) -> dict[str, Any]:
    proc = subprocess.run(
        ["gh", *args], text=True, capture_output=True, check=False, timeout=30,
    )
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
    }


@dataclass
class ReapPlan:
    run_id: str
    repo: str
    pr_numbers: list[int] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)


def find_run_artifacts(
    run_id: str, *, repo: str, gh_runner: Optional[GhRunner] = None,
) -> ReapPlan:
    """List every open PR whose branch is tagged with this ``run_id``."""
    runner = gh_runner or _default_gh_runner
    prefix = RUN_ID_BRANCH_PREFIX.format(run_id=run_id)
    plan = ReapPlan(run_id=run_id, repo=repo)
    result = runner([
        "pr", "list", "--repo", repo, "--state", "open",
        "--json", "number,headRefName", "--limit", "200",
    ])
    if result.get("returncode") != 0:
        return plan
    try:
        rows = json.loads(result.get("stdout") or "[]")
    except json.JSONDecodeError:
        rows = []
    for row in rows:
        branch = str(row.get("headRefName") or "")
        if branch.startswith(prefix):
            plan.pr_numbers.append(int(row.get("number") or 0))
            plan.branches.append(branch)
    return plan


def reap(
    run_id: str,
    *,
    repo: str,
    gh_runner: Optional[GhRunner] = None,
    archive_task: Optional[ArchiveTaskFn] = None,
    list_run_runners: Optional[ListRunRunnersFn] = None,
    stop_runner: Optional[StopRunnerFn] = None,
    task_ids: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Close artifacts and stop every live runner tagged with ``run_id``."""
    task_ids = list(task_ids or [])
    has_gh = gh_runner is not None or gh_available()
    runner = gh_runner or _default_gh_runner
    plan = (
        find_run_artifacts(run_id, repo=repo, gh_runner=runner)
        if has_gh else ReapPlan(run_id=run_id, repo=repo)
    )
    actions: list[dict[str, Any]] = []
    runners = list_run_runners(run_id) if list_run_runners is not None else []
    live_runners = [
        row for row in runners
        if str(row.get("lifecycle_state") or row.get("status") or "").lower()
        not in {"stopped", "completed", "expired", "failed"}
    ]
    for row in live_runners:
        runner_id = str(row.get("runner_session_id") or "")
        action = {"type": "stop_runner", "runner_session_id": runner_id,
                  "dry_run": dry_run}
        if not dry_run:
            if stop_runner is None:
                action["error"] = "stop_runner port is required for live T3 cleanup"
            else:
                action["result"] = stop_runner(runner_id)
        actions.append(action)
    for number in plan.pr_numbers:
        action: dict[str, Any] = {
            "type": "close_pr", "number": number, "dry_run": dry_run,
        }
        if not dry_run:
            action["result"] = runner([
                "pr", "close", str(number), "--repo", repo, "--delete-branch",
            ])
        actions.append(action)
    for task_id in task_ids:
        action = {"type": "archive_task", "task_id": task_id, "dry_run": dry_run}
        if not dry_run and archive_task is not None:
            action["result"] = archive_task(task_id)
        actions.append(action)
    cleanup_errors = [
        action for action in actions
        if action.get("error")
        or (
            isinstance(action.get("result"), dict)
            and (
                action["result"].get("error")
                or action["result"].get("stopped") is False
            )
        )
    ]
    return {
        "run_id": run_id,
        "repo": repo,
        "gh_available": has_gh,
        "pr_numbers": plan.pr_numbers,
        "branches": plan.branches,
        "actions": actions,
        "runner_session_ids": [
            str(row.get("runner_session_id") or "") for row in live_runners
        ],
        "cleanup_complete": not cleanup_errors,
        "cleanup_errors": cleanup_errors,
        "to_archive": [] if (archive_task is not None or dry_run) else task_ids,
        "note": None if has_gh else (
            "gh not found on PATH -- PRs were not discovered or removed; "
            "runner cleanup was still evaluated."
        ),
    }


__all__ = [
    "RUN_ID_BRANCH_PREFIX", "ReapPlan", "gh_available", "find_run_artifacts",
    "reap",
]
