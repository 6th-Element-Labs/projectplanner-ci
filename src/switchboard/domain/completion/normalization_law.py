"""Versioned normalization law for one fresh completion-controller tick.

This module is a contract, not a production router.  It gives the thin
orchestrator a finite vocabulary and makes every selected action carry the
authority, freshness, exact-head, idempotency, receipt, ownership, and repair
facts needed to execute it safely.

Classifier decisions from previous ticks are deliberately not accepted as an
input.  They are audit history.  Callers must provide the decision and effect
plan derived from the supplied fresh snapshot.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
from typing import Any, Mapping, Sequence


NORMALIZATION_LAW_SCHEMA = "switchboard.completion_normalization_law.v1"
NORMALIZED_TICK_SCHEMA = "switchboard.completion_normalized_tick.v1"
NORMALIZATION_TABLE_VERSION = "1"


class NormalizedAction(str, Enum):
    WAIT = "WAIT"
    START = "START"
    RETRY_CI = "RETRY_CI"
    MARK_READY = "MARK_READY"
    ARM_MERGE = "ARM_MERGE"
    BLOCK = "BLOCK"
    MERGED = "MERGED"


@dataclass(frozen=True)
class NormalizationLawRow:
    action: NormalizedAction
    authoritative_sources: tuple[str, ...]
    freshness_rule: str
    exact_head_semantics: str
    live_clock_bound_s: int
    idempotency_key: str
    effect: str
    receipt: str
    owner: str
    minimum_repair: str


@dataclass(frozen=True)
class FreshTick:
    """Facts read during one controller tick.

    ``source_observed_at`` contains immutable observation times, not mutable
    retry counters.  ``wait_started_at`` is the authoritative timestamp for
    the event that began a WAIT (for example the head, check, or queue event);
    callers must preserve it across ticks over unchanged state.
    ``prior_head_sha`` is allowed only to invalidate history; no prior
    classifier decision is represented here.
    """

    observed_at: float
    live_clock_at: float
    source_observed_at: Mapping[str, float]
    head_sha: str
    prior_head_sha: str = ""
    wait_started_at: float | None = None
    snapshot_id: str = ""
    controller_build_sha: str = ""
    table_version: str = NORMALIZATION_TABLE_VERSION


@dataclass(frozen=True)
class WaitEvidence:
    reason: str
    evidence: tuple[str, ...]
    owner: str
    deadline_at: float
    next_observation: str


@dataclass(frozen=True)
class BlockEvidence:
    reason: str
    owner: str
    live_evidence: tuple[str, ...]
    minimum_repair: str


LAW_ROWS: tuple[NormalizationLawRow, ...] = (
    NormalizationLawRow(
        NormalizedAction.WAIT,
        ("github_pr", "required_status_contexts", "merge_queue", "runner_sessions"),
        "all named sources are read in this tick; deadline derives from immutable timestamps",
        "wait evidence is scoped to the current PR head and invalidated by a new head",
        120,
        "completion run + state version + exact head + reason",
        "no external mutation",
        "durable wait observation",
        "named wait owner",
        "reason, evidence, owner, deadline, and next observation",
    ),
    NormalizationLawRow(
        NormalizedAction.START,
        ("completion_run", "runner_sessions", "autopilot_scope", "task"),
        "execution and scope leases are live at selection time",
        "review_merge starts only at the exact head; remediation may advance it",
        60,
        "task + role + generation + exact head",
        "start_task only",
        "wake/execution receipt",
        "Task Execution",
        "repair the failed admission fact, then take a fresh tick",
    ),
    NormalizationLawRow(
        NormalizedAction.RETRY_CI,
        ("github_check_runs", "required_status_contexts", "external_effects"),
        "latest immutable run timestamps and conclusions are read in this tick",
        "retry is deduplicated per exact head; a new head resets eligibility",
        120,
        "task + exact head + check identity + latest run id",
        "request one CI rerun",
        "verified CI dispatch receipt",
        "Task Execution",
        "restore CI infrastructure or authority; never dispatch code remediation",
    ),
    NormalizationLawRow(
        NormalizedAction.MARK_READY,
        ("github_pr", "review_verdict"),
        "draft and exact-head review are read in this tick",
        "review verdict and PR head must match before mutation",
        60,
        "repository + PR number + exact head + mark-ready",
        "mark PR ready",
        "GitHub readback showing draft=false at the exact head",
        "Task Execution",
        "obtain exact-head review or refresh the PR observation",
    ),
    NormalizationLawRow(
        NormalizedAction.ARM_MERGE,
        ("github_pr", "required_status_contexts", "review_verdict", "merge_gate"),
        "all merge gates are re-read in this tick",
        "every gate, effect key, and queue request names the exact current head",
        60,
        "repository + PR number + exact head + queue operation",
        "arm native merge queue once",
        "verified merge-queue entry receipt",
        "one review_merge generation",
        "refresh or repair the failing exact-head gate",
    ),
    NormalizationLawRow(
        NormalizedAction.BLOCK,
        ("completion_run", "live_authority_evidence", "runner_sessions"),
        "the blocking authority evidence is live in this tick",
        "head-scoped blockers are invalidated by a new head",
        120,
        "completion run + state version + exact head + blocker code",
        "persist blocker only",
        "durable blocker receipt",
        "named blocker owner",
        "owner, live evidence, and minimum repair",
    ),
    NormalizationLawRow(
        NormalizedAction.MERGED,
        ("canonical_github_provenance",),
        "canonical PR/default-branch provenance is read in this tick",
        "merged SHA and PR head are observed, never controller-authored",
        120,
        "repository + PR number + canonical merged SHA",
        "no Done mutation; report observed merge",
        "canonical provenance observation",
        "GitHub webhook or reconciliation",
        "restore canonical provenance observation; never synthesize Done",
    ),
)

LAW_BY_ACTION = {row.action: row for row in LAW_ROWS}

_PLAN_ACTIONS = {
    "wait": NormalizedAction.WAIT,
    "attach_and_wait": NormalizedAction.WAIT,
    "none": NormalizedAction.WAIT,
    "ensure_review_generation": NormalizedAction.START,
    "start_remediation": NormalizedAction.START,
    "repair_dispatch": NormalizedAction.RETRY_CI,
    "mark_ready": NormalizedAction.MARK_READY,
    "enqueue": NormalizedAction.ARM_MERGE,
    "escalate_human": NormalizedAction.BLOCK,
    "fence_runner": NormalizedAction.BLOCK,
    "reconcile_provenance": NormalizedAction.MERGED,
}

_INFRASTRUCTURE_ATTRIBUTIONS = frozenset({
    "infrastructure", "authority", "policy", "unknown",
})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _finite_time(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite timestamp") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite timestamp")
    return result


def validate_law_table(
    rows: Sequence[NormalizationLawRow] = LAW_ROWS,
) -> dict[str, Any]:
    errors: list[str] = []
    actions = [row.action for row in rows]
    if len(actions) != len(set(actions)):
        errors.append("actions must be unique")
    missing = sorted(action.value for action in set(NormalizedAction) - set(actions))
    if missing:
        errors.append(f"missing actions: {', '.join(missing)}")
    for row in rows:
        for field, value in asdict(row).items():
            if field == "live_clock_bound_s":
                if not isinstance(value, int) or value <= 0:
                    errors.append(f"{row.action.value}.live_clock_bound_s must be positive")
            elif field == "authoritative_sources":
                if not value or any(not _text(item) for item in value):
                    errors.append(f"{row.action.value}.authoritative_sources must be non-empty")
            elif not _text(value):
                errors.append(f"{row.action.value}.{field} must be non-empty")
    return {
        "schema": NORMALIZATION_LAW_SCHEMA,
        "valid": not errors,
        "errors": errors,
        "actions": [action.value for action in actions],
        "rows": [asdict(row) for row in rows],
    }


def _validate_tick(tick: FreshTick, row: NormalizationLawRow) -> None:
    observed_at = _finite_time(tick.observed_at, "observed_at")
    live_clock_at = _finite_time(tick.live_clock_at, "live_clock_at")
    if abs(live_clock_at - observed_at) > row.live_clock_bound_s:
        raise ValueError("live clock is outside the action bound")
    if not _text(tick.head_sha):
        raise ValueError("fresh tick requires an exact head_sha")
    for source in row.authoritative_sources:
        if source not in tick.source_observed_at:
            raise ValueError(f"fresh tick is missing authoritative source {source}")
        source_at = _finite_time(
            tick.source_observed_at[source], f"source_observed_at.{source}")
        if source_at > live_clock_at or live_clock_at - source_at > row.live_clock_bound_s:
            raise ValueError(f"authoritative source {source} is stale")


def _wait_evidence(
    *, reason: str, row: NormalizationLawRow, tick: FreshTick,
) -> WaitEvidence:
    if tick.wait_started_at is None:
        raise ValueError("WAIT requires an authoritative wait_started_at")
    wait_started_at = _finite_time(tick.wait_started_at, "wait_started_at")
    if wait_started_at > tick.live_clock_at:
        raise ValueError("wait_started_at may not be in the future")
    return WaitEvidence(
        reason=reason or "fresh_observation_pending",
        evidence=tuple(row.authoritative_sources),
        owner=row.owner,
        deadline_at=wait_started_at + row.live_clock_bound_s,
        next_observation=row.freshness_rule,
    )


def _block_evidence(
    *, reason: str, row: NormalizationLawRow,
) -> BlockEvidence:
    return BlockEvidence(
        reason=reason or "authority_blocked",
        owner=row.owner,
        live_evidence=tuple(row.authoritative_sources),
        minimum_repair=row.minimum_repair,
    )


def normalize_fresh_tick(
    *,
    decision: Mapping[str, Any],
    plan: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    tick: FreshTick,
) -> dict[str, Any]:
    """Normalize one freshly classified plan to the versioned law vocabulary."""
    plan_map, decision_map, snapshot_map = _map(plan), _map(decision), _map(snapshot)
    effect = _text(plan_map.get("effect")).lower()
    if effect not in _PLAN_ACTIONS:
        raise ValueError(f"unsupported completion effect {effect!r}")
    action = _PLAN_ACTIONS[effect]
    row = LAW_BY_ACTION[action]
    _validate_tick(tick, row)
    wait = (
        _wait_evidence(
            reason=_text(plan_map.get("reason_code") or decision_map.get("reason_code")),
            row=row,
            tick=tick,
        )
        if action is NormalizedAction.WAIT else None
    )
    wait_expired = bool(wait and tick.live_clock_at >= wait.deadline_at)

    snapshot_head = _text(snapshot_map.get("head_sha"))
    plan_head = _text(plan_map.get("head_sha"))
    if snapshot_head != tick.head_sha or plan_head != tick.head_sha:
        raise ValueError("decision, plan, and fresh snapshot must share the exact head")

    reason = _text(plan_map.get("reason_code") or decision_map.get("reason_code"))
    head_changed = bool(tick.prior_head_sha and tick.prior_head_sha != tick.head_sha)
    invalidated = (
        ["ci", "review", "merge_queue", "retry"]
        if head_changed else []
    )

    if action is NormalizedAction.START:
        role = _text(plan_map.get("role")).lower()
        if role == "remediation":
            contexts = _map(snapshot_map.get("status_contexts"))
            attributions = {
                _text(_map(item).get("failure_attribution")).lower()
                for item in contexts.values()
            }
            if attributions and attributions <= _INFRASTRUCTURE_ATTRIBUTIONS:
                raise ValueError("infrastructure classes may not select remediation")

    missing_merge_provenance = False
    if action is NormalizedAction.MERGED:
        github_pr = _map(snapshot_map.get("github_pr"))
        provenance = _map(snapshot_map.get("merge_provenance"))
        missing_merge_provenance = (
            _text(github_pr.get("state")).upper() != "MERGED"
            and not _text(
                provenance.get("merged_sha")
                or provenance.get("canonical_merged_sha")
            )
        )

    normalized: dict[str, Any] = {
        "schema": NORMALIZED_TICK_SCHEMA,
        "law_schema": NORMALIZATION_LAW_SCHEMA,
        "action": action.value,
        "task_id": _text(plan_map.get("task_id") or snapshot_map.get("task_id")).upper(),
        "head_sha": tick.head_sha,
        "reason_code": reason,
        "effect": row.effect,
        "idempotency_key": _text(plan_map.get("idem_key")),
        "receipt_contract": row.receipt,
        "owner": row.owner,
        "snapshot_id": _text(tick.snapshot_id),
        "observed_at": tick.observed_at,
        "controller_build_sha": _text(tick.controller_build_sha),
        "table_version": _text(tick.table_version),
        "live_clock_at": tick.live_clock_at,
        "source_observed_at": dict(tick.source_observed_at),
        "head_changed": head_changed,
        "invalidated_evidence": invalidated,
        "controller_may_author_done": False,
    }
    if not normalized["task_id"] or not normalized["idempotency_key"]:
        raise ValueError("normalized tick requires task_id and idempotency_key")
    supplied_tick_identity = bool(
        tick.snapshot_id or tick.controller_build_sha
        or tick.table_version != NORMALIZATION_TABLE_VERSION
    )
    if supplied_tick_identity and not all(
        normalized[field]
        for field in ("snapshot_id", "controller_build_sha", "table_version")
    ):
        raise ValueError(
            "fresh tick identity requires snapshot_id, controller_build_sha, "
            "and table_version"
        )
    if action is NormalizedAction.WAIT and not wait_expired:
        normalized["wait"] = asdict(wait)
    if action is NormalizedAction.WAIT and wait_expired:
        normalized.update({
            "action": NormalizedAction.BLOCK.value,
            "reason_code": "wait_deadline_expired",
            "effect": LAW_BY_ACTION[NormalizedAction.BLOCK].effect,
            "idempotency_key": (
                f"{normalized['idempotency_key']}:wait_deadline_expired"
            ),
            "receipt_contract": LAW_BY_ACTION[NormalizedAction.BLOCK].receipt,
            "owner": wait.owner,
            "block": asdict(BlockEvidence(
                reason="wait_deadline_expired",
                owner=wait.owner,
                live_evidence=wait.evidence,
                minimum_repair=(
                    "repair or advance the awaited authoritative event, "
                    "then take a fresh tick"
                ),
            )),
            "expired_wait": asdict(wait),
        })
    if action is NormalizedAction.BLOCK:
        normalized["block"] = asdict(_block_evidence(reason=reason, row=row))
    retry_block_reason = ""
    if action is NormalizedAction.RETRY_CI:
        if reason != "required_ci_infrastructure_failure":
            retry_block_reason = "coordination_retry_live_evidence_missing"
        elif not _text(plan_map.get("failing_check_url")):
            retry_block_reason = "ci_retry_identity_missing"
        else:
            try:
                run_attempt = int(plan_map.get("failing_run_attempt") or 0)
            except (TypeError, ValueError):
                run_attempt = 0
            if run_attempt < 1:
                retry_block_reason = "ci_retry_history_missing"
            elif run_attempt > 1:
                retry_block_reason = "ci_retry_bound_exhausted"
    if retry_block_reason:
        block_row = LAW_BY_ACTION[NormalizedAction.BLOCK]
        normalized.update({
            "action": NormalizedAction.BLOCK.value,
            "reason_code": retry_block_reason,
            "effect": block_row.effect,
            "idempotency_key": (
                f"{normalized['idempotency_key']}:{retry_block_reason}"
            ),
            "receipt_contract": block_row.receipt,
            "owner": block_row.owner,
            "block": asdict(_block_evidence(
                reason=retry_block_reason,
                row=block_row,
            )),
        })
    if missing_merge_provenance:
        block_row = LAW_BY_ACTION[NormalizedAction.BLOCK]
        normalized.update({
            "action": NormalizedAction.BLOCK.value,
            "reason_code": "canonical_merge_provenance_missing",
            "effect": block_row.effect,
            "idempotency_key": (
                f"{normalized['idempotency_key']}:"
                "canonical_merge_provenance_missing"
            ),
            "receipt_contract": block_row.receipt,
            "owner": block_row.owner,
            "block": asdict(_block_evidence(
                reason="canonical_merge_provenance_missing",
                row=block_row,
            )),
        })
    return normalized


__all__ = [
    "BlockEvidence",
    "FreshTick",
    "LAW_BY_ACTION",
    "LAW_ROWS",
    "NORMALIZATION_LAW_SCHEMA",
    "NORMALIZATION_TABLE_VERSION",
    "NORMALIZED_TICK_SCHEMA",
    "NormalizedAction",
    "NormalizationLawRow",
    "WaitEvidence",
    "normalize_fresh_tick",
    "validate_law_table",
]
