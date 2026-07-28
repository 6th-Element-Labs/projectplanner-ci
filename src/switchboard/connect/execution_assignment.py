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
    """Derive the tiny immutable identity/scope contract for one boot.

    Coordination may use ``prior_attempts`` and a rich mission dossier while
    deciding to dispatch.  Neither belongs in the runner assignment: the agent
    rereads live Switchboard and GitHub facts after boot.  Keeping interpreted
    history out of this contract prevents cached diagnosis from masquerading as
    lifecycle authority.
    """
    del prior_attempts

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
            "agent_requires_human": "agent_requires_human",
            "stale_assignment": "report_stale_assignment",
        },
    }
    # A launch pointer is intentionally not a diagnosis.  One trigger and one
    # starting URL save the agent a lookup while every current fact remains
    # discoverable through MCP/GitHub.
    pointer: dict[str, Any] = {
        "trigger": str(lifecycle.get("reason_code") or ""),
        "evidence_url": _launch_pointer_url(lifecycle),
    }
    contract["launch_pointer"] = pointer
    return contract


def _launch_pointer_url(lifecycle: Mapping[str, Any]) -> str:
    findings = lifecycle.get("acceptance_findings")
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            for key in ("failing_check_url", "evidence_url", "review_url", "url"):
                value = str(finding.get(key) or "").strip()
                if value:
                    return value
    dossier = lifecycle.get("mission_dossier")
    if isinstance(dossier, Mapping):
        for key in ("failing_check_url", "evidence_url", "pr_url"):
            value = str(dossier.get(key) or "").strip()
            if value:
                return value
    return str(lifecycle.get("pr_url") or "").strip()


def require_exact_execution_assignment(
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    """Reject any missing, changed, or extra field in the admitted contract."""

    if dict(observed) != dict(expected):
        raise ExecutionAssignmentError("execution_assignment_contract_mismatch")
