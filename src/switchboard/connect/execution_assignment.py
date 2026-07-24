"""Canonical server-owned execution-assignment contracts."""

from __future__ import annotations

from typing import Any, Mapping


SCHEMA = "switchboard.execution_assignment.v1"
EXACT_HEAD_ROLES = frozenset({"review_merge", "remediation"})
VALID_ROLES = frozenset({"implementation", *EXACT_HEAD_ROLES})


class ExecutionAssignmentError(ValueError):
    """A lifecycle cannot produce a safe execution assignment."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def build_execution_assignment(
    *,
    task_id: str,
    assignment: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive the complete immutable contract from admitted server state."""

    role = str(lifecycle.get("role") or "implementation")
    if role not in VALID_ROLES:
        raise ExecutionAssignmentError("execution_assignment_role_invalid")
    head_sha = str(lifecycle.get("head_sha") or "")
    if role in EXACT_HEAD_ROLES and not head_sha:
        raise ExecutionAssignmentError("execution_assignment_exact_head_missing")
    execution_id = str(lifecycle.get("execution_id") or "")
    if not execution_id:
        raise ExecutionAssignmentError("execution_assignment_execution_id_missing")
    generation = int(lifecycle.get("generation") or 0)
    if generation <= 0:
        raise ExecutionAssignmentError("execution_assignment_generation_invalid")
    assignment_id = str(assignment.get("assignment_id") or "")
    if not assignment_id:
        raise ExecutionAssignmentError("execution_assignment_id_missing")

    return {
        "schema": SCHEMA,
        "task_id": str(task_id or "").strip().upper(),
        "execution_id": execution_id,
        "assignment_id": assignment_id,
        "generation": generation,
        "desired_role": role,
        "exact_head_sha": head_sha,
        "exact_pr": {
            "number": int(lifecycle.get("pr_number") or 0),
            "url": str(lifecycle.get("pr_url") or ""),
        },
        "claim_expectations": {
            "required": True,
            "work_session_required": True,
            "role": role,
        },
        "reason_code": str(lifecycle.get("reason_code") or ""),
        "route": str(lifecycle.get("route") or ""),
        "acceptance_findings": list(lifecycle.get("acceptance_findings") or []),
    }


def require_exact_execution_assignment(
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    """Reject any missing, changed, or extra field in the admitted contract."""

    if dict(observed) != dict(expected):
        raise ExecutionAssignmentError("execution_assignment_contract_mismatch")
