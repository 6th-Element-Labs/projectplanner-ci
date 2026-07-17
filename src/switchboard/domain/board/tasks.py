"""Pure task board semantics — no SQLite."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

from switchboard.domain.provenance.git import has_done_provenance, provenance_summary


DEFAULT_IDENTITY_RISK_WINDOW_S = 1800


EDITABLE_TASK_FIELDS = (
    "title", "description", "owner_org", "owner_person_or_role", "assignee",
    "phase", "status", "effort_days", "duration_days", "start_date",
    "finish_date", "risk_level", "is_blocking", "sort_order",
    "entry_criteria", "exit_criteria", "deliverable", "depends_on",
)

EDITABLE = list(EDITABLE_TASK_FIELDS)

READY_TASK_STATUSES = frozenset({"Not Started", "Ready", "Todo", "Backlog"})
TERMINAL_TASK_STATUSES = frozenset({"Done", "Cancelled", "Canceled"})

STALE_DEPENDENCY_RATIONALE_RE = re.compile(
    r"\b(blocked|blocking|blocked\s+on|blocked\s+by|waiting\s+on\s+dependencies)\b",
    re.I,
)
DONE_STATUS_CONTRADICTION_RE = re.compile(
    r"\b(in\s+review|not\s+started|in\s+progress|blocked)\b",
    re.I,
)


def normalize_depends_on(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = value
    else:
        parsed = value
    if isinstance(parsed, str):
        raw_items = parsed.replace("\n", ",").replace(" ", ",").split(",")
    elif isinstance(parsed, list):
        raw_items = parsed
    else:
        raw_items = []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        dep = str(item or "").strip().upper()
        if dep and dep not in seen:
            seen.add(dep)
            out.append(dep)
    return out


def build_dependency_state(
        task: Mapping[str, Any],
        dependency_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(dependency_rows)
    blocking = [row for row in rows if not row.get("done")]
    return {
        "dependencies": rows,
        "dependency_count": len(rows),
        "done": [row["task_id"] for row in rows if row.get("done")],
        "blocking": blocking,
        "blocked_by_count": len(blocking),
        "missing": [row["task_id"] for row in rows if row.get("missing")],
        "satisfied": not blocking,
        "ready": task.get("status") == "Not Started" and not blocking,
    }


def dependency_rows_from_lookup(
        depends_on: Iterable[str],
        by_id: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dep in dict.fromkeys(depends_on or []):
        row = by_id.get(dep)
        status = row["status"] if row else "Missing"
        rows.append({
            "task_id": dep,
            "title": row["title"] if row else None,
            "status": status,
            "done": status == "Done",
            "missing": row is None,
        })
    return rows


def rationale_state(
        rationale: str,
        task: Mapping[str, Any],
        dependency_state: Mapping[str, Any]) -> dict[str, Any]:
    text = rationale or ""
    lower = text.lower()
    flags: list[str] = []
    if (task.get("status") != "Blocked"
            and dependency_state.get("satisfied")
            and STALE_DEPENDENCY_RATIONALE_RE.search(text)
            and "not blocked" not in lower):
        flags.append("says_blocked_but_dependencies_satisfied")
    if task.get("status") == "Done" and DONE_STATUS_CONTRADICTION_RE.search(text):
        flags.append("mentions_pre_done_status_but_task_is_done")
    stale = bool(flags)
    state: dict[str, Any] = {
        "stale": stale,
        "flags": flags,
        "message": (
            "Generated rationale may be stale; trust status, dependency_state, "
            "git_state, and provenance."
        ) if stale else None,
    }
    if stale:
        state["failure_class"] = "missing_data"
        state["expected_signal"] = (
            "Required data is present before workflow execution continues."
        )
    return state


def is_terminal_done_task(task: Mapping[str, Any]) -> bool:
    return (task.get("status") == "Done"
            and has_done_provenance(task.get("git_state") or {}))


def apply_terminal_done_view(task: dict[str, Any]) -> None:
    """Suppress stale derived fields once Done provenance is authoritative."""
    if not is_terminal_done_task(task):
        return
    provenance = task.get("provenance") or provenance_summary(task.get("git_state") or {})
    stale_agent_state = task.get("agent_state") or {}
    stale_claims = task.get("active_claims") or []
    identity = task.get("identity") or {}
    suppressed: dict[str, Any] = {}
    if stale_agent_state:
        reserved = {"validation_policy", "session_policy", "work_session"}
        suppressed["agent_state_agents"] = sorted(
            key for key in stale_agent_state.keys() if key not in reserved)
        if stale_agent_state.get("validation_policy"):
            suppressed["validation_policy"] = True
    if stale_claims:
        suppressed["active_claim_count"] = len(stale_claims)
        suppressed["active_claim_ids"] = [
            claim.get("claim_id") for claim in stale_claims if claim.get("claim_id")
        ]
    if identity.get("active_agents"):
        suppressed["identity_active_agents"] = list(identity.get("active_agents") or [])
    task["terminal_state"] = {
        "terminal": True,
        "authority": "status_git_state_provenance",
        "provenance_type": provenance.get("type"),
        "message": (
            "Task is terminal Done. Consumers should trust status, git_state, and "
            "provenance over historical agent_state, active_claims, identity, or rationale."
        ),
    }
    if suppressed:
        task["terminal_state"]["suppressed_derived"] = suppressed
    task["agent_state"] = {}
    task["active_claims"] = []
    task["identity"] = {
        "active_agents": [],
        "recent_unbound_activity": identity.get("recent_unbound_activity") or [],
        "risk_window_seconds": (
            identity.get("risk_window_seconds") or DEFAULT_IDENTITY_RISK_WINDOW_S
        ),
        "takeover_safe": True,
        "status": "terminal_done",
        "reason": "terminal_done_with_provenance",
        "message": (
            "Identity and takeover risk are closed because the task is already Done "
            "with recorded provenance."
        ),
    }


def block_done_without_provenance() -> dict[str, Any]:
    return {
        "requested_status": "Done",
        "reason": "done_requires_merge_provenance",
        "message": "Status Done requires GitHub/default-branch or offline evidence provenance.",
    }
