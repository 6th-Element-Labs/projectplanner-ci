"""SESSION-12 — PR provenance gate.

The merge gate (SESSION-6) only fires when a cooperating agent calls it with a
known task_id. A process-skipper who never claims a task, never opens a Work
Session, and never calls the gate slips straight through — which is how #143 and
#144 built the same feature twice on the same day.

This module runs at the one chokepoint every PR crosses: the VM CI gate. Given a
PR and the board state, it answers "is this change backed by board process?" and
returns a status verdict. It is intentionally FastAPI-free (like github_sync /
task_id_parser) so it can be unit-tested without the web app and imported by the
CI runner.

Enforcement is policy-profile aware (SESSION-9): only fleet-authored PRs on
enforced profiles (code_strict) block. Human/operator PRs, docs-only changes,
and non-code profiles are exempt. Mode is off | warn | enforce so it can ship in
warn (observe + log, never block) and be flipped to enforce later.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence

import store
import task_id_parser

SCHEMA = "switchboard.pr_provenance_gate.v1"

DEFAULT_FLEET_BRANCH_PREFIXES = ("cursor/", "codex/", "claude/", "agent/", "devin/")
# Board states / signals that prove a change went through the workflow.
_COVERED_STATUSES = {"In Review", "Done"}
_ACTIVE_SESSION_STATUSES = {"proposed", "active", "completed"}
_DOCS_SUFFIXES = (".md", ".mdc", ".rst", ".txt")
_DOCS_DIRS = ("docs/", "plan-docs/", ".cursor/")
_MODES = {"off", "warn", "enforce"}


def _csv_env(name: str, default: Sequence[str]) -> List[str]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return [item for item in default]
    return [item.strip() for item in raw.split(",") if item.strip()]


def gate_mode() -> str:
    """Mode for the primary/home repo (the one the CI runner is centered on)."""
    mode = (os.environ.get("SWITCHBOARD_CI_CLAIM_GATE_MODE") or "warn").strip().lower()
    return mode if mode in _MODES else "warn"


def _default_other_repo_mode() -> str:
    """Mode for canonical repos other than the primary one — warn by default so a
    newly-onboarded repo (Helm, any future project) is observed before it goes red."""
    mode = (os.environ.get("SWITCHBOARD_CI_CLAIM_GATE_MODE_DEFAULT") or "warn").strip().lower()
    return mode if mode in _MODES else "warn"


def _repo_mode_overrides() -> Dict[str, str]:
    """Per-repo mode overrides from SWITCHBOARD_CI_CLAIM_GATE_MODES='owner/repo=enforce,...'."""
    out: Dict[str, str] = {}
    raw = (os.environ.get("SWITCHBOARD_CI_CLAIM_GATE_MODES") or "").strip()
    for item in raw.split(","):
        item = item.strip()
        if "=" not in item:
            continue
        repo, _, mode = item.partition("=")
        repo = repo.strip().lower()
        mode = mode.strip().lower()
        if repo and mode in _MODES:
            out[repo] = mode
    return out


def resolve_mode(repo: str, primary_repo: str = "") -> str:
    """Per-repo claim-gate mode: explicit override wins, else the primary repo uses the
    configured gate_mode() and every other canonical repo uses the (warn) default."""
    repo_l = (repo or "").strip().lower()
    overrides = _repo_mode_overrides()
    if repo_l in overrides:
        return overrides[repo_l]
    if primary_repo and repo_l == primary_repo.strip().lower():
        return gate_mode()
    return _default_other_repo_mode()


def _is_docs_only(changed_paths: Optional[Sequence[str]]) -> Optional[bool]:
    if not changed_paths:
        return None  # unknown — caller did not supply the file list
    for path in changed_paths:
        p = (path or "").strip().lower()
        if not p:
            continue
        if p.endswith(_DOCS_SUFFIXES) or p.startswith(_DOCS_DIRS):
            continue
        return False
    return True


def _is_fleet(branch: str, author: str, *, prefixes: Sequence[str],
              fleet_authors: Sequence[str]) -> bool:
    branch = (branch or "").strip().lower()
    author = (author or "").strip().lower()
    if author and author in {a.lower() for a in fleet_authors}:
        return True
    return any(branch.startswith(p.lower()) for p in prefixes)


def _projects_for_repo(repo: str, project_ids: Sequence[str]) -> List[str]:
    """Projects whose canonical repo is this PR's repo (a shared repo like
    StevenRidder/Helm backs several boards; each owns its own task ids)."""
    out: List[str] = []
    for pid in project_ids:
        try:
            role = store.get_project_repo_role(repo, project=pid)
        except Exception:
            continue
        if role.get("canonical"):
            out.append(pid)
    return out


def _resolve_task(task_id: str, projects: Sequence[str]) -> Optional[Dict[str, Any]]:
    for pid in projects:
        try:
            task = store.get_task(task_id, project=pid)
        except Exception:
            task = None
        if task:
            return {"task_id": task_id, "project": pid, "task": task}
    return None


def _coverage(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Is this referenced task backed by board process right now?"""
    task = entry["task"]
    project = entry["project"]
    task_id = entry["task_id"]
    status = task.get("status") or ""
    if status in _COVERED_STATUSES:
        return {"covered": True, "signal": "status", "detail": status}
    if task.get("active_claims"):
        return {"covered": True, "signal": "active_claim"}
    git_state = task.get("git_state") or {}
    if git_state.get("merged_sha") or git_state.get("pr_number"):
        return {"covered": True, "signal": "git_provenance"}
    try:
        sessions = store.list_work_sessions(project, task_id=task_id, include_expired=False)
    except Exception:
        sessions = []
    if any((s.get("status") or "") in _ACTIVE_SESSION_STATUSES for s in sessions):
        return {"covered": True, "signal": "work_session"}
    return {"covered": False, "signal": None}


