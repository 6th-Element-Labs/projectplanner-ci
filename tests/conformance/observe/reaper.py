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
    task_ids: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Close + delete-branch every PR tagged ``run_id``; report task archival intent."""
    task_ids = list(task_ids or [])
    if gh_runner is None and not gh_available():
        return {
            "run_id": run_id,
            "repo": repo,
            "gh_available": False,
            "pr_numbers": [],
            "branches": [],
            "actions": [],
            "to_archive": task_ids,
            "note": (
                "gh not found on PATH -- nothing discovered or removed. "
                "Install gh (or run where it is available) to reap for real; "
                f"would have searched {repo!r} for branches under "
                f"{RUN_ID_BRANCH_PREFIX.format(run_id=run_id)!r}."
            ),
        }
    runner = gh_runner or _default_gh_runner
    plan = find_run_artifacts(run_id, repo=repo, gh_runner=runner)
    actions: list[dict[str, Any]] = []
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
    return {
        "run_id": run_id,
        "repo": repo,
        "gh_available": True,
        "pr_numbers": plan.pr_numbers,
        "branches": plan.branches,
        "actions": actions,
        "to_archive": [] if (archive_task is not None or dry_run) else task_ids,
    }


__all__ = [
    "RUN_ID_BRANCH_PREFIX", "ReapPlan", "gh_available", "find_run_artifacts",
    "reap",
]
