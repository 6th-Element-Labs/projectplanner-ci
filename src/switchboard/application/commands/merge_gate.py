"""Application command — merge_gate (ARCH-MS-61).

Moved from ``repositories/shell.py``. This is a **gate, not a merge executor**:
it never marks a task Done. GitHub webhooks / reconcile remain the only
code-merge provenance path.

Adapters (REST/MCP) call :func:`execute_mapping_result`. The store façade
re-exports :func:`merge_gate` for compatibility. Policy behavior is preserved
verbatim from the shell residual; no redesign in this move.
"""
from __future__ import annotations

import copy
import json
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from constants import DEFAULT_PROJECT, GITHUB_PR_URL_RE, MERGE_GATE_SCHEMA
from switchboard.domain.provenance.semantic import semantic_completion_gate
from switchboard.domain.validation_policy import (
    UI_CONTEXT,
    classify_task,
    ui_playwright_evidence_gate,
)


__all__ = [
    "_merge_gate_bool",
    "_merge_gate_context_passed",
    "_merge_gate_context_rows",
    "_merge_gate_finding",
    "_merge_gate_pr_evidence",
    "_merge_gate_pr_number",
    "_merge_gate_pr_ref",
    "_merge_gate_required_contexts",
    "_merge_gate_status_contexts",
    "merge_gate",
    "execute_mapping_result",
]


def _store_facade():
    """Resolve board/session/GitHub helpers after store.py is initialized."""
    import store
    return store


def _conn(project: str = DEFAULT_PROJECT):
    from db.connection import _conn as conn_impl
    return conn_impl(project)


