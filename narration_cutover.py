"""Event-driven narration primary-path cutover (NARRATE-14).

This is the production integration that turns the staged NARRATE-8..12 machinery into the live
narration path. It owns ADR-0008 **boundary 3 — publish (compare-and-swap)**, which the worker
(NARRATE-9) and generator (NARRATE-12) deliberately left to the cutover: they record durable
receipts but never write the visible narration. Here we:

- ``make_production_generate`` — build the ``drain`` callback that generates a narration through the
  NARRATE-12 policy/budget engine AND publishes it to the visible surface
  (``task_narrations`` / deliverable ``ceo_narrative``) under a compare-and-swap guard, so a late or
  out-of-order delivery can never clobber a newer published revision.
- ``run_recovery_sweep`` — the SLOW recovery backstop the systemd timer runs: drain every project's
  outbox through the worker + publish boundary. Idempotent; safe to poll.
- ``register_production_wake_sink`` — register the post-commit wake accelerator so a healthy web
  process delivers within seconds of an emit (the primary trigger) instead of waiting for the sweep.
  The wake runs a bounded, debounced drain on a background thread so no LLM work lands on the request
  path, and a failed/missed wake never loses work (the durable outbox + sweep are the source of truth).

**Operator gate — merging this is a no-op in production.** Everything here is inert until
``PM_NARRATION_EVENT_PRIMARY`` is enabled. Until then the legacy ``pending_narrations`` timer stays
primary and the event path only records shadow receipts, so "do not publish from both paths" holds.
Enabling is the monitored cutover; disabling is the instant rollback lever. See
``docs/runbooks/narration-event-rollout.md``.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

import narration_generate
import narration_outbox
import narration_worker

# Bounded drain sizes. The wake accelerator runs inside the web process, so it stays small and
# yields quickly; the recovery sweep runs in the batch cgroup slice and can clear a larger backlog.
DEFAULT_WAKE_MAX_ITEMS = int(os.environ.get("PM_NARRATION_WAKE_MAX_ITEMS") or 12)
DEFAULT_SWEEP_MAX_ITEMS = int(os.environ.get("PM_NARRATION_SWEEP_MAX_ITEMS") or 100)

_PUBLISHABLE_OUTCOMES = ("delivered", "fallback")


def event_primary_enabled() -> bool:
    """Cutover gate (NARRATE-14). DEFAULT OFF: merging this code changes nothing in production
    until an operator sets ``PM_NARRATION_EVENT_PRIMARY=1`` for the monitored soak. When off the
    legacy ``pending_narrations`` timer stays primary, the recovery sweep no-ops, and the wake
    accelerator stays inert — so the event path only records shadow receipts and never double
    publishes. Setting it back to off is the instant rollback lever."""
    raw = (os.environ.get("PM_NARRATION_EVENT_PRIMARY") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


_WORKER_ID: Optional[str] = None


def _worker_id() -> str:
    """Stable per-process worker id, so lease fencing distinguishes this process from another
    worker (e.g. the recovery sweep vs a web-process wake drain)."""
    global _WORKER_ID
    if _WORKER_ID is None:
        _WORKER_ID = f"cutover:{os.getpid()}"
    return _WORKER_ID


def _conn(project: str):
    from db.connection import _conn as conn
    return conn(project)


def _published_revision(project: str, entity_type: str, entity_id: str) -> int:
    """The highest source_revision already published for an entity, read from the durable receipt
    ledger. Used as the compare-and-swap floor so an older revision never overwrites a newer one."""
    with _conn(project) as c:
        r = c.execute(
            "SELECT COALESCE(MAX(source_revision), 0) FROM narration_receipts "
            "WHERE project=? AND entity_type=? AND entity_id=? AND outcome IN ('delivered','fallback')",
            (project, entity_type, entity_id),
        ).fetchone()
    return int((r[0] if r else 0) or 0)


def _visible_source_fingerprint(project: str, entity_type: str, entity_id: str,
                                receipt: Mapping[str, Any]) -> str:
    """Fingerprint stamped on the visible narration surface for stale detection.

    The outbox ``source_hash`` is the projection hash used for coalescing and receipts; the UI
    stale discipline (``task_narration_fingerprint`` / ``brief_source_fingerprint``) uses a
    different contract. Publishing the projection hash made every event-driven narration look
    perpetually stale on the deliverable header and task details tab."""
    import mission_narrative
    import store

    if entity_type == "task":
        task = store.get_task(entity_id, project=project)
        if task:
            return store.task_narration_fingerprint(task)
    elif entity_type == "deliverable":
        status = store.get_mission_status(project=project, deliverable_id=entity_id)
        if not status.get("error"):
            return mission_narrative.brief_source_fingerprint(status)
    return receipt.get("source_hash") or ""


def _publish(project: str, receipt: Mapping[str, Any], *, prev_revision: int) -> bool:
    """Compare-and-swap publish of the visible narration (ADR-0008 boundary 3).

    Publishes only a delivered/fallback receipt that carries narration text AND whose
    source_revision is at least the highest already-published revision for the entity. That guard
    makes publish idempotent and monotonic: a slow or reordered delivery of an older revision is
    dropped instead of clobbering a newer visible narrative. Errors carry no text and never publish.
    """
    narration = receipt.get("narration")
    if not narration or receipt.get("outcome") not in _PUBLISHABLE_OUTCOMES:
        return False
    revision = int(receipt.get("source_revision") or 0)
    if revision < prev_revision:
        return False  # a newer revision already published — CAS drop
    entity_type = receipt.get("entity_type")
    entity_id = receipt.get("entity_id")
    if not entity_id:
        return False
    import store
    fingerprint = _visible_source_fingerprint(project, entity_type, entity_id, receipt)
    model = receipt.get("model") or receipt.get("mode") or ""
    if entity_type == "task":
        store.set_task_narration(entity_id, narration, activity_cursor=revision,
                                 source_fingerprint=fingerprint, model=model, project=project)
        return True
    if entity_type == "deliverable":
        result = store.set_deliverable_narration(entity_id, narration, source_fingerprint=fingerprint,
                                                 model=model, actor="narrator", project=project)
        return not (isinstance(result, dict) and result.get("error"))
    return False


def make_production_generate(project: str,
                             llm_fn: Optional[Callable[..., Dict[str, Any]]] = None,
                             now: Optional[float] = None) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """Build the ``drain`` generate callback for one project: generate through the NARRATE-12
    policy/budget engine, then compare-and-swap publish the visible narration.

    ``llm_fn`` is injectable for tests/canary; production uses NARRATE-12's default gateway client.
    The returned receipt is annotated with ``_published`` so drain callers/telemetry can see whether
    the visible surface was written (a routine deterministic re-narration may match and no-op)."""

    def _generate(event: Dict[str, Any]) -> Dict[str, Any]:
        entity_type = event.get("entity_type")
        entity_id = event.get("entity_id")
        # Snapshot the published floor BEFORE generate records this event's own receipt.
        prev_revision = _published_revision(project, entity_type, entity_id) if entity_id else 0
        kwargs: Dict[str, Any] = {}
        if llm_fn is not None:
            kwargs["llm_fn"] = llm_fn
        if now is not None:
            kwargs["now"] = now
        receipt = narration_generate.generate(event, **kwargs)
        receipt["_published"] = _publish(project, receipt, prev_revision=prev_revision)
        return receipt

    return _generate


def _tally(outcomes: List[Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for _event_id, outcome in outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
    counts["total"] = len(outcomes)
    return counts


def run_recovery_sweep(projects: Optional[List[str]] = None, *,
                       max_items: int = DEFAULT_SWEEP_MAX_ITEMS,
                       now_fn: Callable[[], float] = time.time,
                       llm_fn: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Slow recovery sweep: drain each project's narration outbox through the worker + publish
    boundary. This is the systemd-timer backstop for missed/failed wakes — durable, idempotent,
    and safe to run on a slow cadence. No-op unless the cutover is enabled, so it is safe to install
    the timer before flipping ``PM_NARRATION_EVENT_PRIMARY`` on."""
    if not event_primary_enabled():
        return {"enabled": False, "skipped": "event_primary_disabled", "projects": {}}
    import store
    targets = list(projects) if projects else store.project_ids()
    summary: Dict[str, Any] = {"enabled": True, "projects": {}}
    for project in targets:
        try:
            store.init_db(project)
            gen = make_production_generate(project, llm_fn=llm_fn)
            outcomes = narration_worker.drain(project, worker_id=_worker_id(), generate=gen,
                                               now_fn=now_fn, max_items=max_items)
            summary["projects"][project] = _tally(outcomes)
        except Exception as exc:  # one bad project never stalls the sweep for the others
            summary["projects"][project] = {"error": f"{type(exc).__name__}: {exc}"}
    return summary


