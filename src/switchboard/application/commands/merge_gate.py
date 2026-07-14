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
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from constants import DEFAULT_PROJECT, GITHUB_PR_URL_RE, MERGE_GATE_SCHEMA


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


def _work_session_row(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._work_session_row(*args, **kwargs)


def _normalize_session_policy_profile(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._normalize_session_policy_profile(*args, **kwargs)


def _executed_test_run_gate(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._executed_test_run_gate(*args, **kwargs)


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
    supplied = evidence.get("github_pr") or evidence.get("pr_state") or evidence.get("pr") or {}
    if isinstance(supplied, dict) and supplied:
        return copy.deepcopy(supplied), {"source": "supplied_evidence"}
    if not repo or not pr_number:
        return {}, {"source": "missing", "reason": "pr_url_or_number_missing"}
    pr = _github_pr(repo, pr_number, _github_token())
    if pr:
        return pr, {"source": "github_api"}
    return {}, {"source": "github_api", "reason": "unavailable"}


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
               principal_id: str = "", project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
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

    required_contexts = _merge_gate_required_contexts(topology, merged_payload)
    pr_contexts = _merge_gate_status_contexts(
        pr.get("status_contexts") if pr else None,
        pr.get("statusCheckRollup") if pr else None,
        pr.get("checks") if pr else None,
        merged_payload.get("status_contexts"),
        merged_payload.get("check_runs"),
        merged_payload.get("checks"),
    )
    external_ci = _external_ci_review_gate(task, evidence=merged_payload, project=project)
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

    review_gate, review_findings = review_merge_gate_findings(
        task_id, str(head_sha or merged_payload.get("head_sha") or
                     (task.get("git_state") or {}).get("head_sha") or "").strip(), project=project)
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

    session = None
    if work_session_id:
        session = get_work_session(work_session_id, project=project)
        if not session:
            findings.append(_merge_gate_finding(
                "work_session_not_found",
                "Merge gate work_session_id was not found.",
                "missing_data",
                details={"work_session_id": work_session_id}))
    elif claim_id:
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
                details={"work_session_id": session.get("work_session_id")}))
        elif preflight.get("verdict") == "deny" or preflight.get("ok") is False:
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
        "pr_url": pr_url or None,
        "pr_number": pr_number or None,
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
        "review_gate": review_gate,
        "github_pr_source": pr_source,
        "done_authority": "github_webhook_or_reconcile",
        "done_controlled_by_merge_provenance": True,
        "checked_at": now,
    }
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
