"""Semantic completion rules layered above repository provenance.

Git provenance proves that work landed.  It does not prove that a task's intended
outcome succeeded.  This module keeps those concepts separate and gives lifecycle
chokepoints one fail-closed verdict for explicit negative completion evidence.
"""
from __future__ import annotations

import re
from typing import Any, Mapping


SCHEMA = "switchboard.semantic_completion_gate.v1"
NEGATIVE_OUTCOMES = {"blocked", "fail", "failed", "failure", "no-go", "nogo", "rejected"}
_TERMINAL_OUTCOMES_RE = re.compile(
    r"(?im)^\s*semantic_terminal_outcomes\s*:\s*([^\n#]+)"
)
_COMPLETION_POLICY_RE = re.compile(
    r"(?im)^\s*semantic_completion_policy\s*:\s*([^\n#]+)"
)


def _normalized_outcome(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _explicit_false(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    return isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off"}


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return bool(value)


def terminal_outcomes(task: Mapping[str, Any]) -> set[str]:
    """Return negative outcomes explicitly authorized as terminal by task contract.

    The marker is deliberately task-owned rather than completion-evidence-owned so an
    agent cannot waive a failed gate in the same payload that reports it.
    """
    contract = "\n".join(
        str(task.get(field) or "")
        for field in ("description", "entry_criteria", "exit_criteria")
    )
    allowed: set[str] = set()
    for match in _TERMINAL_OUTCOMES_RE.finditer(contract):
        allowed.update(
            _normalized_outcome(item)
            for item in re.split(r"[,\s]+", match.group(1))
            if item.strip()
        )
    for match in _COMPLETION_POLICY_RE.finditer(contract):
        if _normalized_outcome(match.group(1)) == "decision":
            allowed.add("nogo")
            allowed.add("no-go")
    return allowed


def semantic_completion_gate(task: Mapping[str, Any],
                             evidence: Mapping[str, Any] | None) -> dict[str, Any]:
    evidence = dict(evidence or {})
    nested = evidence.get("semantic_outcome")
    semantic = dict(nested) if isinstance(nested, Mapping) else {}
    outcome = _normalized_outcome(
        semantic.get("outcome") or semantic.get("status") or evidence.get("verdict")
    )
    failed_gates = semantic.get("failed_gates", evidence.get("failed_gates"))
    blocking_gate = semantic.get("blocking_gate", evidence.get("blocking_gate"))
    process_cut_present = (
        "process_cut_authorized" in semantic or "process_cut_authorized" in evidence
    )
    process_cut_value = semantic.get(
        "process_cut_authorized", evidence.get("process_cut_authorized")
    )

    reasons: list[str] = []
    if "passed" in semantic and _explicit_false(semantic.get("passed")):
        reasons.append("semantic_outcome_not_passed")
    if outcome in NEGATIVE_OUTCOMES:
        reasons.append("negative_outcome")
    if _nonempty(failed_gates):
        reasons.append("failed_gates")
    if _nonempty(blocking_gate):
        reasons.append("blocking_gate")
    if process_cut_present and _explicit_false(process_cut_value):
        reasons.append("process_cut_not_authorized")

    if not reasons:
        return {
            "schema": SCHEMA,
            "ok": True,
            "status": "passed",
            "task_id": task.get("task_id"),
            "outcome": outcome or None,
            "reasons": [],
            "allowed_terminal_outcomes": sorted(terminal_outcomes(task)),
        }

    allowed = terminal_outcomes(task)
    explicitly_terminal = bool(outcome and outcome in allowed)
    if explicitly_terminal:
        return {
            "schema": SCHEMA,
            "ok": True,
            "status": "terminal_negative_outcome_authorized",
            "task_id": task.get("task_id"),
            "outcome": outcome,
            "reasons": reasons,
            "allowed_terminal_outcomes": sorted(allowed),
        }

    return {
        "schema": SCHEMA,
        "ok": False,
        "status": "blocked",
        "code": "semantic_completion_failed",
        "failure_class": "failed_gate",
        "message": (
            "Completion evidence reports a failed or blocked task outcome. "
            "Repair the same task, or explicitly authorize the negative outcome in "
            "the task contract with semantic_terminal_outcomes."
        ),
        "task_id": task.get("task_id"),
        "outcome": outcome or None,
        "reasons": reasons,
        "failed_gates": failed_gates or [],
        "blocking_gate": blocking_gate or None,
        "process_cut_authorized": process_cut_value if process_cut_present else None,
        "allowed_terminal_outcomes": sorted(allowed),
    }