# ---------------------------------------------------------------------------
# Wake accelerator (ADR-0008 boundary 1 -> wake). Registered in the web process so an emit's
# post-commit ``request_wake`` triggers a bounded, debounced background drain within seconds. The
# durable outbox + recovery sweep remain the source of truth: a dropped/failed wake never loses
# work, it only delays it to the next sweep.
# ---------------------------------------------------------------------------

_WAKE_LOCK = threading.Lock()
_WAKE_INFLIGHT: set = set()
_SINK_REGISTERED = False


def _drain_once_bg(project: str) -> None:
    try:
        if not event_primary_enabled():
            return
        import store
        store.init_db(project)
        gen = make_production_generate(project)
        narration_worker.drain(project, worker_id=_worker_id(), generate=gen,
                               max_items=DEFAULT_WAKE_MAX_ITEMS)
    except Exception:
        # Best-effort acceleration only. The recovery sweep is the durable backstop, so a failed
        # wake drain must never raise into the emitter's post-commit path or crash the web process.
        pass
    finally:
        with _WAKE_LOCK:
            _WAKE_INFLIGHT.discard(project)


def _wake_sink(project: str, **context: Any) -> None:
    """Post-commit wake sink: schedule one bounded background drain per project (debounced).

    Debounced because the outbox is the source of truth — a burst of emits needs at most one
    in-flight drain, and any request that lands after the drain started is caught by the next wake
    or the recovery sweep. No-op unless the cutover is enabled, so it is inert until flip."""
    if not event_primary_enabled():
        return
    with _WAKE_LOCK:
        if project in _WAKE_INFLIGHT:
            return
        _WAKE_INFLIGHT.add(project)
    threading.Thread(target=_drain_once_bg, args=(project,), daemon=True,
                     name=f"narrate-wake:{project}").start()


def register_production_wake_sink() -> bool:
    """Install the wake accelerator as the process-local narration wake sink. Idempotent. Safe to
    call at web-process startup unconditionally — the sink no-ops until the cutover is enabled."""
    global _SINK_REGISTERED
    if _SINK_REGISTERED:
        return False
    narration_outbox.register_wake_sink(_wake_sink)
    _SINK_REGISTERED = True
    return True