def _enforced_profile(entry: Dict[str, Any]) -> bool:
    task = entry["task"]
    project = entry["project"]
    try:
        profile = store._task_work_session_profile(task, project=project)
        rules = store._session_policy_profile_rules(profile, project=project)
    except Exception:
        return True  # fail safe: unknown -> treat as enforced
    entry["policy_profile"] = profile
    return bool(rules.get("work_session_required")
                or rules.get("requires_branch_task_scope"))


def _finding(code: str, message: str, blocking: bool,
             **details: Any) -> Dict[str, Any]:
    return {"code": code, "message": message, "blocking": blocking, **details}


def evaluate_pr_provenance(
    pr: Mapping[str, Any],
    *,
    repo: str,
    mode: str = "",
    changed_paths: Optional[Sequence[str]] = None,
    project_ids: Optional[Sequence[str]] = None,
    fleet_branch_prefixes: Optional[Sequence[str]] = None,
    fleet_authors: Optional[Sequence[str]] = None,
    record_activity: bool = True,
    activity_project: str = "",
) -> Dict[str, Any]:
    """Decide whether a PR is backed by board process.

    Returns {schema, ok, state, mode, context_description, fleet, exempt, reason,
    task_ids, resolved, findings, ...}. `state` is the GitHub commit-status state
    to post ('success' or 'failure'); in warn mode it is always 'success' and a
    would-be block is surfaced only in the description + activity log.
    """
    mode = (mode or gate_mode()).strip().lower()
    if mode not in _MODES:
        mode = "warn"
    prefixes = list(fleet_branch_prefixes) if fleet_branch_prefixes is not None else \
        _csv_env("SWITCHBOARD_CI_FLEET_BRANCH_PREFIXES", DEFAULT_FLEET_BRANCH_PREFIXES)
    authors = list(fleet_authors) if fleet_authors is not None else \
        _csv_env("SWITCHBOARD_CI_FLEET_AUTHORS", ())

    head = pr.get("head") or {}
    branch = str(head.get("ref") or "")
    head_sha = str(head.get("sha") or "")
    author = str((pr.get("user") or {}).get("login") or pr.get("author_login") or "")
    number = pr.get("number")
    task_ids = task_id_parser.task_ids_for_pr(pr)
    fleet = _is_fleet(branch, author, prefixes=prefixes, fleet_authors=authors)
    docs_only = _is_docs_only(changed_paths)

    result: Dict[str, Any] = {
        "schema": SCHEMA, "mode": mode, "repo": repo, "pr_number": number,
        "branch": branch, "head_sha": head_sha, "author": author, "fleet": fleet,
        "task_ids": task_ids, "resolved": [], "findings": [],
    }

    if mode == "off":
        result.update(ok=True, state="success", exempt=True, reason="gate_disabled",
                      context_description="claim gate disabled")
        return result
    if not fleet:
        result.update(ok=True, state="success", exempt=True, reason="non_fleet_pr",
                      context_description="Exempt: non-fleet (human/operator) PR")
        return result
    if docs_only:
        result.update(ok=True, state="success", exempt=True, reason="docs_only_change",
                      context_description="Exempt: docs-only change")
        return result

    pids = list(project_ids) if project_ids is not None else list(store.project_ids())
    repo_projects = _projects_for_repo(repo, pids)
    result["repo_projects"] = repo_projects

    resolved: List[Dict[str, Any]] = []
    for tid in task_ids:
        entry = _resolve_task(tid, repo_projects)
        if entry:
            entry["coverage"] = _coverage(entry)
            entry["enforced"] = _enforced_profile(entry)
            resolved.append({k: entry[k] for k in
                             ("task_id", "project", "coverage", "enforced", "policy_profile")
                             if k in entry})
    result["resolved"] = resolved

    findings: List[Dict[str, Any]] = []
    if not task_ids:
        findings.append(_finding(
            "no_task_reference",
            "Fleet PR references no board task. Claim a task and put its id in the "
            "branch, title, or a 'Closes TASK-N' line before merge.",
            blocking=True, branch=branch))
    elif not resolved:
        findings.append(_finding(
            "task_not_on_board",
            f"PR references {', '.join(task_ids)} but no such task exists on any board "
            f"whose canonical repo is {repo}.",
            blocking=True, task_ids=task_ids))
    else:
        covered = [r for r in resolved if r["coverage"]["covered"]]
        uncovered = [r for r in resolved if not r["coverage"]["covered"]]
        if covered:
            signals = ", ".join(f"{r['task_id']}({r['coverage']['signal']})" for r in covered)
            result["covered_by"] = signals
        else:
            enforced = any(r["enforced"] for r in resolved)
            findings.append(_finding(
                "uncovered_tasks",
                f"PR references {', '.join(r['task_id'] for r in uncovered)} but none has "
                "an active claim, Work Session, or In Review/Done state on the board.",
                blocking=enforced,
                uncovered=[r["task_id"] for r in uncovered],
                enforced=enforced))

    blocking = [f for f in findings if f.get("blocking")]
    result["findings"] = findings
    would_block = bool(blocking)
    result["would_block"] = would_block
    result["exempt"] = False

    if not findings:
        # Clean pass — referenced task(s) covered, or nothing to enforce.
        result.update(ok=True, state="success",
                      reason="covered" if result.get("covered_by") else "clean",
                      context_description=(
                          f"Backed by {result['covered_by']}" if result.get("covered_by")
                          else "PR provenance OK"))
    else:
        primary = (blocking or findings)[0]
        result["reason"] = primary["code"]
        if would_block and mode == "enforce":
            # Block: fail the status so the PR cannot merge.
            result.update(ok=False, state="failure",
                          context_description=primary["message"][:140])
        elif would_block:
            # Warn mode: surface the block but never fail the status.
            result.update(ok=True, state="success",
                          context_description=("WARN (would block): " + primary["message"])[:140])
        else:
            # Non-enforced profile: note it, do not block in any mode.
            result.update(ok=True, state="success",
                          context_description=("Note: " + primary["message"])[:140])

    if record_activity:
        _record(result, repo_projects, activity_project)
    return result


