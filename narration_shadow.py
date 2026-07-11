"""Shadow-mode comparison for the event-driven narrator (NARRATE-10, ADR-0008).

Runs the wakeable worker's decision logic *in shadow*: it compares what the event-driven
outbox would narrate against what the legacy ``pending_narrations`` queue would narrate, and
lets the worker drain with a non-publishing generator. Nothing here writes a visible narration
(``task_narrations``) or calls an LLM — "do not publish from both paths" during the soak.

Two surfaces:

- ``compare_narration_paths(project)`` — a **read-only**, side-effect-free diff of the two
  impact sets plus coalescing / stale-suppression counts. Safe to run on a timer for the
  NARRATE-13 operator view. The load-bearing signal is ``only_legacy``: a task the legacy queue
  would narrate but the event path would not (a durability gap that must be empty before
  NARRATE-14 flips the primary path). ``only_event`` is expected to be non-empty because the
  outbox emits on any material change while legacy only narrates a configured trigger subset.

- ``run_shadow_drain(project)`` — actually exercises the claim/lease/coalesce/retry machine via
  ``narration_worker.drain`` with a recorder that captures the impact and publishes nothing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import narration_worker
import store


def legacy_impact_set(project: str) -> Dict[str, Any]:
    """Read-only: the task ids the legacy ``run_pending`` drain would consider narrating now —
    i.e. queued markers that pass the trigger-status filter. Does not clear markers or narrate.

    Uses the exact trigger config the legacy job uses (``narrate._trigger_statuses``) so the
    comparison can't drift from production behavior. The legacy per-task fingerprint idle-guard
    is a cost optimization, not an impact-set definition, so it is intentionally not applied
    here — this is the set of tasks the queue would hand to the narrator."""
    import narrate  # local import: pulls httpx; keep it off the module import path
    triggers = narrate._trigger_statuses()
    impacts: List[str] = []
    filtered_out = 0
    for row in store.list_pending_narrations(project=project):
        reason = (row.get("reason") or "").lower()
        status = (row.get("status") or "").strip().lower()
        if triggers is not None and reason != "create" and status not in triggers:
            filtered_out += 1
            continue
        impacts.append(row["task_id"])
    return {"impacts": sorted(set(impacts)), "filtered_out": filtered_out}


def event_impact_set(project: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """Read-only: the task ids the event-driven worker would narrate now, plus coalescing /
    stale-suppression counts. Delegates to the worker's own preview so the two never diverge."""
    preview = narration_worker.preview_impact_set(project, now=now)
    task_ids = sorted({i["entity_id"] for i in preview["impacts"] if i["entity_type"] == "task"})
    return {
        "impacts": task_ids,
        "coalesced": preview["coalesced"],
        "stale_suppressed": preview["stale_suppressed"],
    }


def compare_narration_paths(project: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """Side-effect-free diff of the legacy vs event-driven task impact sets.

    ``only_legacy`` must be empty for the event path to be a safe replacement (every task the
    legacy queue would narrate has a corresponding fresh outbox request). ``only_event`` is the
    expected surplus from the outbox's broader (every-material-change) triggering vs the legacy
    trigger subset. ``in_sync`` is True when there is no legacy-only drift."""
    legacy = legacy_impact_set(project)
    event = event_impact_set(project, now=now)
    legacy_ids = set(legacy["impacts"])
    event_ids = set(event["impacts"])
    only_legacy = sorted(legacy_ids - event_ids)
    only_event = sorted(event_ids - legacy_ids)
    return {
        "project": project,
        "legacy": legacy,
        "event": event,
        "both": sorted(legacy_ids & event_ids),
        "only_legacy": only_legacy,
        "only_event": only_event,
        "in_sync": not only_legacy,
    }


class ShadowRecorder:
    """A non-publishing ``generate`` callback: records the request it would have narrated and
    performs no LLM call and no visible-narration write. Handed to ``narration_worker.drain``."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def __call__(self, event: Dict[str, Any]) -> Dict[str, Any]:
        receipt = {
            "shadow": True,
            "entity_type": event["entity_type"],
            "entity_id": event["entity_id"],
            "source_revision": event["source_revision"],
            "source_hash": event["source_hash"],
            "trace_id": event["trace_id"],
        }
        self.records.append(receipt)
        return receipt


def run_shadow_drain(project: str, *, worker_id: str = "shadow", max_items: int = 200,
                     now_fn=None) -> Dict[str, Any]:
    """Drain the outbox through the real claim/lease/coalesce/retry machine with a recorder that
    publishes nothing. Proves the worker settles cleanly end-to-end in shadow and returns what it
    would have narrated. Consumes the drained rows (marks them delivered) — intended for tests
    and one-off soak exercises, not the repeatable read-only comparison above."""
    recorder = ShadowRecorder()
    kwargs: Dict[str, Any] = {"worker_id": worker_id, "generate": recorder, "max_items": max_items}
    if now_fn is not None:
        kwargs["now_fn"] = now_fn
    outcomes = narration_worker.drain(project, **kwargs)
    return {"records": recorder.records, "outcomes": outcomes}
