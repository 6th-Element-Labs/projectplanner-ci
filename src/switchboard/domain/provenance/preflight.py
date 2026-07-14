"""Repo/worktree preflight — FS/git inspection (ARCH-MS-58).

Moved from ``repositories/shell.py``. This is **not** a SQL repository: it
inspects a local checkout and returns a typed pass/warn/deny report.

The report is side-effect-free (no ``git`` mutations; read-only subprocess and
filesystem scans). ``merge_gate`` / ``pre_tool_check`` / work-session helpers
consume the report via the store façade re-export. Lease-collision checks reach
persistence through a lazy store façade so this module stays free of owning SQL.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from constants import DEFAULT_PROJECT, GITHUB_REPO_RE, REPO_PREFLIGHT_SCHEMA


def _store_facade():
    """Resolve board/topology/lease helpers after store.py is initialized."""
    import store
    return store


def _conn(project: str = DEFAULT_PROJECT):
    from db.connection import _conn as conn_impl
    return conn_impl(project)


def _json_obj(raw: Any, default: Any):
    from db.core import _json_obj as json_obj_impl
    return json_obj_impl(raw, default)


def _repo_preflight_finding(code: str, message: str, failure_class: str,
                            severity: str = "high", blocking: bool = True,
                            details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "code": code,
        "failure_class": failure_class,
        "severity": severity,
        "blocking": bool(blocking),
        "message": message,
        **(details or {}),
    }


def _repo_git(repo_path: str, args: List[str], timeout_seconds: int = 10) -> Dict[str, Any]:
    try:
        completed = subprocess.run(
            ["git", "-C", repo_path, *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": (completed.stdout or "").strip(),
            "stderr": (completed.stderr or "").strip(),
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(exc)}


def _repo_remote_slug(remote_url: str) -> str:
    text = (remote_url or "").strip()
    if not text:
        return ""
    match = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", text)
    if match:
        return match.group(1).removesuffix(".git")
    if GITHUB_REPO_RE.match(text):
        return text.removesuffix(".git")
    return ""


def _repo_parse_status(lines: List[str]) -> Tuple[List[str], List[str]]:
    dirty: List[str] = []
    untracked: List[str] = []
    for line in lines:
        if not line.strip():
            continue
        path = line[3:] if len(line) > 3 else line.strip()
        if line.startswith("?? "):
            untracked.append(path)
        else:
            dirty.append(path)
    return dirty, untracked


def _repo_git_dir(repo_path: str) -> str:
    git_dir = _repo_git(repo_path, ["rev-parse", "--git-dir"])
    if not git_dir.get("ok"):
        return ""
    raw = git_dir.get("stdout") or ""
    if os.path.isabs(raw):
        return raw
    return os.path.abspath(os.path.join(repo_path, raw))


def _repo_merge_state(git_dir: str) -> Dict[str, Any]:
    if not git_dir:
        return {"active": False, "states": []}
    checks = {
        "merge": "MERGE_HEAD",
        "rebase_merge": "rebase-merge",
        "rebase_apply": "rebase-apply",
        "cherry_pick": "CHERRY_PICK_HEAD",
        "revert": "REVERT_HEAD",
    }
    active = [name for name, rel in checks.items() if os.path.exists(os.path.join(git_dir, rel))]
    return {"active": bool(active), "states": active}


def _repo_list_candidate_files(repo_path: str, max_files: int) -> List[str]:
    listed = _repo_git(repo_path, ["ls-files", "-co", "--exclude-standard"], timeout_seconds=20)
    if not listed.get("ok"):
        return []
    return [line for line in (listed.get("stdout") or "").splitlines() if line.strip()][:max_files]


def _repo_scan_conflict_markers(repo_path: str, max_files: int = 4000,
                                max_file_bytes: int = 1024 * 1024) -> List[Dict[str, Any]]:
    markers: List[Dict[str, Any]] = []
    for rel in _repo_list_candidate_files(repo_path, max_files=max_files):
        full = os.path.abspath(os.path.join(repo_path, rel))
        if not full.startswith(os.path.abspath(repo_path) + os.sep):
            continue
        try:
            if not os.path.isfile(full) or os.path.getsize(full) > max_file_bytes:
                continue
            with open(full, "rb") as fh:
                raw = fh.read(max_file_bytes + 1)
            if b"\0" in raw:
                continue
            text = raw.decode("utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("<<<<<<<", ">>>>>>>")):
                markers.append({"path": rel, "line": lineno, "marker": stripped[:16]})
                break
    return markers


def _repo_worktree_collisions(path: str, agent_id: str,
                              project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    s = _store_facade()
    if not s.has_project(project):
        return []
    path_real = os.path.realpath(os.path.abspath(path))
    collisions: List[Dict[str, Any]] = []
    now = time.time()
    with _conn(project) as c:
        for lease in s._active_resource_leases_in(c, now, "worktree"):
            if lease.get("agent_id") == agent_id:
                continue
            names = _json_obj(lease.get("names") or "[]", [])
            for name in names:
                if os.path.realpath(os.path.abspath(str(name))) == path_real:
                    collisions.append({
                        "lease_id": lease.get("id"),
                        "agent_id": lease.get("agent_id"),
                        "task_id": lease.get("task_id"),
                        "name": str(name),
                        "expires_at": lease.get("claimed_at", 0) + lease.get("ttl_seconds", 0),
                    })
    return collisions


def repo_preflight(worktree_path: str, project: str = DEFAULT_PROJECT,
                   task_id: str = "", agent_id: str = "",
                   repo_role: str = "canonical", expected_branch: str = "",
                   expected_base_ref: str = "", scan_conflicts: bool = True,
                   max_scan_files: int = 4000) -> Dict[str, Any]:
    """Inspect a local git worktree before agents edit, claim, complete, or merge.

    The report is side-effect-free and returns pass/warn/deny plus typed findings
    that adapters and hosts can enforce without inferring from prose.
    """
    now = time.time()
    path = os.path.abspath(os.path.expanduser(str(worktree_path or "").strip()))
    findings: List[Dict[str, Any]] = []
    s = _store_facade()
    topology = s.get_project_repo_topology(project) if s.has_project(project) else {}
    roles = topology.get("roles") or {}
    role = roles.get(repo_role) or {}
    default_branch = (role.get("default_branch") or "").strip()
    base_ref = (expected_base_ref or (f"origin/{default_branch}" if default_branch else "")).strip()
    report: Dict[str, Any] = {
        "schema": REPO_PREFLIGHT_SCHEMA,
        "project": project,
        "task_id": (task_id or "").strip().upper(),
        "agent_id": (agent_id or "").strip(),
        "repo_role": (repo_role or "").strip() or "canonical",
        "repo_path": path,
        "expected_branch": (expected_branch or "").strip(),
        "expected_base_ref": base_ref,
        "created_at": now,
        "verdict": "deny",
        "ok": False,
        "findings": findings,
    }
    if not s.has_project(project):
        findings.append(_repo_preflight_finding(
            "unknown_project", f"Unknown project: {project}", "wrong_repo"))
        return report
    if not os.path.isdir(path):
        findings.append(_repo_preflight_finding(
            "worktree_missing", f"Worktree path does not exist: {path}", "wrong_repo"))
        return report
    inside = _repo_git(path, ["rev-parse", "--is-inside-work-tree"])
    if not inside.get("ok") or inside.get("stdout") != "true":
        findings.append(_repo_preflight_finding(
            "not_git_worktree", "Path is not inside a git worktree.", "wrong_repo",
            details={"stderr": inside.get("stderr") or ""}))
        return report

    root = _repo_git(path, ["rev-parse", "--show-toplevel"])
    repo_path = os.path.abspath(root.get("stdout") or path)
    report["repo_path"] = repo_path
    git_dir = _repo_git_dir(repo_path)
    report["git_dir"] = git_dir

    remote = _repo_git(repo_path, ["remote", "get-url", "origin"])
    remote_url = remote.get("stdout") if remote.get("ok") else ""
    remote_slug = _repo_remote_slug(remote_url)
    expected_repo = (role.get("repo") or "").strip()
    expected_slug = _repo_remote_slug(expected_repo)
    report["remote"] = {"name": "origin", "url": remote_url, "repo": remote_slug}
    report["expected_repo"] = expected_repo
    if expected_slug and remote_slug and remote_slug.lower() != expected_slug.lower():
        findings.append(_repo_preflight_finding(
            "wrong_repo",
            f"origin repo {remote_slug} does not match project {project} {repo_role} repo {expected_slug}.",
            "wrong_repo",
            details={"actual_repo": remote_slug, "expected_repo": expected_slug}))

    branch = _repo_git(repo_path, ["branch", "--show-current"])
    current_branch = branch.get("stdout") if branch.get("ok") else ""
    head = _repo_git(repo_path, ["rev-parse", "HEAD"])
    report["branch"] = current_branch
    report["head_sha"] = head.get("stdout") if head.get("ok") else ""
    if not current_branch:
        findings.append(_repo_preflight_finding(
            "detached_head", "Worktree is in detached HEAD state.", "detached_head"))

    expected = (expected_branch or "").strip()
    if expected and current_branch != expected:
        findings.append(_repo_preflight_finding(
            "wrong_branch",
            f"Current branch {current_branch or '(detached)'} does not match expected branch {expected}.",
            "wrong_branch",
            details={"actual_branch": current_branch, "expected_branch": expected}))
    elif not expected and task_id and agent_id and current_branch and not s._branch_matches_task(
            agent_id, task_id, current_branch):
        findings.append(_repo_preflight_finding(
            "wrong_branch",
            f"Current branch {current_branch} is not task-scoped for {task_id}.",
            "wrong_branch",
            details={"actual_branch": current_branch, "task_id": task_id}))

    upstream = _repo_git(repo_path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    upstream_ref = upstream.get("stdout") if upstream.get("ok") else ""
    report["upstream"] = upstream_ref
    if not upstream_ref:
        findings.append(_repo_preflight_finding(
            "missing_upstream", "Branch has no upstream tracking ref.", "missing_upstream",
            severity="medium", blocking=False, details={"stderr": upstream.get("stderr") or ""}))
    else:
        upstream_sha = _repo_git(repo_path, ["rev-parse", f"{upstream_ref}^{{commit}}"])
        report["upstream_sha"] = upstream_sha.get("stdout") if upstream_sha.get("ok") else ""
        counts = _repo_git(repo_path, ["rev-list", "--left-right", "--count", f"HEAD...{upstream_ref}"])
        if counts.get("ok"):
            try:
                ahead, behind = [int(x) for x in counts.get("stdout", "0 0").split()]
                report["upstream_distance"] = {"ahead": ahead, "behind": behind}
            except ValueError:
                findings.append(_repo_preflight_finding(
                    "upstream_distance_unavailable",
                    "Could not parse ahead/behind distance to upstream.",
                    "git_signal_unavailable", severity="medium", blocking=False))

    if base_ref:
        base_sha = _repo_git(repo_path, ["rev-parse", f"{base_ref}^{{commit}}"])
        if base_sha.get("ok"):
            report["base_ref"] = base_ref
            report["base_sha"] = base_sha.get("stdout")
            merge_base = _repo_git(repo_path, ["merge-base", "HEAD", base_ref])
            report["merge_base"] = merge_base.get("stdout") if merge_base.get("ok") else ""
            base_counts = _repo_git(repo_path, ["rev-list", "--left-right", "--count", f"HEAD...{base_ref}"])
            if base_counts.get("ok"):
                try:
                    ahead_base, behind_base = [int(x) for x in base_counts.get("stdout", "0 0").split()]
                    report["base_distance"] = {"ahead": ahead_base, "behind": behind_base}
                    if behind_base > 0:
                        findings.append(_repo_preflight_finding(
                            "stale_base",
                            f"Branch is {behind_base} commit(s) behind {base_ref}.",
                            "stale_base",
                            details={"base_ref": base_ref, "behind": behind_base}))
                except ValueError:
                    findings.append(_repo_preflight_finding(
                        "base_distance_unavailable",
                        "Could not parse ahead/behind distance to base ref.",
                        "git_signal_unavailable", severity="medium", blocking=False))
        else:
            findings.append(_repo_preflight_finding(
                "missing_base_ref",
                f"Base ref {base_ref!r} is not reachable in this checkout.",
                "missing_base_ref", severity="medium", blocking=False,
                details={"stderr": base_sha.get("stderr") or ""}))

    status = _repo_git(repo_path, ["status", "--porcelain=v1", "-uall"], timeout_seconds=20)
    status_lines = (status.get("stdout") or "").splitlines() if status.get("ok") else []
    dirty_files, untracked_files = _repo_parse_status(status_lines)
    report["git_status"] = {"porcelain": status_lines[:200], "count": len(status_lines)}
    report["dirty"] = bool(status_lines)
    report["dirty_files"] = dirty_files[:100]
    report["untracked_files"] = untracked_files[:100]
    if status_lines:
        findings.append(_repo_preflight_finding(
            "dirty_worktree",
            f"Worktree has {len(status_lines)} dirty or untracked file(s).",
            "dirty_worktree",
            details={"dirty_count": len(dirty_files), "untracked_count": len(untracked_files)}))

    merge_state = _repo_merge_state(git_dir)
    report["merge_state"] = merge_state
    if merge_state.get("active"):
        findings.append(_repo_preflight_finding(
            "merge_or_rebase_in_progress",
            "Worktree has an active merge/rebase/cherry-pick/revert state.",
            "merge_or_rebase_in_progress",
            details={"states": merge_state.get("states") or []}))

    conflict_markers = _repo_scan_conflict_markers(repo_path, max_files=max_scan_files) if scan_conflicts else []
    report["conflict_markers"] = conflict_markers[:100]
    report["conflict_marker_count"] = len(conflict_markers)
    if conflict_markers:
        findings.append(_repo_preflight_finding(
            "conflict_markers",
            f"Found conflict markers in {len(conflict_markers)} file(s).",
            "conflict_markers",
            details={"paths": [m.get("path") for m in conflict_markers[:20]]}))

    collisions = _repo_worktree_collisions(repo_path, report["agent_id"], project=project)
    report["resource_collisions"] = collisions
    if collisions:
        findings.append(_repo_preflight_finding(
            "shared_worktree_collision",
            "Worktree path is already leased by another active agent.",
            "shared_worktree_collision",
            details={"collisions": collisions}))

    blocking = [f for f in findings if f.get("blocking")]
    report["verdict"] = "deny" if blocking else ("warn" if findings else "pass")
    report["ok"] = report["verdict"] == "pass"
    return report


__all__ = [
    "_repo_preflight_finding",
    "_repo_git",
    "_repo_remote_slug",
    "_repo_parse_status",
    "_repo_git_dir",
    "_repo_merge_state",
    "_repo_list_candidate_files",
    "_repo_scan_conflict_markers",
    "_repo_worktree_collisions",
    "repo_preflight",
]