def _record(result: Dict[str, Any], repo_projects: Sequence[str],
            activity_project: str) -> None:
    # Only violations are worth an activity trail (repeat-offender surfacing).
    # A clean/exempt pass every 5-minute tick would flood the log.
    if not result.get("would_block"):
        return
    project = (activity_project
               or (result.get("resolved") or [{}])[0].get("project")
               or (repo_projects[0] if repo_projects else "")
               or store.DEFAULT_PROJECT)
    pr_number = result.get("pr_number")
    head_sha = result.get("head_sha") or ""
    reason = result.get("reason")
    # Dedupe: don't re-log the same (pr, head_sha, reason) the timer already recorded.
    try:
        with store._conn(project) as c:
            rows = c.execute(
                "SELECT payload FROM activity WHERE kind='pr.provenance_gate' "
                "ORDER BY id DESC LIMIT 200").fetchall()
        for row in rows:
            try:
                prev = json.loads(row["payload"])
            except Exception:
                continue
            if (prev.get("pr_number") == pr_number
                    and prev.get("head_sha") == head_sha
                    and prev.get("reason") == reason):
                return
    except Exception:
        pass
    payload = {
        "schema": SCHEMA,
        "pr_number": pr_number,
        "repo": result.get("repo"),
        "branch": result.get("branch"),
        "head_sha": head_sha,
        "author": result.get("author"),
        "mode": result.get("mode"),
        "reason": reason,
        "would_block": result.get("would_block"),
        "task_ids": result.get("task_ids"),
        "resolved": result.get("resolved"),
    }
    resolved = result.get("resolved") or []
    task_id = resolved[0].get("task_id") if resolved else None
    try:
        store.append_activity("pr.provenance_gate", "switchboard-ci/claim-gate",
                              payload, task_id=task_id, project=project)
    except Exception:
        pass
