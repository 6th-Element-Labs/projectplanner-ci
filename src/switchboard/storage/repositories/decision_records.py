"""Append-only decision corpus (COORD-50, docs/DECISION-CORPUS-SPEC.md).

``completion_runs`` is the *current-state* authority: one row per task, upserted with
``ON CONFLICT(task_id) DO UPDATE SET ... reason_code=excluded.reason_code``. Its
append-only companion ``task_execution_completion_phases`` has no ``reason_code``
column at all. So the reason-code timeline was destroyed as it was produced, and
counting it was impossible — there was nothing durable to count.

This module is the durable half. Every classified tick appends here; identical
consecutive observations collapse into one *episode* with ``tick_count``
incremented, so a task stuck for two hundred ticks contributes one row rather than
dominating every count with polling frequency.

Retention (spec §5): the projected half is kept indefinitely; snapshot bodies are
dropped after 90 days *except* for escalated, non-convergent, or human-resolved
episodes — the scarce demonstrations.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Iterable, Mapping, Optional

from constants import DEFAULT_PROJECT
from db.connection import _conn, _write_through
from switchboard.domain.decisions.features import (
    DIAGNOSTIC_FIELDS,
    FEATURE_FIELDS,
    FEATURES_VERSION,
    project_diagnostics,
    project_features,
    strip_diagnostics,
)
from switchboard.domain.decisions.outcomes import (
    DECISION_OUTCOMES,
    OPEN_OUTCOME,
    OUTCOMES,
    RETAINED_OUTCOMES,
    TERMINAL_OUTCOMES,
    is_registered_outcome,
    is_terminal_outcome,
    normalize_outcome,
)
from switchboard.domain.decisions.reason_codes import (
    canonical_reason_code,
    get_reason_code,
    is_registered,
    is_retry_budget_exhausted,
)


SCHEMA = "switchboard.decision_record.v1"
COUNTS_SCHEMA = "switchboard.decision_reason_code_counts.v1"
OUTCOME_SCHEMA = "switchboard.decision_episode_outcome.v1"
REPLAY_SCHEMA = "switchboard.decision_corpus_replay.v1"

DEFAULT_SNAPSHOT_TTL_DAYS = 90

# Columns an export may read. Asserted by test to exclude every private-half column;
# widening this list is a code change reviewable on its own, which is the point of
# materializing the projection at write time (spec §6).
EXPORT_COLUMNS = (
    "project",
    "reason_code",
    "route",
    "desired_role",
    "features_json",
    "features_version",
    "classifier_version",
    "advice_version",
    "tick_count",
    "outcome",
    "head_advanced",
    "generations_spent",
    "human_intervened",
)

# Never exported, and named here so the guarantee is testable rather than implied.
PRIVATE_COLUMNS = (
    "snapshot_json",
    "decision_json",
    "snapshot_hash",
    "head_sha",
    "merged_sha",
    "pr_number",
    "task_id",
    "deliverable_id",
    "host_id",
    "record_id",
    "human_action",
    "first_seen_at",
    "last_seen_at",
    "generation",
    "fence_epoch",
    # §3.2 identity. It names one runner session in one environment, so it is exactly
    # as private as host_id, and is excluded for the same reason.
    "execution_id",
)


class DecisionRecordError(ValueError):
    pass


def _map(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def episode_hash(
    *,
    reason_code: str,
    route: str,
    desired_role: str,
    head_sha: str,
    pr_number: int,
    generation: int,
    fence_epoch: int,
    features: Mapping[str, Any],
) -> str:
    """Identity of one decision episode.

    Deliberately *not* a hash of the raw snapshot. The hydrated snapshot embeds the
    whole task row and session-health probe, both of which carry timestamps that
    change on every tick — hashing them would make every tick unique and defeat the
    episode collapsing in spec §5 entirely. The identity is the decision-relevant
    content: the classified verdict, the exact-head fence, and the materialized
    feature vector. Anything that would change the decision changes the hash.
    """
    payload = _canonical_json({
        "reason_code": canonical_reason_code(reason_code),
        "route": str(route or "").strip().lower(),
        "desired_role": str(desired_role or "").strip().lower(),
        "head_sha": str(head_sha or "").strip().lower(),
        "pr_number": _int(pr_number),
        "generation": _int(generation),
        "fence_epoch": _int(fence_epoch),
        "features": {name: features.get(name) for name in FEATURE_FIELDS},
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _column(row: Any, name: str, default: Any = None) -> Any:
    """Read one column tolerantly.

    A DB whose additive migration has not run yet has no ``execution_id``; reading the
    episode must still work, and the absence must read as absent rather than crash a
    completion tick on a corpus column that gates nothing.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def _row(row: Any) -> Optional[dict[str, Any]]:
    if not row:
        return None
    reason_code = row["reason_code"] or ""
    entry = get_reason_code(reason_code)
    outcome = normalize_outcome(row["outcome"]) or OPEN_OUTCOME
    return {
        "schema": SCHEMA,
        "record_id": row["record_id"],
        "project": row["project"],
        "task_id": row["task_id"],
        "pr_number": _int(row["pr_number"]),
        "head_sha": row["head_sha"] or "",
        "generation": _int(row["generation"]),
        "fence_epoch": _int(row["fence_epoch"]),
        "deliverable_id": row["deliverable_id"] or "",
        "host_id": row["host_id"] or "",
        # §3.2: resolves to a runner session via (execution_id) directly, or via
        # (host_id, generation, fence_epoch) for episodes written before 0120.
        "execution_id": _column(row, "execution_id") or "",
        "snapshot_hash": row["snapshot_hash"],
        "snapshot": _map(row["snapshot_json"]),
        "snapshot_retained": bool(_int(row["snapshot_retained"])),
        "decision": _map(row["decision_json"]),
        "classifier_version": row["classifier_version"],
        "reason_code": reason_code,
        "reason_code_registered": is_registered(reason_code),
        "reason_code_family": entry.family if entry else "",
        "route": row["route"] or "",
        "desired_role": row["desired_role"] or "",
        "features": _map(row["features_json"]),
        "features_version": row["features_version"],
        "advice_version": row["advice_version"],
        "tick_count": _int(row["tick_count"]),
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "outcome": outcome,
        # An outcome nobody declared is surfaced, never silently counted as closed.
        "outcome_registered": is_registered_outcome(outcome),
        "outcome_terminal": is_terminal_outcome(outcome),
        "head_advanced": bool(_int(row["head_advanced"])),
        "generations_spent": _int(row["generations_spent"]),
        "merged_sha": row["merged_sha"] or "",
        "human_intervened": bool(_int(row["human_intervened"])),
        "human_action": row["human_action"] or "",
    }