def _parse_evidence(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._parse_evidence(*args, **kwargs)


def _coerce_str_list(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._coerce_str_list(*args, **kwargs)


def _github_pr(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._github_pr(*args, **kwargs)


def _github_token(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._github_token(*args, **kwargs)


def _github_repo_from_pr_url(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._github_repo_from_pr_url(*args, **kwargs)


def get_project_github_repo(*args: Any, **kwargs: Any) -> Any:
    return _store_facade().get_project_github_repo(*args, **kwargs)


def get_project_repo_topology(*args: Any, **kwargs: Any) -> Any:
    return _store_facade().get_project_repo_topology(*args, **kwargs)


def get_project_repo_role(*args: Any, **kwargs: Any) -> Any:
    return _store_facade().get_project_repo_role(*args, **kwargs)


def has_project(*args: Any, **kwargs: Any) -> Any:
    return _store_facade().has_project(*args, **kwargs)


def get_task(*args: Any, **kwargs: Any) -> Any:
    return _store_facade().get_task(*args, **kwargs)


def _external_ci_review_gate(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._external_ci_review_gate(*args, **kwargs)


def review_merge_gate_findings(*args: Any, **kwargs: Any) -> Any:
    return _store_facade().review_merge_gate_findings(*args, **kwargs)


def _task_work_session_profile(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._task_work_session_profile(*args, **kwargs)


def _session_policy_profile_rules(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._session_policy_profile_rules(*args, **kwargs)


def get_session_policy_profiles(*args: Any, **kwargs: Any) -> Any:
    return _store_facade().get_session_policy_profiles(*args, **kwargs)


def get_work_session(*args: Any, **kwargs: Any) -> Any:
    return _store_facade().get_work_session(*args, **kwargs)


def list_work_sessions(*args: Any, **kwargs: Any) -> Any:
    return _store_facade().list_work_sessions(*args, **kwargs)


def _work_session_row(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._work_session_row(*args, **kwargs)


def _normalize_session_policy_profile(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._normalize_session_policy_profile(*args, **kwargs)


def _executed_test_run_gate(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._executed_test_run_gate(*args, **kwargs)


def record_review_save(*args: Any, **kwargs: Any) -> Any:
    return _store_facade().record_review_save(*args, **kwargs)


def pr_backed_by_process(*args: Any, **kwargs: Any) -> Any:
    return _store_facade().pr_backed_by_process(*args, **kwargs)


def append_activity(*args: Any, **kwargs: Any) -> Any:
    return _store_facade().append_activity(*args, **kwargs)


def _merge_gate_finding(code: str, message: str, failure_class: str,
                        severity: str = "high", blocking: bool = True,
                        details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "failure_class": failure_class,
        "severity": severity,
        "blocking": bool(blocking),
        **(details or {}),
    }


def _merge_gate_pr_number(pr_url: str, pr_number: Any = None) -> int:
    if pr_number not in (None, ""):
        try:
            return int(pr_number)
        except (TypeError, ValueError):
            return 0
    match = GITHUB_PR_URL_RE.search((pr_url or "").strip())
    if not match:
        return 0
    try:
        return int(match.group(2))
    except (TypeError, ValueError):
        return 0


def _merge_gate_context_rows(value: Any) -> List[Dict[str, Any]]:
    if not value:
        return []
    rows: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        if any(k in value for k in ("context", "name", "state", "status", "conclusion")):
            rows.append(value)
        else:
            for context, state in value.items():
                rows.append({"context": context, "state": state})
        return rows
    if isinstance(value, list):
        for item in value:
            rows.extend(_merge_gate_context_rows(item))
    return rows


def _merge_gate_status_contexts(*sources: Any) -> Dict[str, str]:
    contexts: Dict[str, str] = {}
    for source in sources:
        for row in _merge_gate_context_rows(source):
            name = str(row.get("context") or row.get("name") or row.get("check_name") or "").strip()
            if not name:
                continue
            state = str(
                row.get("state")
                or row.get("status")
                or row.get("conclusion")
                or row.get("result")
                or ""
            ).strip().lower()
            contexts[name] = state
    return contexts


def _merge_gate_context_passed(state: str) -> bool:
    return (state or "").strip().lower() in {"success", "passed", "pass", "ok", "neutral", "skipped"}


# Work Session states that still count as real evidence of the work having happened.
# A claim completing (In Review, awaiting merge) is the normal path into the merge gate,
# so "completed" belongs here — mirrors PR_ACTIVE_SESSION_STATUSES in the repository.
_MERGE_GATE_SESSION_STATUSES = frozenset({"proposed", "active", "completed"})


def _task_scoped_work_session(task_id: str, project: str,
                              head_sha: str = "") -> Optional[Dict[str, Any]]:
    """Resolve the canonical Work Session bound to a task.

    merge_gate previously found a session only from an explicit ``work_session_id`` or
    the task's *active* claim. The branch-protection projection
    (``Switchboard / merge authorization``) supplies neither, so once a claim completed
    or its lease lapsed a code_strict task looked permanently sessionless and reported
    ``work_session_required``/``missing_executed_test_run`` forever — unmergeable no
    matter how healthy the session recorded against it was.

    This is deliberately narrower than "any session for the task": it only considers
    canonical sessions, and when a head is being gated it requires a session pinned to
    that exact head, so a stale session can never authorize a newer commit.
    """
    if not task_id:
        return None
    try:
        sessions = list_work_sessions(project, task_id=task_id, repo_role="canonical")
    except Exception:
        return None
    usable = [
        session for session in (sessions or [])
        if str(session.get("status") or "").strip().lower() in _MERGE_GATE_SESSION_STATUSES
    ]
    if not usable:
        return None
    head = str(head_sha or "").strip().lower()
    if not head:
        return usable[0]
    return next(
        (session for session in usable
         if str(session.get("head_sha") or "").strip().lower() == head),
        None,
    )


def _branch_scoped_work_session(project: str, branch: str, head_sha: str,
                                repo: str = "") -> Optional[Dict[str, Any]]:
    """Resolve the canonical Work Session that produced this exact commit, any task.

    One PR legitimately closes several board tasks — ``evaluate_pr_provenance`` resolves
    every task id referenced by the branch's commits, and merge_gate then runs once per
    resolved task. But the work happened in ONE workspace on ONE branch, so only the
    task that owned the claim has a Work Session. Every co-resolved task looked
    permanently sessionless and wedged the PR on ``work_session_required`` +
    ``missing_executed_test_run`` (BUG-176 on PR #859: its fix shipped in the BUG-177
    branch's session, and #859 had to be landed by operator bypass because of it).

    Demanding a separate session per task is not satisfiable and would not mean
    anything: the sessions would be byte-identical descriptions of the same workspace.
    A Work Session proves *workspace hygiene for a branch at a head* — clean tree, no
    conflict markers, right branch and upstream, known base — and that is a property of
    the commit, not of which task id the commit is filed under.

    So this borrows the session that actually produced the head being gated. It stays
    fail-closed on everything that makes the evidence meaningful:
      * canonical repo role only,
      * exact branch match,
      * exact head match — a session for any other commit cannot authorize this one,
      * usable lifecycle status,
      * same repo when the session records one.
    Per-task review verdicts are still required independently, so each task keeps its own
    human/agent sign-off; only the workspace-hygiene evidence is shared.

    The borrow is reported in the gate result (``work_session_borrowed_from_task``) so it
    is auditable and never a silent fallback.
    """
    branch = str(branch or "").strip()
    head = str(head_sha or "").strip().lower()
    if not branch or not head:
        return None
    try:
        sessions = list_work_sessions(
            project, repo_role="canonical", branch=branch, head_sha=head)
    except Exception:
        return None
    wanted_repo = str(repo or "").strip().lower()
    for session in sessions or []:
        if str(session.get("status") or "").strip().lower() not in _MERGE_GATE_SESSION_STATUSES:
            continue
        session_repo = str(session.get("repo") or "").strip().lower()
        if wanted_repo and session_repo and session_repo != wanted_repo:
            continue
        return session
    return None


def _merge_gate_required_contexts(topology: Dict[str, Any],
                                  evidence: Dict[str, Any]) -> List[str]:
    roles = topology.get("roles") or {}
    required: List[str] = []
    for role_name in ("canonical", "public_ci"):
        required.extend(_coerce_str_list((roles.get(role_name) or {}).get("required_status_contexts")))
    required.extend(_coerce_str_list(evidence.get("required_status_contexts")))
    required.extend(_coerce_str_list(evidence.get("required_contexts")))
    return list(dict.fromkeys([c for c in required if c]))


def _merge_gate_pr_evidence(pr_url: str, pr_number: int,
                            evidence: Dict[str, Any],
                            repo: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Resolve PR evidence, always hydrating exact-head commit statuses when missing.

    Callers such as ``run_merge_authorization_for_pr`` supply a Pulls REST payload that
    never includes ``status_contexts``. Returning that payload untouched made merge
    authorization fail closed on empty contexts and overwrite a previously valid
    ``Switchboard / merge authorization`` status (BUG-173).
    """
    supplied = evidence.get("github_pr") or evidence.get("pr_state") or evidence.get("pr") or {}
    token = _github_token()
    if isinstance(supplied, dict) and supplied:
        pr = copy.deepcopy(supplied)
        source: Dict[str, Any] = {"source": "supplied_evidence"}
    elif not repo or not pr_number:
        return {}, {"source": "missing", "reason": "pr_url_or_number_missing"}
    else:
        pr = _github_pr(repo, pr_number, token)
        if not pr:
            return {}, {"source": "github_api", "reason": "unavailable"}
        source = {"source": "github_api"}

    head_sha = str(
        evidence.get("head_sha")
        or _merge_gate_pr_ref(pr, "head", "sha")
        or ""
    ).strip()
    if head_sha and not _merge_gate_status_contexts(
            pr.get("status_contexts"),
            pr.get("statusCheckRollup"),
            pr.get("checks")):
        statuses = _github_commit_statuses(repo, head_sha, token)
        if statuses:
            pr["status_contexts"] = statuses
            source["hydrated_status_contexts"] = True
    return pr, source


def _github_commit_statuses(repo: str, head_sha: str,
                            token: str = "") -> List[Dict[str, Any]]:
    """Hydrate exact-head commit statuses omitted by GitHub's Pulls REST API."""
    if not repo or not head_sha:
        return []
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/commits/{head_sha}/status")
    request.add_header("Accept", "application/vnd.github+json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode())
    except Exception:
        return []
    statuses = payload.get("statuses") if isinstance(payload, dict) else None
    return statuses if isinstance(statuses, list) else []


def _merge_gate_pr_ref(pr: Dict[str, Any], side: str, field: str) -> str:
    obj = pr.get(side) or {}
    return str(obj.get(field) or "").strip()


def _merge_gate_bool(value: Any, default: Optional[bool] = None) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "ok", "pass", "passed", "clean"}:
        return True
    if text in {"0", "false", "no", "n", "fail", "failed", "dirty", "blocked"}:
        return False
    return default


def merge_gate(payload: Dict[str, Any], actor: str = "system",
               principal_id: str = "", project: str = DEFAULT_PROJECT,
               record: bool = True) -> Dict[str, Any]:
    """Evaluate whether an agent may safely request/perform a PR merge.

    This is a gate, not a merge executor. It never marks a task Done; GitHub webhooks or
    reconcile remain the only code-merge provenance path.
    """
    now = time.time()
    payload = dict(payload or {})
    evidence = _parse_evidence(payload.get("evidence") or {})
    merged_payload = {**payload, **evidence}
    task_id = str(merged_payload.get("task_id") or "").strip().upper()
    agent_id = str(merged_payload.get("agent_id") or "").strip()
    claim_id = str(merged_payload.get("claim_id") or "").strip()
    work_session_id = str(merged_payload.get("work_session_id") or "").strip()
    pr_url = str(merged_payload.get("pr_url") or "").strip()
    pr_number = _merge_gate_pr_number(pr_url, merged_payload.get("pr_number"))
    repo = (
        str(merged_payload.get("repo") or "").strip()
        or _github_repo_from_pr_url(pr_url)
        or get_project_github_repo(project)
    )
    target_branch = str(merged_payload.get("target_branch") or "").strip()
    findings: List[Dict[str, Any]] = []
    if not has_project(project):
        findings.append(_merge_gate_finding(
            "unknown_project", f"Unknown project: {project}", "invalid_input"))
        return {"schema": MERGE_GATE_SCHEMA, "ok": False, "status": "blocked",
                "project": project, "task_id": task_id, "findings": findings}
    topology = get_project_repo_topology(project)
    roles = topology.get("roles") or {}
    canonical = roles.get("canonical") or {}
    default_branch = (canonical.get("default_branch") or "master").strip() or "master"
    if not target_branch:
        target_branch = default_branch
    task = get_task(task_id, project=project) if task_id else None
    if not task:
        findings.append(_merge_gate_finding(
            "task_not_found", "Merge gate requires a known task_id.", "missing_data",
            details={"task_id": task_id}))
        task = {"task_id": task_id, "agent_state": {}}
    role_info = get_project_repo_role(repo, project=project)
    if not role_info.get("canonical"):
        findings.append(_merge_gate_finding(
            "repo_role_cannot_merge",
            "Only the project canonical repo can be merged as code truth.",
            "failed_gate",
            details={"repo": repo, "repo_role": role_info.get("role"),
                     "evidence_only": role_info.get("evidence_only")}))
    if not topology.get("code_repo_gate", {}).get("passed"):
        findings.append(_merge_gate_finding(
            "canonical_repo_missing",
            "Project canonical repo is not configured; merge provenance cannot be trusted.",
            "missing_data",
            details={"code_repo_gate": topology.get("code_repo_gate")}))
    if target_branch != default_branch:
        findings.append(_merge_gate_finding(
            "wrong_target_branch",
            f"Merge target {target_branch!r} does not match canonical default branch {default_branch!r}.",
            "failed_gate",
            details={"target_branch": target_branch, "default_branch": default_branch}))

    pr, pr_source = _merge_gate_pr_evidence(pr_url, pr_number, merged_payload, repo)
    head_sha = ""
    if not pr:
        findings.append(_merge_gate_finding(
            "github_pr_state_unavailable",
            "Merge gate requires GitHub PR state or supplied PR evidence.",
            "broken_connection" if pr_source.get("source") == "github_api" else "missing_data",
            details={"pr_url": pr_url, "pr_number": pr_number, "source": pr_source}))
    else:
        if not pr_url:
            pr_url = str(pr.get("html_url") or "").strip()
        if not pr_number:
            pr_number = int(pr.get("number") or 0)
        base_ref = _merge_gate_pr_ref(pr, "base", "ref")
        head_ref = _merge_gate_pr_ref(pr, "head", "ref")
        head_sha = _merge_gate_pr_ref(pr, "head", "sha")
        if base_ref and base_ref != target_branch:
            findings.append(_merge_gate_finding(
                "wrong_target_branch",
                f"PR base {base_ref!r} does not match requested target {target_branch!r}.",
                "failed_gate",
                details={"pr_base": base_ref, "target_branch": target_branch}))
        if pr.get("draft") is True:
            findings.append(_merge_gate_finding(
                "draft_pr", "Draft PRs cannot pass the merge gate.", "failed_gate"))
        mergeable = _merge_gate_bool(pr.get("mergeable"), default=True)
        merge_state = str(
            pr.get("mergeable_state")
            or pr.get("mergeStateStatus")
            or pr.get("merge_state")
            or ""
        ).strip().lower()
        if mergeable is False or merge_state in {"dirty", "blocked", "behind", "unstable", "unknown"}:
            findings.append(_merge_gate_finding(
                "pr_not_mergeable",
                "GitHub PR state is not cleanly mergeable.",
                "failed_gate",
                details={"mergeable": pr.get("mergeable"), "merge_state": merge_state}))
        expected_head = str(
            merged_payload.get("head_sha")
            or (task.get("git_state") or {}).get("head_sha")
            or ""
        ).strip()
        if expected_head and head_sha and expected_head != head_sha:
            findings.append(_merge_gate_finding(
                "stale_head_sha",
                "PR head SHA does not match task/session evidence.",
                "stale_branch",
                details={"expected_head_sha": expected_head, "pr_head_sha": head_sha}))
        expected_branch = str(merged_payload.get("branch") or (task.get("git_state") or {}).get("branch") or "").strip()
        if expected_branch and head_ref and expected_branch != head_ref:
            findings.append(_merge_gate_finding(
                "stale_branch",
                "PR branch does not match task/session evidence.",
                "stale_branch",
                details={"expected_branch": expected_branch, "pr_branch": head_ref}))
        behind = pr.get("behind_by", pr.get("behind_count", 0))
        try:
            behind_count = int(behind or 0)
        except (TypeError, ValueError):
            behind_count = 0
        if behind_count > 0 or _merge_gate_bool(merged_payload.get("branch_up_to_date"), default=True) is False:
            findings.append(_merge_gate_finding(
                "stale_branch",
                "PR branch is behind target branch and needs rebase/merge.",
                "stale_branch",
                details={"behind": behind_count, "target_branch": target_branch}))
        if _merge_gate_bool(merged_payload.get("safe_rebase_required"), default=False) and not (
                merged_payload.get("safe_rebase_evidence") or merged_payload.get("rebased_at")):
            findings.append(_merge_gate_finding(
                "missing_safe_rebase_evidence",
                "Merge gate requires safe rebase evidence before merge.",
                "missing_data"))

    session_hint = None
    if work_session_id:
        session_hint = get_work_session(work_session_id, project=project)
    elif claim_id:
        with _conn(project) as c:
            row = c.execute(
                "SELECT * FROM work_sessions WHERE claim_id=? ORDER BY updated_at DESC LIMIT 1",
                (claim_id,),
            ).fetchone()
            session_hint = _work_session_row(row) if row else None
    session_borrowed_from_task = ""
    if session_hint is None and not work_session_id:
        # Neither an explicit session id nor a live claim resolved one. That is the
        # normal state for the branch-protection projection (it passes only PR facts)
        # and for any task whose claim has completed, so fall back to the session bound
        # to the task itself — pinned to the exact head being gated. An explicit but
        # unknown work_session_id is left alone so work_session_not_found still fires.
        resolved_head = str(
            head_sha or merged_payload.get("head_sha")
            or (task.get("git_state") or {}).get("head_sha") or ""
        ).strip()
        session_hint = _task_scoped_work_session(
            task_id, project, head_sha=resolved_head)
        if session_hint is None:
            # The task has no session of its own. It may still be a co-resolved task on a
            # PR whose work happened in another task's session on this same branch/head —
            # see _branch_scoped_work_session. Borrow that one rather than declaring the
            # PR unmergeable for work that demonstrably has clean workspace evidence.
            resolved_branch = str(
                merged_payload.get("branch")
                or (task.get("git_state") or {}).get("branch") or ""
            ).strip()
            borrowed = _branch_scoped_work_session(
                project, resolved_branch, resolved_head, repo=repo)
            if borrowed is not None:
                session_hint = borrowed
                session_borrowed_from_task = str(borrowed.get("task_id") or "")
    hint_hygiene = (session_hint or {}).get("hygiene") or {}
    declared_changed_files = (
        merged_payload.get("changed_files")
        or (hint_hygiene.get("repo_preflight") or {}).get("changed_files")
        or hint_hygiene.get("changed_files")
        or []
    )
    task_validation = classify_task(
        task, project=project, existing=task,
        changed_files=declared_changed_files)
    required_contexts = _merge_gate_required_contexts(topology, merged_payload)
    if (task_validation.get("ok")
            and task_validation.get("ui_impact") == "yes"
            and UI_CONTEXT not in required_contexts):
        required_contexts.append(UI_CONTEXT)
    if not task_validation.get("ok"):
        findings.append(_merge_gate_finding(
            task_validation.get("error") or "ui_validation_policy_failed",
            task_validation.get("message") or "Task UI validation classification failed.",
            "missing_data", details={"validation_policy": task_validation}))
    pr_contexts = _merge_gate_status_contexts(
        pr.get("status_contexts") if pr else None,
        pr.get("statusCheckRollup") if pr else None,
        pr.get("checks") if pr else None,
        merged_payload.get("status_contexts"),
        merged_payload.get("check_runs"),
        merged_payload.get("checks"),
    )
    external_ci = _external_ci_review_gate(task, evidence=merged_payload, project=project)
    semantic_evidence = {
        **(((task.get("git_state") or {}).get("evidence") or {})),
        **merged_payload,
    }
    semantic_gate = semantic_completion_gate(task, semantic_evidence)
    if not semantic_gate.get("ok"):
        findings.append(_merge_gate_finding(
            semantic_gate.get("code") or "semantic_completion_failed",
            semantic_gate.get("message") or "Task semantic completion gate failed.",
            semantic_gate.get("failure_class") or "failed_gate",
            details={"semantic_gate": semantic_gate},
        ))
    missing_contexts = [
        context for context in required_contexts
        if not _merge_gate_context_passed(pr_contexts.get(context, ""))
    ]
    if missing_contexts and not external_ci.get("passed"):
        findings.append(_merge_gate_finding(
            "missing_required_status_contexts",
            "Required CI/status contexts are missing or not successful.",
            "failed_gate",
            details={"missing_contexts": missing_contexts,
                     "required_contexts": required_contexts,
                     "status_contexts": pr_contexts}))
    if external_ci.get("required") and not external_ci.get("passed"):
        findings.append(_merge_gate_finding(
            "external_ci_required",
            "External CI mirror evidence is required before merge.",
            "failed_gate",
            details={"external_ci": external_ci}))

    review_head_sha = str(
        head_sha or merged_payload.get("head_sha")
        or (task.get("git_state") or {}).get("head_sha") or ""
    ).strip()
    review_gate, review_findings = review_merge_gate_findings(
        task_id, review_head_sha, project=project)
    findings.extend(review_findings)

    profile = _task_work_session_profile(
        task,
        str(merged_payload.get("session_policy_profile") or merged_payload.get("policy_profile") or ""),
        project=project,
    )
    profile_rules = _session_policy_profile_rules(profile, project=project)
    if not profile_rules:
        findings.append(_merge_gate_finding(
            "unknown_policy_profile",
            f"Unknown session policy profile: {profile or '<empty>'}.",
            "invalid_input",
            details={"known_profiles": sorted((get_session_policy_profiles(project).get("profiles") or {}).keys())}))

    session = session_hint
    if work_session_id:
        if not session:
            findings.append(_merge_gate_finding(
                "work_session_not_found",
                "Merge gate work_session_id was not found.",
                "missing_data",
                details={"work_session_id": work_session_id}))
    elif claim_id and not session:
        with _conn(project) as c:
            row = c.execute(
                "SELECT * FROM work_sessions WHERE claim_id=? ORDER BY updated_at DESC LIMIT 1",
                (claim_id,),
            ).fetchone()
            session = _work_session_row(row) if row else None
    require_session = (
        _merge_gate_bool(merged_payload.get("require_work_session"), default=False)
        or bool(profile_rules.get("merge_requires_work_session"))
    )
    if session:
        session_profile = _normalize_session_policy_profile(
            session.get("policy_profile") or profile or "")
        session_rules = _session_policy_profile_rules(session_profile, project=project) or profile_rules
        if session.get("repo_role") != "canonical":
            findings.append(_merge_gate_finding(
                "wrong_work_session_repo_role",
                "Merge gate requires a canonical Work Session.",
                "failed_gate",
                details={"work_session_id": session.get("work_session_id"),
                         "repo_role": session.get("repo_role")}))
        if session.get("dirty_status") == "dirty" and "dirty_work_session" in set(
                session_rules.get("deny_hygiene") or []):
            findings.append(_merge_gate_finding(
                "dirty_work_session",
                "Work Session is dirty; run repo preflight and commit or clean changes before merge.",
                "failed_gate",
                details={"work_session_id": session.get("work_session_id")}))
        if int(session.get("conflict_marker_count") or 0) > 0 and "conflict_markers" in set(
                session_rules.get("deny_hygiene") or []):
            findings.append(_merge_gate_finding(
                "conflict_markers",
                "Work Session reports conflict markers.",
                "failed_gate",
                details={"work_session_id": session.get("work_session_id")}))
        preflight = ((session.get("hygiene") or {}).get("repo_preflight") or {})
        if not preflight:
            findings.append(_merge_gate_finding(
                "missing_work_session_preflight",
                "Merge gate requires a recorded clean Work Session preflight.",
                "missing_data",
                # BUG-177: this used to state the requirement without naming the fix, so
                # agents whose workspace is off the coordinator's filesystem assumed it was
                # unsatisfiable and skipped preflight entirely — wedging their own PR.
                # preflight_work_session ALWAYS records something usable: for a host-local
                # path it falls through to the BUG-97/BUG-159 ladder and stores a
                # non-blocking `coordinator_unverifiable`/`agent_host_pending` report
                # (verdict "warn"), which this gate accepts. Only a never-run preflight
                # blocks here.
                details={"work_session_id": session.get("work_session_id"),
                         "repair": (
                             "Run preflight_work_session(work_session_id=...) before "
                             "requesting merge authorization. A workspace the coordinator "
                             "cannot stat records a non-blocking unverifiable/pending "
                             "preflight, which satisfies this gate."
                         )}))
        else:
            preflight_verdict = (preflight.get("verdict") or "").strip().lower()
            blocking_preflight_findings = [
                finding for finding in (preflight.get("findings") or [])
                if finding.get("blocking") is not False
            ]
            if (
                    preflight_verdict == "deny"
                    or bool(blocking_preflight_findings)
                    or (not preflight_verdict and preflight.get("ok") is False)):
                findings.append(_merge_gate_finding(
                    "work_session_preflight_failed",
                    "Work Session preflight is not clean.",
                    "failed_gate",
                    details={"work_session_id": session.get("work_session_id"),
                             "preflight": preflight}))
    elif require_session:
        findings.append(_merge_gate_finding(
            "work_session_required",
            f"Policy profile {profile} requires a Work Session for merge intent.",
            "missing_data",
            details={"policy_profile": profile}))
    if profile_rules.get("requires_executed_tests"):
        executed_test_gate = _executed_test_run_gate(merged_payload, session)
        if not executed_test_gate.get("ok"):
            findings.append(_merge_gate_finding(
                executed_test_gate.get("reason") or "missing_executed_test_run",
                "Merge gate requires a passing executed test run with output/log hash.",
                "missing_data",
                details={"executed_test_gate": executed_test_gate,
                         "policy_profile": profile}))
    hygiene = (session or {}).get("hygiene") or {}
    preflight = hygiene.get("repo_preflight") or {}
    changed_files = (
        declared_changed_files
        or preflight.get("changed_files")
        or hygiene.get("changed_files")
        or []
    )
    ui_gate = ui_playwright_evidence_gate(
        task, merged_payload, session, project=project,
        head_sha=review_head_sha, changed_files=changed_files)
    if not ui_gate.get("ok"):
        findings.append(_merge_gate_finding(
            ui_gate.get("reason") or ui_gate.get("error") or "missing_ui_playwright_evidence",
            ui_gate.get("message") or "UI Playwright evidence gate failed.",
            "missing_data", details={"ui_playwright_gate": ui_gate}))

    # Shared "is this task backed by board process" check (ADR-0006) — the same
    # definition the SESSION-12 claim gate enforces at the CI chokepoint. merge_gate
    # layers its stricter work-session hygiene above; this guards the base case (a task
    # with no claim, Work Session, In-Review/Done state, or provenance must not merge).
    backing = pr_backed_by_process(task, project=project)
    if task.get("status") and not backing.get("backed"):
        findings.append(_merge_gate_finding(
            "task_not_backed",
            "Task has no board backing: no active claim, Work Session, or In-Review/Done state.",
            "missing_data",
            details={"backing": backing}))

    blocking = [f for f in findings if f.get("blocking")]
    ok = not blocking
    result = {
        "schema": MERGE_GATE_SCHEMA,
        "project": project,
        "task_id": task_id,
        "backed": bool(backing.get("backed")),
        "backing_signal": backing.get("signal"),
        "claim_id": claim_id or None,
        "agent_id": agent_id or None,
        "work_session_id": (session or {}).get("work_session_id") or work_session_id or None,
        # Non-null when this task had no Work Session of its own and the gate used the
        # session that produced the same branch/head under another co-resolved task.
        # Surfaced deliberately: a borrowed session must be visible in the audit trail,
        # not an invisible fallback.
        "work_session_borrowed_from_task": session_borrowed_from_task or None,
        "pr_url": pr_url or None,
        "pr_number": pr_number or None,
        "head_sha": review_head_sha or None,
        "repo": repo,
        "repo_role": role_info,
        "target_branch": target_branch,
        "policy_profile": profile,
        "policy": profile_rules,
        "work_session_required": require_session,
        "ok": ok,
        "status": "passed" if ok else "blocked",
        "findings": findings,
        "required_status_contexts": required_contexts,
        "status_contexts": pr_contexts,
        "external_ci": external_ci,
        "semantic_gate": semantic_gate,
        "review_gate": review_gate,
        "validation_policy": task_validation,
        "ui_playwright_gate": ui_gate,
        "github_pr_source": pr_source,
        "done_authority": "github_webhook_or_reconcile",
        "done_controlled_by_merge_provenance": True,
        "checked_at": now,
    }
    if record and not ok and task_id and review_head_sha:
        result["review_remediation_save"] = record_review_save(
            task_id, review_head_sha, result, actor=actor, project=project)
    if record:
        append_activity(
            "merge.gate",
            actor,
            {k: v for k, v in result.items() if k not in {"external_ci"}},
            task_id=task_id or None,
            project=project,
        )
    return result


MergeGateFn = Callable[..., dict[str, Any]]


def execute_mapping_result(
        data: dict[str, Any],
        *,
        actor: str,
        principal_id: str = "",
        merge_gate: Optional[MergeGateFn] = None) -> dict[str, Any]:
    """Adapter-facing entry: evaluate safe-merge readiness from mapping input."""
    payload = dict(data or {})
    project = payload.pop("project", None) or DEFAULT_PROJECT
    gate = merge_gate or globals()["merge_gate"]
    return gate(
        payload,
        actor=actor,
        principal_id=principal_id,
        project=project,
    )
