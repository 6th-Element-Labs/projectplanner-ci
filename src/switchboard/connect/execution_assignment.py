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
    prior_attempts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive the complete immutable contract from admitted server state.

    ``prior_attempts`` (COORD-52) is bounded execution memory built by
    ``domain.decisions.prior_attempts.build_prior_attempts`` at DISPATCH time and then
    carried verbatim. It is passed in rather than derived here on purpose: this function is
    re-run at claim time and compared to the stored contract with exact dict equality
    (``require_exact_execution_assignment``), so a block re-derived from the append-only
    corpus would drift between dispatch and claim and fail every claim. Callers on the
    verification path must echo the stored value.

    Omitted entirely when absent — a first-ever dispatch carries no key at all, because a
    zeroed object reads as "nothing worked" rather than "there is no history".
    """

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

    contract: dict[str, Any] = {
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
        # Sibling of claim_expectations (not nested): the Connect claim bind
        # hard-compares claim_expectations to a fixed three-key shape. Typed
        # tool names live here so agents see them without breaking that bind.
        "typed_tools": {
            "executed_test_run": "record_executed_test_run",
            "human_blocker": "record_human_blocker",
        },
        "reason_code": str(lifecycle.get("reason_code") or ""),
        "route": str(lifecycle.get("route") or ""),
        "acceptance_findings": list(lifecycle.get("acceptance_findings") or []),
    }
    if prior_attempts:
        contract["prior_attempts"] = dict(prior_attempts)
    return contract


def require_exact_execution_assignment(
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    """Reject any missing, changed, or extra field in the admitted contract."""

    if dict(observed) != dict(expected):
        raise ExecutionAssignmentError("execution_assignment_contract_mismatch")