def _deliverable_for(c: Any, task_id: str, project: str) -> str:
    row = c.execute(
        "SELECT deliverable_id FROM deliverable_task_links "
        "WHERE project_id=? AND task_id=? ORDER BY created_at LIMIT 1",
        (project, task_id),
    ).fetchone()
    return str(row["deliverable_id"] or "") if row else ""


def _finish_episode(
    c: Any,
    *,
    project: str,
    task_id: str,
    record_id: str,
    reason_code: str,
    superseded: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """Apply the tick-time closures, then read the episode back.

    §3.1's fifth trigger: a reason code that asserts the retry budget is spent means
    no further attempt will be made, so the task's open episodes — this one included —
    are ``abandoned``. It is applied here rather than in the driver so it commits with
    the episode that classified it; a separate write could record the classification
    and lose its consequence, which is the compute-then-discard shape this whole task
    exists to remove.
    """
    abandoned = (
        abandon_open_episodes_in(c, project=project, task_id=task_id)
        if is_retry_budget_exhausted(reason_code) else {"updated": 0}
    )
    record = _row(c.execute(
        "SELECT * FROM decision_records WHERE record_id=?", (record_id,),
    ).fetchone())
    if record is not None:
        record["superseded_prior_episodes"] = _int(superseded.get("updated"))
        record["abandoned_open_episodes"] = _int(abandoned.get("updated"))
    return record


def record_decision_episode_in(
    c: Any,
    *,
    project: str,
    snapshot: Mapping[str, Any],
    decision: Mapping[str, Any],
    classifier_version: str,
    advice_version: Optional[str] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Append one classified tick as an episode on the caller's transaction.

    Idempotent by construction: an identical observation increments ``tick_count``
    and advances ``last_seen_at`` instead of writing a second row.
    """
    snap = _map(snapshot)
    verdict = _map(decision)
    task_id = str(snap.get("task_id") or "").strip().upper()
    if not task_id:
        raise DecisionRecordError("decision records require a task_id")
    if not str(classifier_version or "").strip():
        raise DecisionRecordError("decision records require a classifier_version")

    runner = _map(snap.get("runner"))
    reason_code = canonical_reason_code(verdict.get("reason_code"))
    route = str(verdict.get("route") or "").strip().lower()
    desired_role = str(verdict.get("desired_role") or "").strip().lower()
    head_sha = str(snap.get("head_sha") or "").strip().lower()
    pr_number = _int(snap.get("pr_number"))
    generation = _int(runner.get("generation"))
    fence_epoch = _int(runner.get("fence_epoch"))
    host_id = str(runner.get("host_id") or "").strip()
    execution_id = str(runner.get("execution_id") or "").strip()

    features = project_features(snap, verdict)
    snapshot_hash = episode_hash(
        reason_code=reason_code,
        route=route,
        desired_role=desired_role,
        head_sha=head_sha,
        pr_number=pr_number,
        generation=generation,
        fence_epoch=fence_epoch,
        features=features,
    )
    # §3.3 identity rides in features_json but is NOT part of the episode identity:
    # hashing a check URL that changes per CI run would fragment one episode into many
    # and defeat the collapsing in parent spec §5, which is the same trap that kept
    # episode_hash off the raw snapshot.
    stored_features = {**features, **project_diagnostics(snap, verdict)}
    stamp = float(now if now is not None else time.time())

    # §3.1 head advance. This is the seam the spec describes as "new head_sha observed
    # for a task": the driver is the only producer of episodes, so an episode arriving
    # at a *different* head is precisely that observation — and it is transactional
    # with the insert rather than dependent on a webhook that may never be delivered.
    # Guarded on a non-empty new head: losing PR hydration is a head regression, not an
    # advance, and recording head_advanced=1 for it would be a lie.
    superseded = (
        supersede_prior_episodes_in(
            c, project=project, task_id=task_id, head_sha=head_sha)
        if head_sha else {"updated": 0}
    )

    existing = c.execute(
        "SELECT * FROM decision_records "
        "WHERE project=? AND task_id=? AND snapshot_hash=?",
        (project, task_id, snapshot_hash),
    ).fetchone()
    if existing:
        # Backfill-only on collapse: an episode's decision is immutable, but identity
        # that was merely unavailable on the first tick is worth filling in once it
        # arrives. Never overwrite a value already recorded.
        c.execute(
            "UPDATE decision_records SET tick_count=tick_count+1, last_seen_at=?, "
            "execution_id=CASE WHEN execution_id='' THEN ? ELSE execution_id END, "
            "features_json=CASE WHEN features_json=? THEN ? ELSE features_json END "
            "WHERE record_id=?",
            (
                stamp, execution_id,
                _canonical_json(features), _canonical_json(stored_features),
                existing["record_id"],
            ),
        )
        return _finish_episode(
            c, project=project, task_id=task_id,
            record_id=existing["record_id"], reason_code=reason_code,
            superseded=superseded,
        )

    record_id = "decision-" + uuid.uuid4().hex[:20]
    c.execute(
        "INSERT INTO decision_records("
        "record_id, project, task_id, pr_number, head_sha, generation, fence_epoch, "
        "deliverable_id, host_id, execution_id, snapshot_hash, snapshot_json, "
        "decision_json, classifier_version, reason_code, route, desired_role, "
        "features_json, features_version, advice_version, tick_count, "
        "first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
        (
            record_id, project, task_id, pr_number, head_sha, generation,
            fence_epoch, _deliverable_for(c, task_id, project), host_id,
            execution_id, snapshot_hash, _canonical_json(snap),
            _canonical_json(verdict), str(classifier_version), reason_code, route,
            desired_role, _canonical_json(stored_features), FEATURES_VERSION,
            advice_version or None, stamp, stamp,
        ),
    )
    return _finish_episode(
        c, project=project, task_id=task_id, record_id=record_id,
        reason_code=reason_code, superseded=superseded,
    )


def record_decision_episode(
    *,
    project: str = DEFAULT_PROJECT,
    snapshot: Mapping[str, Any],
    decision: Mapping[str, Any],
    classifier_version: str,
    advice_version: Optional[str] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    def write():
        with _conn(project) as c:
            return record_decision_episode_in(
                c,
                project=project,
                snapshot=snapshot,
                decision=decision,
                classifier_version=classifier_version,
                advice_version=advice_version,
                now=now,
            )

    return _write_through(project, write)


def _window_clause(
    project: str,
    since: Optional[float],
    until: Optional[float],
    task_id: str,
    deliverable_id: str,
    host_id: str,
) -> tuple[str, list[Any]]:
    clauses = ["project=?"]
    params: list[Any] = [project]
    if since is not None:
        clauses.append("last_seen_at >= ?")
        params.append(float(since))
    if until is not None:
        clauses.append("first_seen_at <= ?")
        params.append(float(until))
    if task_id:
        clauses.append("task_id=?")
        params.append(str(task_id).strip().upper())
    if deliverable_id:
        clauses.append("deliverable_id=?")
        params.append(str(deliverable_id).strip())
    if host_id:
        clauses.append("host_id=?")
        params.append(str(host_id).strip())
    return " AND ".join(clauses), params


def count_reason_code_episodes(
    *,
    project: str = DEFAULT_PROJECT,
    since: Optional[float] = None,
    until: Optional[float] = None,
    task_id: str = "",
    deliverable_id: str = "",
    host_id: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """Reason-code concentration over one window, counting episodes not ticks.

    ``episodes`` is the number of distinct decision episodes; ``ticks`` is how many
    times the driver re-observed them. A retry loop inflates ``ticks`` and leaves
    ``episodes`` alone, which is the whole point — the DOGFOOD-19 stall was three
    tasks emitting one code, not three hundred polls.

    Unregistered codes are reported with ``registered: false`` and listed separately
    rather than counted as anonymous free text.

    COORD-51 §3.1 adds the outcome half. Each code carries its outcome breakdown plus
    ``open_episodes``/``terminal_episodes`` and a ``never_terminal`` flag, so the
    question "which reason codes never converge" is a field rather than a forensics
    exercise. ``never_terminal_reason_codes`` collects them at the top level: those are
    the codes whose loop, on this window's evidence, has no exit.
    """
    where, params = _window_clause(
        project, since, until, task_id, deliverable_id, host_id)
    with _conn(project) as c:
        rows = c.execute(
            "SELECT reason_code, route, outcome, "
            "COUNT(*) AS episodes, "
            "SUM(tick_count) AS ticks, "
            "COUNT(DISTINCT task_id) AS tasks, "
            "SUM(head_advanced) AS head_advanced, "
            "SUM(generations_spent) AS generations_spent, "
            "SUM(human_intervened) AS human_intervened, "
            "MIN(first_seen_at) AS first_seen_at, "
            "MAX(last_seen_at) AS last_seen_at "
            f"FROM decision_records WHERE {where} "
            "GROUP BY reason_code, route, outcome "
            "ORDER BY episodes DESC, reason_code ASC",
            tuple(params),
        ).fetchall()

    # Group by (code, route) as before; the outcome dimension folds into a breakdown
    # so the existing shape of `codes` is preserved for current readers.
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    unregistered_outcomes: set[str] = set()
    for row in rows:
        code = row["reason_code"] or ""
        route = row["route"] or ""
        outcome = normalize_outcome(row["outcome"]) or OPEN_OUTCOME
        if not is_registered_outcome(outcome):
            unregistered_outcomes.add(outcome)
        episodes = _int(row["episodes"])
        item = grouped.get((code, route))
        if item is None:
            entry = get_reason_code(code)
            item = {
                "reason_code": code,
                "route": route,
                "registered": entry is not None,
                "family": entry.family if entry else "",
                "expected": entry.expected if entry else "",
                "resolver": entry.resolver if entry else "",
                "episodes": 0,
                "ticks": 0,
                "tasks": 0,
                "outcomes": {},
                "open_episodes": 0,
                "terminal_episodes": 0,
                "head_advanced_episodes": 0,
                "generations_spent": 0,
                "human_intervened_episodes": 0,
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
            }
            grouped[(code, route)] = item
        item["episodes"] += episodes
        item["ticks"] += _int(row["ticks"])
        # Distinct task counts do not sum across outcome groups; the per-group max is
        # a floor, and claiming a sum would overstate breadth.
        item["tasks"] = max(item["tasks"], _int(row["tasks"]))
        item["outcomes"][outcome] = item["outcomes"].get(outcome, 0) + episodes
        item["head_advanced_episodes"] += _int(row["head_advanced"])
        item["generations_spent"] += _int(row["generations_spent"])
        item["human_intervened_episodes"] += _int(row["human_intervened"])
        if is_terminal_outcome(outcome):
            item["terminal_episodes"] += episodes
        else:
            item["open_episodes"] += episodes
        item["first_seen_at"] = min(
            item["first_seen_at"], row["first_seen_at"],
            key=lambda value: float(value or 0.0),
        )
        item["last_seen_at"] = max(
            item["last_seen_at"], row["last_seen_at"],
            key=lambda value: float(value or 0.0),
        )

    codes = list(grouped.values())
    for item in codes:
        item["never_terminal"] = item["terminal_episodes"] == 0

    total_episodes = sum(item["episodes"] for item in codes)
    ranked = sorted(codes, key=lambda item: (-item["episodes"], item["reason_code"]))
    for item in ranked:
        item["share"] = (
            round(item["episodes"] / total_episodes, 4) if total_episodes else 0.0
        )
    dominant = ranked[0] if ranked else None
    return {
        "schema": COUNTS_SCHEMA,
        "project": project,
        "window": {"since": since, "until": until},
        "filters": {
            "task_id": str(task_id or "").strip().upper(),
            "deliverable_id": str(deliverable_id or "").strip(),
            "host_id": str(host_id or "").strip(),
        },
        "total_episodes": total_episodes,
        "total_ticks": sum(item["ticks"] for item in codes),
        "distinct_tasks": max((item["tasks"] for item in codes), default=0),
        "open_episodes": sum(item["open_episodes"] for item in codes),
        "terminal_episodes": sum(item["terminal_episodes"] for item in codes),
        "dominant_reason_code": dominant,
        "unregistered_reason_codes": [
            item["reason_code"] for item in ranked if not item["registered"]
        ],
        "unregistered_outcomes": sorted(unregistered_outcomes),
        # The §3.1 question, answered directly: codes with episodes in this window and
        # not one of them closed.
        "never_terminal_reason_codes": [
            item["reason_code"] for item in ranked if item["never_terminal"]
        ],
        "codes": ranked[: max(int(limit or 0), 0) or len(ranked)],
    }


def list_decision_episodes(
    *,
    project: str = DEFAULT_PROJECT,
    task_id: str = "",
    since: Optional[float] = None,
    until: Optional[float] = None,
    deliverable_id: str = "",
    host_id: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """The reason-code timeline. Oldest first — it is a history, not a status."""
    where, params = _window_clause(
        project, since, until, task_id, deliverable_id, host_id)
    with _conn(project) as c:
        rows = c.execute(
            f"SELECT * FROM decision_records WHERE {where} "
            "ORDER BY first_seen_at ASC, record_id ASC LIMIT ?",
            (*params, max(int(limit or 0), 1)),
        ).fetchall()
    return [record for record in (_row(row) for row in rows) if record]


def replay_decision_corpus(
    *,
    project: str = DEFAULT_PROJECT,
    since: Optional[float] = None,
    until: Optional[float] = None,
    task_id: str = "",
    limit: int = 1000,
) -> dict[str, Any]:
    """Replay retained snapshots through today's classifier without mutating state.

    The comparison is decision-to-decision. Snapshot hashes, classifier versions,
    timestamps, and feature projections are deliberately excluded: a newly hydrated
    snapshot that produces the same verdict is not a behavioral change.
    """
    from switchboard.domain.completion.state_machine import (
        COMPLETION_CLASSIFIER_VERSION,
        classify_completion,
    )

    where, params = _window_clause(project, since, until, task_id, "", "")
    with _conn(project) as c:
        rows = c.execute(
            "SELECT record_id, task_id, first_seen_at, last_seen_at, "
            "snapshot_json, snapshot_retained, decision_json, classifier_version "
            f"FROM decision_records WHERE {where} "
            "ORDER BY first_seen_at ASC, record_id ASC LIMIT ?",
            (*params, max(int(limit or 0), 1)),
        ).fetchall()

    changes: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    movements: dict[tuple[str, str], dict[str, Any]] = {}
    replayed = 0
    for row in rows:
        if not bool(_int(row["snapshot_retained"])):
            skipped.append({
                "record_id": row["record_id"],
                "task_id": row["task_id"],
                "reason": "snapshot_compacted",
            })
            continue
        snapshot = _map(row["snapshot_json"])
        if snapshot.get("schema") != "switchboard.completion_snapshot.v1":
            skipped.append({
                "record_id": row["record_id"],
                "task_id": row["task_id"],
                "reason": "unsupported_snapshot_schema",
                "snapshot_schema": snapshot.get("schema") or "",
            })
            continue
        recorded = _map(row["decision_json"])
        current = classify_completion(None, snapshot)
        replayed += 1
        if _canonical_json(recorded) == _canonical_json(current):
            continue
        old_code = canonical_reason_code(recorded.get("reason_code"))
        new_code = canonical_reason_code(current.get("reason_code"))
        movement = movements.setdefault((old_code, new_code), {
            "recorded_reason_code": old_code,
            "current_reason_code": new_code,
            "episodes": 0,
            "tasks": set(),
        })
        movement["episodes"] += 1
        movement["tasks"].add(str(row["task_id"]))
        changes.append({
            "record_id": row["record_id"],
            "task_id": row["task_id"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "recorded_classifier_version": row["classifier_version"],
            "current_classifier_version": COMPLETION_CLASSIFIER_VERSION,
            "recorded_verdict": recorded,
            "current_verdict": current,
            "snapshot": snapshot,
        })

    reason_code_movements = []
    for movement in movements.values():
        reason_code_movements.append({
            **movement,
            "tasks": sorted(movement["tasks"]),
        })
    reason_code_movements.sort(key=lambda item: (
        -item["episodes"],
        item["recorded_reason_code"],
        item["current_reason_code"],
    ))
    affected_tasks = sorted({item["task_id"] for item in changes})
    return {
        "schema": REPLAY_SCHEMA,
        "advisory": True,
        "mutates_state": False,
        "project": project,
        "window": {"since": since, "until": until},
        "filter": {"task_id": str(task_id or "").strip().upper()},
        "current_classifier_version": COMPLETION_CLASSIFIER_VERSION,
        "episodes_considered": len(rows),
        "episodes_replayed": replayed,
        "episodes_skipped": len(skipped),
        "changed_verdicts": len(changes),
        "affected_task_count": len(affected_tasks),
        "affected_tasks": affected_tasks,
        "reason_code_movements": reason_code_movements,
        "changes": changes,
        "skipped": skipped,
    }


def export_projection(
    *,
    project: str = DEFAULT_PROJECT,
    since: Optional[float] = None,
    until: Optional[float] = None,
    min_episodes: int = 1,
) -> list[dict[str, Any]]:
    """Read the projected half only, by explicit column list.

    Export never touches ``snapshot_json``. The privacy boundary is the column list
    in :data:`EXPORT_COLUMNS`, not the discipline of whoever writes the query.
    """
    columns = ", ".join(EXPORT_COLUMNS)
    where, params = _window_clause(project, since, until, "", "", "")
    with _conn(project) as c:
        rows = c.execute(
            f"SELECT {columns} FROM decision_records WHERE {where} "
            "AND tick_count >= ? ORDER BY reason_code ASC",
            (*params, max(int(min_episodes or 1), 1)),
        ).fetchall()

    exported: list[dict[str, Any]] = []
    for row in rows:
        record = {name: row[name] for name in EXPORT_COLUMNS}
        # COORD-51 §3.3 stores the failing check identity inside features_json so a
        # reader finds it where the projection lives. It is content, not shape:
        # context names leak internal tooling inventory and a check URL carries the
        # repository, both excluded from the poolable tier by parent spec §4.2. The
        # column list stays the boundary for columns; this is the same boundary for
        # the keys within one, enforced in code and asserted by test.
        record["features_json"] = _canonical_json(
            strip_diagnostics(_map(record.get("features_json")))
        )
        exported.append(record)
    return exported


def compact_decision_snapshots(
    *,
    project: str = DEFAULT_PROJECT,
    ttl_days: int = DEFAULT_SNAPSHOT_TTL_DAYS,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Drop snapshot bodies past their TTL, keeping the demonstrations (spec §5).

    Escalated, non-convergent, and human-resolved episodes are the scarcest and
    densest labels in the system and are retained indefinitely. Everything else
    keeps its projected half — which is the corpus — and loses the 5–50 KB body.
    """
    cutoff = float(now if now is not None else time.time()) - (
        max(int(ttl_days or 0), 0) * 86400.0
    )

    def write():
        with _conn(project) as c:
            retained = ",".join("?" for _ in sorted(RETAINED_OUTCOMES))
            cursor = c.execute(
                "UPDATE decision_records SET snapshot_json='{}', snapshot_retained=0 "
                "WHERE project=? AND snapshot_retained=1 AND last_seen_at < ? "
                f"AND outcome NOT IN ({retained}) "
                "AND human_intervened=0",
                (project, cutoff, *sorted(RETAINED_OUTCOMES)),
            )
            return {
                "schema": SCHEMA,
                "project": project,
                "cutoff": cutoff,
                "compacted": int(cursor.rowcount or 0),
            }

    return _write_through(project, write)


# ---------------------------------------------------------------------------
# §3.1 — close the outcome.
#
# ``decision_records`` shipped outcome, head_advanced, generations_spent,
# merged_sha, human_intervened and human_action under the comment "backfilled by
# reconcile / webhook". That backfill was never built, so all six sat at their
# defaults and every episode ever written read ``open`` — the ledger could prove a
# loop occurred and never that an attempt accomplished anything. On the CO-20 window
# of 2026-07-25 all 21 episodes read open/generations_spent=0, including the
# seventeen definitively superseded when the head moved on.
#
# Every writer below obeys three rules:
#   * it only ever moves a row **out of** ``open``, so it is idempotent by
#     construction and re-running a backfill changes nothing;
#   * it never rewrites ``reason_code``, ``features_json`` or ``snapshot_hash`` —
#     the episode stays the immutable unit;
#   * it takes the caller's connection, so the closure commits atomically with the
#     provenance/claim/board transition that caused it. A corpus that gates nothing
#     must never be able to roll back merge truth, and it never becomes a second,
#     separately-failing write either.
# ---------------------------------------------------------------------------


def _corpus_present(c: Any) -> bool:
    """Report whether this DB carries the corpus table yet.

    The corpus explicitly carries no authority: it gates nothing and routes nothing.
    A DB whose migration has not run must therefore not be able to fail a merge
    webhook, a Done stamp, or a claim revoke — losing merge provenance to a storage
    table that only records history would be a far worse defect than an unrecorded
    outcome.

    This is a *named* fallback, not a silent one: every closer returns
    ``skipped: True`` with ``reason='decision_records_absent'`` so the caller's result
    carries why nothing was written. Any other ``OperationalError`` — a corrupt table,
    a locked DB, a renamed column — still propagates.
    """
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_records'",
    ).fetchone() is not None


def _corpus_absent(project: str, task_id: str, outcome: str) -> dict[str, Any]:
    return {
        "schema": OUTCOME_SCHEMA,
        "project": project,
        "task_id": str(task_id or "").strip().upper(),
        "outcome": outcome,
        "updated": 0,
        "skipped": True,
        "reason": "decision_records_absent",
        "failure_class": "missing_data",
    }


def _close_open_episodes_in(
    c: Any,
    *,
    project: str,
    task_id: str,
    outcome: str,
    merged_sha: str = "",
    head_advanced: bool = False,
    generations_delta: int = 0,
    human_intervened: bool = False,
    human_action: str = "",
    exclude_head_sha: str = "",
) -> dict[str, Any]:
    """Move a task's still-open episodes to one registered terminal outcome."""
    outcome = normalize_outcome(outcome)
    if not is_registered_outcome(outcome):
        raise DecisionRecordError(f"unsupported decision outcome: {outcome}")
    if outcome == OPEN_OUTCOME:
        raise DecisionRecordError("closing an episode requires a terminal outcome")
    task_id = str(task_id or "").strip().upper()
    if not task_id:
        raise DecisionRecordError("closing episodes requires a task_id")
    if not _corpus_present(c):
        return _corpus_absent(project, task_id, outcome)

    clauses = ["project=?", "task_id=?", f"outcome='{OPEN_OUTCOME}'"]
    params: list[Any] = [project, task_id]
    if exclude_head_sha:
        clauses.append("head_sha<>?")
        params.append(str(exclude_head_sha).strip().lower())

    cursor = c.execute(
        "UPDATE decision_records SET outcome=?, "
        # An empty merged_sha must not erase one already recorded.
        "merged_sha=CASE WHEN ?<>'' THEN ? ELSE merged_sha END, "
        "head_advanced=CASE WHEN ?=1 THEN 1 ELSE head_advanced END, "
        "generations_spent=generations_spent+?, "
        "human_intervened=CASE WHEN ?=1 THEN 1 ELSE human_intervened END, "
        "human_action=CASE WHEN ?<>'' THEN ? ELSE human_action END "
        f"WHERE {' AND '.join(clauses)}",
        (
            outcome,
            str(merged_sha or ""), str(merged_sha or ""),
            1 if head_advanced else 0,
            max(_int(generations_delta), 0),
            1 if human_intervened else 0,
            str(human_action or ""), str(human_action or ""),
            *params,
        ),
    )
    return {
        "schema": OUTCOME_SCHEMA,
        "project": project,
        "task_id": task_id,
        "outcome": outcome,
        "updated": int(cursor.rowcount or 0),
    }


def supersede_prior_episodes_in(
    c: Any, *, project: str, task_id: str, head_sha: str,
) -> dict[str, Any]:
    """The head moved: every open episode about an older head is superseded.

    ``generations_spent`` increments rather than being set, so an episode carried
    across three head advances reports three spent generations. This is the label
    that separates *"remediation ran seven times"* from *"remediation ran seven times
    and resolved nothing"*.
    """
    head_sha = str(head_sha or "").strip().lower()
    if not head_sha:
        return {"schema": OUTCOME_SCHEMA, "project": project,
                "task_id": task_id, "outcome": "superseded", "updated": 0}
    return _close_open_episodes_in(
        c, project=project, task_id=task_id, outcome="superseded",
        head_advanced=True, generations_delta=1, exclude_head_sha=head_sha,
    )


def close_merged_episodes_in(
    c: Any, *, project: str, task_id: str, merged_sha: str,
) -> dict[str, Any]:
    """Canonical merge provenance closes every open episode for the task."""
    return _close_open_episodes_in(
        c, project=project, task_id=task_id, outcome="merged",
        merged_sha=merged_sha,
    )


def close_done_episodes_in(
    c: Any, *, project: str, task_id: str, merged_sha: str = "",
) -> dict[str, Any]:
    """A terminal Done closes every open episode for the task."""
    return _close_open_episodes_in(
        c, project=project, task_id=task_id, outcome="done",
        merged_sha=merged_sha,
    )


def abandon_open_episodes_in(
    c: Any, *, project: str, task_id: str,
) -> dict[str, Any]:
    """The retry budget was spent without resolving the reason code."""
    return _close_open_episodes_in(
        c, project=project, task_id=task_id, outcome="abandoned",
    )


def mark_human_intervention_in(
    c: Any, *, project: str, task_id: str, human_action: str,
    resolved: bool = False,
) -> dict[str, Any]:
    """Record that an operator took control of a task's open episodes.

    Per spec §3.1 this normally sets the ``human_intervened`` flag and the verb and
    leaves ``outcome`` alone: an operator revoking a claim has intervened in the loop,
    not necessarily ended it, and calling that terminal would hide a task that is
    still spinning. ``resolved=True`` is the separate case where the intervention
    *is* the closure and the episodes become ``human_resolved``.
    """
    task_id = str(task_id or "").strip().upper()
    if not task_id:
        raise DecisionRecordError("marking intervention requires a task_id")
    action = str(human_action or "").strip()
    if not action:
        raise DecisionRecordError("marking intervention requires a human_action")
    if not _corpus_present(c):
        return _corpus_absent(project, task_id, OPEN_OUTCOME)
    if resolved:
        return _close_open_episodes_in(
            c, project=project, task_id=task_id, outcome="human_resolved",
            human_intervened=True, human_action=action,
        )
    # Idempotent: an episode already flagged is left alone, so a repeated revoke does
    # not re-report work it did not do.
    cursor = c.execute(
        "UPDATE decision_records SET human_intervened=1, human_action=? "
        f"WHERE project=? AND task_id=? AND outcome='{OPEN_OUTCOME}' "
        "AND human_intervened=0",
        (action, project, task_id),
    )
    return {
        "schema": OUTCOME_SCHEMA,
        "project": project,
        "task_id": task_id,
        "outcome": OPEN_OUTCOME,
        "human_action": action,
        "updated": int(cursor.rowcount or 0),
    }


def _closer(name: str, function: Any) -> Any:
    """Wrap an ``_in`` closer as a standalone single-writer write."""

    def call(*, project: str = DEFAULT_PROJECT, **kwargs: Any) -> dict[str, Any]:
        def write():
            with _conn(project) as c:
                return function(c, project=project, **kwargs)

        return _write_through(project, write)

    call.__name__ = name
    call.__doc__ = function.__doc__
    return call


close_merged_episodes = _closer("close_merged_episodes", close_merged_episodes_in)
close_done_episodes = _closer("close_done_episodes", close_done_episodes_in)
abandon_open_episodes = _closer("abandon_open_episodes", abandon_open_episodes_in)
mark_human_intervention = _closer(
    "mark_human_intervention", mark_human_intervention_in)
supersede_prior_episodes = _closer(
    "supersede_prior_episodes", supersede_prior_episodes_in)


def record_episode_outcome(
    *,
    project: str = DEFAULT_PROJECT,
    task_id: str,
    outcome: str,
    merged_sha: str = "",
    head_advanced: bool = False,
    generations_spent: int = 0,
    human_intervened: bool = False,
    human_action: str = "",
    record_ids: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Backfill the outcome half for a task's open episodes, or for exact records.

    The explicit-``record_ids`` escape hatch and absolute ``generations_spent`` are
    kept for operator/replay use. The hook-driven closers above are what production
    calls; prefer them, because they carry the increment and exclusion semantics that
    §3.1 specifies and they commit with the transition that caused them.
    """
    outcome = normalize_outcome(outcome)
    if not is_registered_outcome(outcome):
        raise DecisionRecordError(f"unsupported decision outcome: {outcome}")
    task_id = str(task_id or "").strip().upper()
    wanted = [str(item) for item in (record_ids or []) if str(item or "").strip()]

    def write():
        with _conn(project) as c:
            if wanted:
                placeholders = ",".join("?" for _ in wanted)
                clause = f"record_id IN ({placeholders})"
                params: list[Any] = list(wanted)
            else:
                clause = f"project=? AND task_id=? AND outcome='{OPEN_OUTCOME}'"
                params = [project, task_id]
            cursor = c.execute(
                f"UPDATE decision_records SET outcome=?, merged_sha=?, "
                "head_advanced=?, generations_spent=?, human_intervened=?, "
                f"human_action=? WHERE {clause}",
                (
                    outcome, str(merged_sha or ""), 1 if head_advanced else 0,
                    _int(generations_spent), 1 if human_intervened else 0,
                    str(human_action or ""), *params,
                ),
            )
            return {
                "schema": SCHEMA,
                "project": project,
                "task_id": task_id,
                "outcome": outcome,
                "updated": int(cursor.rowcount or 0),
            }

    return _write_through(project, write)


__all__ = [
    "COUNTS_SCHEMA",
    "DEFAULT_SNAPSHOT_TTL_DAYS",
    "DIAGNOSTIC_FIELDS",
    "EXPORT_COLUMNS",
    "OUTCOMES",
    "OUTCOME_SCHEMA",
    "PRIVATE_COLUMNS",
    "SCHEMA",
    "TERMINAL_OUTCOMES",
    "DecisionRecordError",
    "abandon_open_episodes",
    "abandon_open_episodes_in",
    "close_done_episodes",
    "close_done_episodes_in",
    "close_merged_episodes",
    "close_merged_episodes_in",
    "compact_decision_snapshots",
    "count_reason_code_episodes",
    "episode_hash",
    "export_projection",
    "list_decision_episodes",
    "mark_human_intervention",
    "mark_human_intervention_in",
    "record_decision_episode",
    "record_decision_episode_in",
    "record_episode_outcome",
    "supersede_prior_episodes",
    "supersede_prior_episodes_in",
]
