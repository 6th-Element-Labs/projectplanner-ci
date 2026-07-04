"""Git preflight checks for Switchboard review and audit workflows."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


class ReviewPreflightError(RuntimeError):
    pass


def _git(repo: Path, args: List[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _git_out(repo: Path, args: List[str], *, check: bool = True) -> str:
    result = _git(repo, args, check=check)
    return result.stdout.strip()


def _bool_env(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _finding(severity: str, code: str, detail: str, *, blocking: bool = True) -> Dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "failure_class": "stale_branch" if "stale" in code or "behind" in code else "failed_gate",
        "detail": detail,
        "blocking": blocking,
    }


def run_git_review_preflight(
    repo_path: str | Path,
    *,
    target_ref: str = "HEAD",
    upstream_ref: str = "",
    intended_project: str = "",
    intended_branch: str = "",
    require_clean: bool = True,
    allow_dirty: bool = False,
    allow_behind: bool = False,
) -> Dict[str, Any]:
    """Return a machine-readable git preflight report for a review target.

    A red report means reviewer/audit agents should not be spawned against this
    target unless an operator deliberately overrides the named finding.
    """
    repo = Path(repo_path).resolve()
    if not repo.exists():
        raise ReviewPreflightError(f"repo path does not exist: {repo}")
    if _git(repo, ["rev-parse", "--is-inside-work-tree"], check=False).returncode != 0:
        raise ReviewPreflightError(f"not a git worktree: {repo}")

    findings: List[Dict[str, Any]] = []
    current_branch = _git_out(repo, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    head_sha = _git_out(repo, ["rev-parse", f"{target_ref}^{{commit}}"], check=False)
    if not head_sha:
        findings.append(_finding(
            "critical", "target_ref_not_found",
            f"Target ref {target_ref!r} is not reachable in this checkout.",
        ))

    dirty_lines = _git_out(repo, ["status", "--porcelain", "--untracked-files=all"], check=False).splitlines()
    dirty_files = [line[3:] if len(line) > 3 else line for line in dirty_lines if line.strip()]
    if require_clean and dirty_files:
        findings.append(_finding(
            "high" if not allow_dirty else "medium",
            "dirty_worktree",
            f"Review target has {len(dirty_files)} uncommitted/untracked file(s).",
            blocking=not allow_dirty,
        ))

    upstream_sha = ""
    merge_base = ""
    behind = ahead = None
    if upstream_ref:
        upstream_sha = _git_out(repo, ["rev-parse", f"{upstream_ref}^{{commit}}"], check=False)
        if not upstream_sha:
            findings.append(_finding(
                "high", "upstream_ref_not_found",
                f"Upstream ref {upstream_ref!r} is not reachable in this checkout.",
            ))
        elif head_sha:
            merge_base = _git_out(repo, ["merge-base", upstream_sha, head_sha], check=False)
            counts = _git_out(repo, ["rev-list", "--left-right", "--count",
                                     f"{upstream_sha}...{head_sha}"], check=False)
            try:
                left, right = counts.split()
                behind, ahead = int(left), int(right)
            except Exception:
                findings.append(_finding(
                    "high", "branch_distance_unavailable",
                    f"Could not compute branch distance for {upstream_ref!r}...{target_ref!r}.",
                ))
            if behind and behind > 0:
                findings.append(_finding(
                    "high" if not allow_behind else "medium",
                    "target_branch_behind_upstream",
                    f"Target is {behind} commit(s) behind {upstream_ref!r}.",
                    blocking=not allow_behind,
                ))

    blocking = [f for f in findings if f.get("blocking")]
    status = "red" if blocking else ("yellow" if findings else "pass")
    return {
        "schema": "switchboard.review_git_preflight.v1",
        "ok": status == "pass",
        "status": status,
        "project": intended_project,
        "intended_branch": intended_branch,
        "repo_path": str(repo),
        "target_ref": target_ref,
        "target_sha": head_sha,
        "current_branch": current_branch,
        "upstream_ref": upstream_ref,
        "upstream_sha": upstream_sha,
        "merge_base": merge_base,
        "branch_distance": {
            "behind": behind,
            "ahead": ahead,
        },
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files[:50],
        "dirty_count": len(dirty_files),
        "override": {
            "allow_dirty": bool(allow_dirty),
            "allow_behind": bool(allow_behind),
        },
        "findings": findings,
    }


def format_preflight_header(report: Dict[str, Any]) -> str:
    """Human-readable report header for review/audit logs."""
    lines = [
        "Switchboard review git preflight",
        f"status={report.get('status')}",
        f"project={report.get('project') or '(unspecified)'}",
        f"intended_branch={report.get('intended_branch') or '(unspecified)'}",
        f"repo_path={report.get('repo_path')}",
        f"target_ref={report.get('target_ref')} target_sha={report.get('target_sha')}",
        f"upstream_ref={report.get('upstream_ref') or '(none)'} upstream_sha={report.get('upstream_sha') or '(none)'}",
        f"branch_distance={json.dumps(report.get('branch_distance') or {}, sort_keys=True)}",
        f"dirty={report.get('dirty')} dirty_count={report.get('dirty_count')}",
    ]
    for finding in report.get("findings") or []:
        lines.append(
            f"- [{finding.get('severity')}] {finding.get('code')}: {finding.get('detail')}"
        )
    return "\n".join(lines) + "\n"
