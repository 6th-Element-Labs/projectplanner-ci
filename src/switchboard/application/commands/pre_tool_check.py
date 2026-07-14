"""Application command — pre_tool_check (ARCH-MS-60).

Moved from ``repositories/shell.py``. This is **not** a SQL repository: it
orchestrates identity binding, session policy, activity audit, work-session
validation, and file-lease conflict checks before adapter side effects.

Adapters (REST/MCP) and the store façade call :func:`pre_tool_check`. Policy
behavior is preserved verbatim from the shell residual; no redesign in this
move.
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from constants import DEFAULT_PROJECT, PRE_TOOL_CHECK_SCHEMA
from switchboard.domain.access.identity import write_binding_activity_payload


def _store_facade():
    """Resolve board/session/lease helpers after store.py is initialized."""
    import store
    return store


def _conn(project: str = DEFAULT_PROJECT):
    from db.connection import _conn as conn_impl
    return conn_impl(project)


def has_project(project: Optional[str]) -> bool:
    return _store_facade().has_project(project)


def resolve_write_actor(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return _store_facade().resolve_write_actor(*args, **kwargs)


def get_task(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return _store_facade().get_task(*args, **kwargs)


def append_activity(*args: Any, **kwargs: Any) -> Any:
    return _store_facade().append_activity(*args, **kwargs)


def check_resources(*args: Any, **kwargs: Any) -> Any:
    return _store_facade().check_resources(*args, **kwargs)


def _task_work_session_profile(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._task_work_session_profile(*args, **kwargs)


def _session_policy_profile_rules(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._session_policy_profile_rules(*args, **kwargs)


def _unknown_session_policy_profile(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._unknown_session_policy_profile(*args, **kwargs)


def _active_work_session_row_in(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._active_work_session_row_in(*args, **kwargs)


def _work_session_row(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._work_session_row(*args, **kwargs)


def _validate_work_session_claim_state(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._validate_work_session_claim_state(*args, **kwargs)


def _work_session_failure(*args: Any, **kwargs: Any) -> Any:
    return _store_facade()._work_session_failure(*args, **kwargs)


def _pre_tool_input(value: Any) -> Dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {"value": value}


def _pre_tool_classify(tool_name: str, tool_input: Dict[str, Any],
                       action: str = "") -> Dict[str, Any]:
    raw_action = (action or "").strip().lower()
    name = (tool_name or "").strip()
    lowered = name.lower()
    ti = tool_input or {}
    if raw_action:
        effect = raw_action
    elif name in {"Edit", "Write", "NotebookEdit"}:
        effect = "file_write"
    elif "complete_claim" in lowered or lowered.endswith("/complete_claim"):
        effect = "complete_claim"
    elif "pr create" in str(ti.get("command") or "").lower() or "gh pr create" in str(ti.get("command") or "").lower():
        effect = "pr_create"
    elif name == "Bash":
        cmd = str(ti.get("command") or "").lower()
        if re.search(r"\bgit\s+(merge|rebase|cherry-pick|commit|push|reset|checkout|switch)\b", cmd):
            effect = "git_command"
        elif re.search(r"\b(gh\s+pr\s+merge|gh\s+pr\s+create)\b", cmd):
            effect = "pr_or_merge"
        elif re.search(r"\b(systemctl|uvicorn|npm\s+run|python3?\s+.*app\.py|kill|pkill)\b", cmd):
            effect = "runtime_control"
        else:
            effect = "shell"
    elif lowered.endswith(("update_task", "claim_task", "claim_next")):
        effect = "board_write"
    else:
        effect = "unknown"
    side_effect = effect not in {"read", "noop", "unknown"}
    requires_work_session = effect in {
        "file_write", "git_command", "pr_create", "pr_or_merge", "complete_claim",
        "merge", "server_start", "server_kill", "runtime_control", "external_effect",
        "board_write",
    }
    return {
        "tool_name": name,
        "action": effect,
        "side_effect": side_effect,
        "requires_work_session": requires_work_session,
    }


def _pre_tool_target_path(tool_input: Dict[str, Any]) -> str:
    ti = tool_input or {}
    return str(ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or "").strip()


def _pre_tool_relpath(path: str, session: Dict[str, Any]) -> str:
    path = (path or "").strip()
    if not path:
        return ""
    if not os.path.isabs(path):
        return path.replace(os.sep, "/")
    root = (session.get("worktree_path") or session.get("clone_path") or "").strip()
    if root:
        try:
            return os.path.relpath(path, root).replace(os.sep, "/")
        except ValueError:
            pass
    return os.path.basename(path)


def _pre_tool_decision(decision: str, reason: str, failure_class: str = "",
                       severity: str = "", remediation: Optional[List[str]] = None,
                       **extra: Any) -> Dict[str, Any]:
    return {
        "schema": PRE_TOOL_CHECK_SCHEMA,
        "decision": decision,
        "reason": reason,
        "failure_class": failure_class,
        "severity": severity,
        "remediation": remediation or [],
        **extra,
    }


def _pre_tool_requested_profile(payload: Dict[str, Any], classification: Dict[str, Any],
                                session: Optional[Dict[str, Any]] = None) -> str:
    requested = str(payload.get("session_policy_profile") or payload.get("policy_profile") or "").strip()
    if requested:
        return requested
    if session and session.get("policy_profile"):
        return str(session.get("policy_profile") or "")
    if classification.get("action") in {
        "git_command", "pr_create", "pr_or_merge", "complete_claim", "merge",
        "server_start", "server_kill", "runtime_control",
    }:
        return "code_strict"
    return ""


def _record_pre_tool_activity(task_id: str, actor: str, kind: str,
                              payload: Dict[str, Any],
                              project: str = DEFAULT_PROJECT) -> None:
    if not task_id:
        return
    append_activity(kind, actor, payload, task_id=task_id, project=project)


def pre_tool_check(payload: Dict[str, Any], actor: str = "system",
                   principal_id: str = "",
                   project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Validate a pending side-effectful tool call against Work Session state.

    This is the server-side contract adapters call before file writes, git/PR/merge
    actions, claim completion, and runner/server controls. It intentionally fails closed for
    risky effects when no active Work Session is bound, while read/noop checks remain allowed.
    """
    if not has_project(project):
        return _pre_tool_decision(
            "deny", f"unknown project: {project}", "invalid_input", "high",
            ["Call prepare_agent_session and pass the selected project explicitly."],
            project=project, ok=False)

    payload = dict(payload or {})
    tool_input = _pre_tool_input(payload.get("tool_input") or payload.get("input") or {})
    agent_id = str(payload.get("agent_id") or "").strip()
    task_id = str(payload.get("task_id") or payload.get("task") or "").strip().upper()
    work_session_id = str(payload.get("work_session_id") or "").strip()
    claim_id = str(payload.get("claim_id") or "").strip()
    control_mode = str(payload.get("control_mode") or payload.get("control_fidelity") or "").strip()
    classification = _pre_tool_classify(
        str(payload.get("tool_name") or payload.get("tool") or ""),
        tool_input,
        str(payload.get("action") or ""),
    )
    base = {
        "project": project,
        "task_id": task_id or None,
        "agent_id": agent_id or None,
        "work_session_id": work_session_id or None,
        "claim_id": claim_id or None,
        "classification": classification,
        "control_mode": control_mode or None,
    }
    if not classification["side_effect"] and not classification["requires_work_session"]:
        return _pre_tool_decision("allow", "", **base, ok=True)

    binding = resolve_write_actor(
        actor,
        project=project,
        task_id=task_id,
        agent_id=agent_id,
        principal_id=principal_id,
    )
    if not binding.get("ok"):
        event = {
            **base,
            "reason": binding.get("error") or "unbound_write",
            "failure_class": "unbound_identity",
            "principal_actor": binding.get("principal_actor") or actor,
            "principal_id": principal_id,
            "remediation": binding.get("remediation") or [],
        }
        _record_pre_tool_activity(task_id, "switchboard/identity",
                                  "principal.unbound_write", event, project=project)
        return _pre_tool_decision(
            "deny",
            binding.get("message") or "Tool side effect requires a bound active agent identity.",
            "unbound_identity",
            "high",
            binding.get("remediation") or [],
            **base,
            binding=binding,
            activity_kind="principal.unbound_write",
            ok=False,
        )

    actor_name = binding.get("actor") or actor
    if not task_id:
        event = {**base, "reason": "task_id_required", "failure_class": "missing_data"}
        _record_pre_tool_activity("", actor_name, "work_session.unsafe_session", event, project=project)
        return _pre_tool_decision(
            "deny",
            "Side-effectful tools must name task_id so the Work Session can be validated.",
            "missing_data",
            "high",
            ["Pass task_id and work_session_id from the active claim/session."],
            **base,
            activity_kind="work_session.unsafe_session",
            ok=False,
        )
    task = get_task(task_id, project=project)
    if not task:
        return _pre_tool_decision(
            "deny", "task_id does not exist in this project.", "invalid_input", "high",
            ["Refresh the board and use a task from the selected project."],
            **base, ok=False)
    profile = _task_work_session_profile(
        task,
        _pre_tool_requested_profile(payload, classification),
        project=project,
    )
    rules = _session_policy_profile_rules(profile, project=project)
    base["policy_profile"] = profile
    base["policy_action"] = rules.get("pre_tool_missing_session") if rules else None
    if not rules:
        verdict = _unknown_session_policy_profile(profile, project)
        return _pre_tool_decision(
            "deny",
            verdict.get("message") or "Unknown session policy profile.",
            verdict.get("failure_class") or "invalid_input",
            verdict.get("severity") or "high",
            ["Use one of the project's session_policy_profiles.known_profiles."],
            **base,
            known_profiles=verdict.get("known_profiles") or [],
            ok=False,
        )

    now = time.time()
    with _conn(project) as c:
        row = _active_work_session_row_in(
            c, work_session_id=work_session_id, task_id=task_id, agent_id=agent_id,
            now=now)
        if not row:
            action = str(rules.get("pre_tool_missing_session") or "deny").strip().lower()
            strict_missing = bool(rules.get("work_session_required")) or action == "deny"
            event = {
                **base,
                "reason": "work_session_required" if strict_missing else "work_session_missing_allowed_by_policy",
                "failure_class": "missing_data",
                "binding": write_binding_activity_payload(binding),
                "policy": rules,
            }
            _record_pre_tool_activity(task_id, actor_name,
                                      "work_session.unsafe_session" if strict_missing else
                                      "work_session.policy_warning",
                                      event, project=project)
            if not strict_missing:
                return _pre_tool_decision(
                    "warn" if action == "warn" else "allow",
                    f"Policy profile {profile} allows this side effect without a bound Work Session.",
                    "missing_data" if action == "warn" else "",
                    "medium" if action == "warn" else "",
                    [
                        "Bind a Work Session for stronger provenance when this touches code.",
                        "Use code_strict for repo/code changes.",
                    ] if action == "warn" else [],
                    **base,
                    binding=write_binding_activity_payload(binding),
                    activity_kind="work_session.policy_warning",
                    ok=True,
                )
            return _pre_tool_decision(
                "deny",
                f"Policy profile {profile} requires a valid active Work Session before this tool side effect.",
                "missing_data",
                "high",
                [
                    "Create or bind a Work Session for this task and repo role.",
                    "Run repo_preflight/preflight_work_session and retry from the task branch.",
                    "Advisory runtimes must surface this deny and mark reduced control fidelity.",
                ],
                **base,
                activity_kind="work_session.unsafe_session",
                ok=False,
            )
        session = _work_session_row(row)
        profile = _task_work_session_profile(
            task,
            _pre_tool_requested_profile(payload, classification, session),
            project=project,
        )
        rules = _session_policy_profile_rules(profile, project=project)
        if not rules:
            verdict = _unknown_session_policy_profile(profile, project)
            rules = {}
        else:
            verdict = _validate_work_session_claim_state(
                session, task, agent_id, project,
                required=bool(rules.get("work_session_required")),
                profile=profile,
                source="pre_tool_check", normalized_payload=None, now=now)
        base["policy_profile"] = profile
        base["policy_action"] = rules.get("pre_tool_missing_session") if rules else None
        base["work_session_id"] = session.get("work_session_id")
        if claim_id and session.get("claim_id") and claim_id != session.get("claim_id"):
            verdict = _work_session_failure(
                "wrong_claim",
                "Work Session claim_id does not match the pending tool claim.",
                "invalid_input",
                details={"problems": [{"reason": "wrong_claim",
                                        "failure_class": "invalid_input",
                                        "message": "claim_id mismatch"}],
                         "work_session_id": session.get("work_session_id"),
                         "policy_profile": profile},
            )
        if not verdict.get("ok"):
            event = {
                **base,
                "reason": verdict.get("reason") or "unsafe_session",
                "failure_class": verdict.get("failure_class") or "failed_gate",
                "problems": verdict.get("problems") or [],
                "binding": write_binding_activity_payload(binding),
            }
            _record_pre_tool_activity(task_id, actor_name, "work_session.unsafe_session",
                                      event, project=project)
            return _pre_tool_decision(
                "deny",
                verdict.get("message") or "Work Session is unsafe for this tool side effect.",
                verdict.get("failure_class") or "failed_gate",
                verdict.get("severity") or "high",
                [
                    "Repair the Work Session hygiene failure.",
                    "Run preflight_work_session before retrying.",
                    "Do not proceed through a hidden fallback.",
                ],
                **base,
                problems=verdict.get("problems") or [],
                activity_kind="work_session.unsafe_session",
                ok=False,
            )

    target_path = _pre_tool_target_path(tool_input)
    if classification["action"] == "file_write" and target_path:
        relpath = _pre_tool_relpath(target_path, session)
        held = check_resources("file", [relpath], project=project)
        conflicts = [h for h in held if h.get("name") == relpath and
                     h.get("held_by") and h.get("held_by") != agent_id]
        if conflicts:
            event = {
                **base,
                "target_path": relpath,
                "reason": "file_lease_conflict",
                "failure_class": "failed_gate",
                "conflicts": conflicts,
            }
            _record_pre_tool_activity(task_id, actor_name, "work_session.unsafe_session",
                                      event, project=project)
            return _pre_tool_decision(
                "deny",
                f"'{relpath}' is leased by another active agent.",
                "failed_gate",
                "high",
                ["Coordinate through Switchboard or wait for the lease to release."],
                **base,
                target_path=relpath,
                conflicts=conflicts,
                activity_kind="work_session.unsafe_session",
                ok=False,
            )

    return _pre_tool_decision(
        "allow",
        "Work Session validated for this tool side effect.",
        **base,
        binding=write_binding_activity_payload(binding),
        ok=True,
    )


def execute_mapping_result(
        data: Dict[str, Any],
        *,
        actor: str,
        principal_id: str = "",
        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Adapter-facing entry: evaluate a pending tool against Work Session policy."""
    payload = dict(data or {})
    project_id = payload.pop("project", None) or project or DEFAULT_PROJECT
    return pre_tool_check(
        payload,
        actor=actor,
        principal_id=principal_id,
        project=project_id,
    )
