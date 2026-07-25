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
    FEATURE_FIELDS,
    FEATURES_VERSION,
    project_features,
)
from switchboard.domain.decisions.reason_codes import (
    canonical_reason_code,
    get_reason_code,
    is_registered,
)


SCHEMA = "switchboard.decision_record.v1"
COUNTS_SCHEMA = "switchboard.decision_reason_code_counts.v1"

OUTCOMES = frozenset({"open", "converged", "abandoned", "escalated"})

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


def _row(row: Any) -> Optional[dict[str, Any]]:
    if not row:
        return None
    reason_code = row["reason_code"] or ""
    entry = get_reason_code(reason_code)
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
        "outcome": row["outcome"] or "open",
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
    stamp = float(now if now is not None else time.time())

    existing = c.execute(
        "SELECT * FROM decision_records "
        "WHERE project=? AND task_id=? AND snapshot_hash=?",
        (project, task_id, snapshot_hash),
    ).fetchone()
    if existing:
        c.execute(
            "UPDATE decision_records SET tick_count=tick_count+1, last_seen_at=? "
            "WHERE record_id=?",
            (stamp, existing["record_id"]),
        )
        return _row(c.execute(
            "SELECT * FROM decision_records WHERE record_id=?",
            (existing["record_id"],),
        ).fetchone())

    record_id = "decision-" + uuid.uuid4().hex[:20]
    c.execute(
        "INSERT INTO decision_records("
        "record_id, project, task_id, pr_number, head_sha, generation, fence_epoch, "
        "deliverable_id, host_id, snapshot_hash, snapshot_json, decision_json, "
        "classifier_version, reason_code, route, desired_role, features_json, "
        "features_version, advice_version, tick_count, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
        (
            record_id, project, task_id, pr_number, head_sha, generation,
            fence_epoch, _deliverable_for(c, task_id, project), host_id,
            snapshot_hash, _canonical_json(snap), _canonical_json(verdict),
            str(classifier_version), reason_code, route, desired_role,
            _canonical_json(features), FEATURES_VERSION,
            advice_version or None, stamp, stamp,
        ),
    )
    return _row(c.execute(
        "SELECT * FROM decision_records WHERE record_id=?", (record_id,),
    ).fetchone())


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
    """
    where, params = _window_clause(
        project, since, until, task_id, deliverable_id, host_id)
    with _conn(project) as c:
        rows = c.execute(
            "SELECT reason_code, route, "
            "COUNT(*) AS episodes, "
            "SUM(tick_count) AS ticks, "
            "COUNT(DISTINCT task_id) AS tasks, "
            "MIN(first_seen_at) AS first_seen_at, "
            "MAX(last_seen_at) AS last_seen_at "
            f"FROM decision_records WHERE {where} "
            "GROUP BY reason_code, route "
            "ORDER BY episodes DESC, reason_code ASC",
            tuple(params),
        ).fetchall()

    codes: list[dict[str, Any]] = []
    for row in rows:
        code = row["reason_code"] or ""
        entry = get_reason_code(code)
        codes.append({
            "reason_code": code,
            "route": row["route"] or "",
            "registered": entry is not None,
            "family": entry.family if entry else "",
            "expected": entry.expected if entry else "",
            "resolver": entry.resolver if entry else "",
            "episodes": _int(row["episodes"]),
            "ticks": _int(row["ticks"]),
            "tasks": _int(row["tasks"]),
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
        })

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
        "dominant_reason_code": dominant,
        "unregistered_reason_codes": [
            item["reason_code"] for item in ranked if not item["registered"]
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
    return [{name: row[name] for name in EXPORT_COLUMNS} for row in rows]


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
            cursor = c.execute(
                "UPDATE decision_records SET snapshot_json='{}', snapshot_retained=0 "
                "WHERE project=? AND snapshot_retained=1 AND last_seen_at < ? "
                "AND outcome NOT IN ('escalated','abandoned') "
                "AND human_intervened=0",
                (project, cutoff),
            )
            return {
                "schema": SCHEMA,
                "project": project,
                "cutoff": cutoff,
                "compacted": int(cursor.rowcount or 0),
            }

    return _write_through(project, write)


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
    """Backfill the outcome half for a task's open episodes.

    Called by reconcile/webhook once the world settles. Only the outcome columns
    move; the classified decision and its inputs are never rewritten.
    """
    outcome = str(outcome or "").strip().lower()
    if outcome not in OUTCOMES:
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
                clause = "project=? AND task_id=? AND outcome='open'"
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
    "EXPORT_COLUMNS",
    "OUTCOMES",
    "PRIVATE_COLUMNS",
    "SCHEMA",
    "DecisionRecordError",
    "compact_decision_snapshots",
    "count_reason_code_episodes",
    "episode_hash",
    "export_projection",
    "list_decision_episodes",
    "record_decision_episode",
    "record_decision_episode_in",
    "record_episode_outcome",
]
