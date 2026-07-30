"""Production composition and single-writer cutover for Mission Bot v4."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from switchboard.application.commands import task_execution
from switchboard.application.mission_bot_v4.worker import (
    ScopedMissionWorkerPorts,
    tick_scoped_mission,
)
from switchboard.storage.repositories import autopilot_scopes, runner


@dataclass
class ReadOnlyEffectSpy:
    """Record attempted v4 starts without admitting Capacity or moving a cursor."""

    attempts: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.attempts.append({"task_id": task_id, **kwargs})
        return {
            "error": "shadow_effect_blocked",
            "action": "observed",
            "started": False,
            "attached": False,
        }


def production_ports(
    *, actor: str, agent_id: str, scope_project: str,
    scope_authority: Mapping[str, Any], store_mod: Any,
    effect_spy: ReadOnlyEffectSpy | None = None,
) -> ScopedMissionWorkerPorts:
    """Load the production service graph with exactly one work-driving port."""

    def validate(authority: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        validator = getattr(
            store_mod, "validate_autopilot_scope_authority",
            autopilot_scopes.validate_autopilot_scope_authority,
        )
        return validator(dict(authority), project=scope_project, **{
            key: value for key, value in kwargs.items() if key != "project"
        })

    def get_task(task_id: str, *, project: str) -> Mapping[str, Any]:
        return store_mod.get_task(task_id, project=project) or {}

    def has_live(task_id: str, *, project: str) -> bool:
        checker = getattr(store_mod, "task_has_live_execution", None)
        return bool(
            checker(task_id, project=project)
            if callable(checker)
            else runner.task_has_live_execution(task_id, project=project)
        )

    def start(task_id: str, **kwargs: Any) -> Mapping[str, Any]:
        if effect_spy is not None:
            return effect_spy(task_id, **kwargs)
        authority = kwargs.pop("scope_authority")
        verdict = validate(
            authority, project=kwargs["project"],
            task_project=kwargs["project"], task_id=task_id,
        )
        if verdict.get("allowed") is not True:
            return {
                "error": verdict.get("error") or "scope_authority_denied",
                "reason_codes": verdict.get("reason_codes") or [],
            }
        return task_execution.start_task(
            task_id,
            project=str(kwargs["project"]),
            actor=actor,
            agent_id=agent_id,
            role=str(kwargs["role"]),
            source_sha=str(kwargs.get("source_sha") or ""),
            instruction=str(kwargs.get("instruction") or ""),
            mission_key=str(kwargs.get("mission_key") or ""),
        )

    return ScopedMissionWorkerPorts(
        validate_scope=validate,
        get_task=get_task,
        has_live_execution=has_live,
        start_task=start,
    )


def run_v4_tick(
    task_id: str, *, project: str, scope_project: str,
    scope_authority: Mapping[str, Any], actor: str, agent_id: str,
    store_mod: Any, effect_spy: ReadOnlyEffectSpy | None = None,
) -> dict[str, Any]:
    """Run the production v4 pager, optionally with every effect blocked."""
    return tick_scoped_mission(
        task_id,
        project=project,
        scope_authority=scope_authority,
        actor=actor,
        ports=production_ports(
            actor=actor,
            agent_id=agent_id,
            scope_project=scope_project,
            scope_authority=scope_authority,
            store_mod=store_mod,
            effect_spy=effect_spy,
        ),
    )


__all__ = ["ReadOnlyEffectSpy", "production_ports", "run_v4_tick"]
