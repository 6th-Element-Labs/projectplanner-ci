"""Preview the ``execution_assignment`` a live ``start_task`` would receive.

Purely a projection over an already-planned effect (``plan_effect`` output, or
one of the ``ObserveEffectAdapters`` receipts). It never calls
``task_execution.start_task`` — this is what an operator or the scoreboard
reads to answer "what would Autopilot have dispatched here?" without paying
for a runner to find out.
"""
from __future__ import annotations

from typing import Any, Mapping


#: Mirrors the keyword arguments ``completion_driver.production_effect_adapters``
#: forwards into ``task_execution.start_task`` for the mutating effects that
#: launch or resume a runner (``ensure_review_generation`` / ``start_remediation``
#: / ``repair_dispatch``). Kept here rather than imported so this preview has no
#: dependency on the production adapter wiring — only on the plan shape.
_LAUNCH_EFFECTS = frozenset({
    "ensure_review_generation", "start_remediation", "repair_dispatch",
})


def preview_execution_assignment(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Project the ``start_task`` call-shape implied by one observed plan.

    Returns an empty ``launches_runner: False`` preview for effects that never
    call ``start_task`` (``enqueue``, ``mark_ready``, ``fence_runner``,
    ``reconcile_provenance``, ``wait``, ``none``, ``escalate_human``, ...).
    """
    plan = dict(plan)
    effect = str(plan.get("effect") or "")
    preview: dict[str, Any] = {
        "effect": effect,
        "launches_runner": effect in _LAUNCH_EFFECTS,
        "task_id": plan.get("task_id"),
        "role": plan.get("role") or "review_merge",
        "source_sha": plan.get("head_sha"),
        "reason_code": plan.get("reason_code"),
        "route": plan.get("route"),
        "decision_attempt": plan.get("decision_attempt"),
        "state_version": plan.get("state_version"),
        "acceptance_findings": list(plan.get("acceptance_findings") or []),
    }
    return preview


def preview_from_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Convenience wrapper over an ``ObserveEffectAdapters`` receipt's plan."""
    return preview_execution_assignment(dict(receipt).get("plan") or {})


__all__ = ["preview_execution_assignment", "preview_from_receipt"]
