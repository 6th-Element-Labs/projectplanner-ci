"""Shared Mission Bot v4 decision vocabulary."""
from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


MissionResult = Literal["continue", "wait", "human", "done"]
EffectKind = Literal["start_task", "merge_queue", "reconcile_task"]


class EffectIntent(TypedDict):
    kind: EffectKind
    key: str
    payload: dict[str, object]


class MissionDecision(TypedDict):
    result: MissionResult
    reason: str
    effect: NotRequired[EffectIntent]
    evidence: NotRequired[dict[str, object]]


__all__ = ["EffectIntent", "EffectKind", "MissionDecision", "MissionResult"]
