"""Observe-cut effect adapters: capture the plan, never touch the real world.

T2's whole reason to exist is proving orchestration decisions against real
GitHub shapes *before* capacity (a Connect wake / runner boot) is spent. Every
port below is a pure capture: it appends the plan to ``receipts`` and returns
an ``observe_cut`` receipt instead of calling ``task_execution.start_task``,
`gh`, ``provenance.reconcile``, or any other external boundary.

Constraint from the harness design doc: "record planned effect +
execution-assignment contract (role, head, reason_code, findings), then
**stop**." This module is that stop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from switchboard.domain.completion.executor import CompletionEffectAdapters


#: Every effect port ``CompletionEffectAdapters`` exposes. Kept as a tuple
#: (not derived from the dataclass fields) so a typo here fails loudly instead
#: of silently observe-cutting a port nobody asked for.
EFFECT_PORTS: tuple[str, ...] = (
    "ensure_review_generation",
    "start_remediation",
    "mark_ready",
    "update_branch",
    "enqueue",
    "repair_dispatch",
    "fence_runner",
    "reconcile_provenance",
)


@dataclass
class ObserveEffectAdapters:
    """Records what each completion effect *would* do; performs none of it.

    Usage::

        observe = ObserveEffectAdapters()
        completion_driver.run_completion_tick(
            task_id, project=project, actor=actor, agent_id=agent_id,
            hydrator=hydrator, adapters=observe.as_completion_adapters(),
        )
        # observe.receipts now holds one row per effect port invoked.
    """

    run_id: str = ""
    receipts: list[dict[str, Any]] = field(default_factory=list)

    def _capture(self, effect: str) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
        def port(plan: Mapping[str, Any]) -> dict[str, Any]:
            plan_copy = dict(plan)
            record = {
                "schema": "switchboard.completion_observe_receipt.v1",
                "action": "observe_cut",
                "observed": True,
                "effect": effect,
                "run_id": self.run_id,
                "task_id": plan_copy.get("task_id"),
                "route": plan_copy.get("route"),
                "role": plan_copy.get("role"),
                "head_sha": plan_copy.get("head_sha"),
                "reason_code": plan_copy.get("reason_code"),
                "idem_key": plan_copy.get("idem_key"),
                "plan": plan_copy,
            }
            self.receipts.append(record)
            return record
        return port

    def as_completion_adapters(self) -> CompletionEffectAdapters:
        """Bind every port to the capture — never a live external call."""
        return CompletionEffectAdapters(
            **{name: self._capture(name) for name in EFFECT_PORTS}
        )

    def effects_fired(self) -> list[str]:
        return [row["effect"] for row in self.receipts]


__all__ = ["EFFECT_PORTS", "ObserveEffectAdapters"]
