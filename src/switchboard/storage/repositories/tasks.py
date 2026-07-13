"""Task persistence repository (ARCH-MS-31).

Owns task CRUD, board rollups/payload, tallies, archive/move helpers, and the
``_task_row`` / dependency helpers previously planned for ``tasks_store.py``.
Cross-cutting enrichment (git provenance, claims, CI, sessions, narration) still
lives on the store facade and is reached via ``_store_facade()`` during the
strangler. ``store.py`` re-exports these symbols; root ``tasks_store.py`` is a
compatibility shim.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

import narration_outbox
from constants import *  # noqa: F401,F403
from db.connection import _conn
from db.core import _insert_row, _json_payload
from switchboard.domain.board.tasks import (
    EDITABLE_TASK_FIELDS,
    READY_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    apply_terminal_done_view as _apply_terminal_done_view,
    block_done_without_provenance,
    build_dependency_state,
    dependency_rows_from_lookup,
    is_terminal_done_task as _is_terminal_done_task,
    normalize_depends_on as _normalize_depends_on,
    rationale_state as _rationale_state,
)
from switchboard.domain.provenance.git import (
    has_done_provenance as _has_done_provenance,
    provenance_summary as _provenance_summary,
)
from switchboard.storage.repositories.access import (
    _task_identity_state_in,
    has_project,
    project_access,
    projects,
)

EDITABLE = list(EDITABLE_TASK_FIELDS)

__all__ = [
    "EDITABLE",
    "TASK_MOVE_TABLES",
    "AUTOINCREMENT_TASK_TABLES",
    "StoreTaskRepository",
    "default_task_repository",
    "_task_row",
    "_dependency_state_in",
    "_approval_payload",
    "_task_human_gate_state",
    "_task_proof_bucket",
    "_merge_task_tally_into_totals",
    "list_tasks",
    "list_tasks_slim",
    "list_tasks_for_board",
    "board_rollups",
    "get_task",
    "_update_task_impl",
    "update_task",
    "_create_task_impl",
    "create_task",
    "_deps_done",
    "_task_tally_snapshot",
    "task_tally",
    "project_tally",
    "delete_task",
    "_rows_for_task",
    "_task_snapshot_in",
    "_active_task_state_in",
    "_insert_archive_in",
    "_delete_task_related_in",
    "_apply_task_id",
    "_missing_dependencies",
    "get_archived_task",
    "archive_task",
    "_is_cleanup_proof_task",
    "move_task",
    "_task_looks_like_code_work",
    "_task_hierarchy_breadcrumb",
    "project_task_stamp",
    "_build_board_payload",
    "board_payload",
]


def _store_facade():
    """Resolve transitional store helpers after store.py is initialized."""
    import store
    return store


TASK_MOVE_TABLES = (
    "activity",
    "task_git_state",
    "task_summaries",
    "task_narrations",
    "pending_narrations",
    "llm_spend",
    "outcomes",
    "task_claims",
    "file_leases",
    "resource_leases",
    "decisions",
)
AUTOINCREMENT_TASK_TABLES = {"activity", "llm_spend", "decisions"}

_BOARD_LITE_DROP = ("session_health", "external_ci", "publication",
                    "entry_criteria", "exit_criteria", "deliverable", "agent_state")

def _task_row(r: sqlite3.Row) -> Dict[str, Any]:
    d = dict(r)
    d["depends_on"] = _normalize_depends_on(d.get("depends_on"))
    d["is_blocking"] = bool(d.get("is_blocking"))
    d["_wsId"] = d.pop("workstream_id")
    d["_wsName"] = d.pop("workstream_name")
    raw_state = d.pop("agent_state", None)
    d["agent_state"] = json.loads(raw_state) if raw_state else {}
    return d

def _dependency_state_in(c: sqlite3.Connection, task: Dict[str, Any]) -> Dict[str, Any]:
    deps = list(dict.fromkeys(task.get("depends_on") or []))
    by_id: Dict[str, Dict[str, Any]] = {}
    if deps:
        placeholders = ",".join("?" for _ in deps)
        rows = c.execute(
            f"SELECT task_id, title, status FROM tasks WHERE task_id IN ({placeholders})",
            deps,
        ).fetchall()
        by_id = {r["task_id"]: {"title": r["title"], "status": r["status"]} for r in rows}
    return build_dependency_state(task, dependency_rows_from_lookup(deps, by_id))

def _approval_payload(task: Dict[str, Any]) -> Dict[str, Any]:
    state = task.get("agent_state") or {}
    if not isinstance(state, dict):
        return {}
    candidates = [
        state.get("human_gate"),
        state.get("approval"),
        (state.get("governance") or {}).get("human_gate")
        if isinstance(state.get("governance"), dict) else None,
        (state.get("governance") or {}).get("approval")
        if isinstance(state.get("governance"), dict) else None,
        (state.get("bug_intake") or {}).get("conversion_gate")
        if isinstance(state.get("bug_intake"), dict) else None,
    ]
    for value in candidates:
        if isinstance(value, dict) and value:
            return value
    return {}

def _task_human_gate_state(task: Dict[str, Any]) -> Dict[str, Any]:
    raw = _approval_payload(task)
    required = bool(raw.get("required") or raw.get("approval_required")
                    or raw.get("needs_human"))
    approved_by = raw.get("approved_by") or raw.get("approver")
    status = str(raw.get("status") or "").strip().lower()
    approved = bool(
        raw.get("approved") is True
        or approved_by
        or status in set(_store_facade().BUG_INTAKE_POLICY["conversion_gate"]["approved_statuses"])
    )
    blocked = bool(required and not approved)
    return {
        "required": required,
        "approved": approved,
        "blocked": blocked,
        "reason": (
            raw.get("reason")
            or raw.get("approval_reason")
            or ("human approval required" if blocked else None)
        ),
        "status": (
            _store_facade().BUG_INTAKE_POLICY["conversion_gate"]["unapproved_status"]
            if blocked else (status or ("approved" if approved else "not_required"))
        ),
        "approved_by": approved_by,
        "approved_at": raw.get("approved_at") or raw.get("accepted_at"),
        "source_bug_task_id": raw.get("source_bug_task_id"),
        "target_workstream": raw.get("target_workstream"),
        "severity": raw.get("severity") or raw.get("severity_hint"),
        "policy": "bug_intake_human_gate.v1",
    }

def _task_proof_bucket(task: Dict[str, Any]) -> str:
    if _is_terminal_done_task(task):
        return "proven"
    status = task.get("status")
    if status == "In Review":
        return "in_review"
    if status == "In Progress":
        return "active"
    return "other"

def _merge_task_tally_into_totals(totals: Dict[str, Any], tally: Dict[str, Any]) -> None:
    spend = tally.get("spend") or {}
    outcomes = tally.get("outcomes") or {}
    verified = int(outcomes.get("verified") or 0)
    proposed = int(outcomes.get("proposed") or 0)
    rejected = int(outcomes.get("rejected") or 0)
    superseded = int(outcomes.get("superseded") or 0)
    cost = float(spend.get("cost_usd") or 0.0)
    kpi_groups = tally.get("kpis") or []
    kpi_contribution = round(sum(float(k.get("verified_contribution") or 0.0)
                                 for k in kpi_groups), 6)
    totals["linked_task_count"] += 1
    _store_facade()._merge_spend_totals(totals["spend"], spend)
    totals["verified_outcomes"] += verified
    totals["proposed_outcomes"] += proposed
    totals["rejected_outcomes"] += rejected
    totals["superseded_outcomes"] += superseded
    totals["verified_kpi_contribution"] = round(
        totals["verified_kpi_contribution"] + kpi_contribution, 6)
    if cost:
        totals["tasks_with_spend"] += 1
    if verified:
        totals["tasks_with_verified_outcomes"] += 1

def list_tasks(workstream: Optional[str] = None, status: Optional[str] = None,
               assignee: Optional[str] = None, project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    q = "SELECT * FROM tasks WHERE 1=1"
    p: List[Any] = []
    if workstream:
        q += " AND workstream_id=?"; p.append(workstream)
    if status:
        q += " AND status=?"; p.append(status)
    if assignee:
        q += " AND assignee=?"; p.append(assignee)
    q += " ORDER BY sort_order"
    with _conn(project) as c:
        tasks = []
        for r in c.execute(q, p).fetchall():
            t = _task_row(r)
            t["provenance"] = _provenance_summary(_store_facade()._load_git_state(c, t["task_id"]))
            t["external_ci"] = _store_facade()._task_external_ci_summary_in(c, t["task_id"], project=project)
            t["publication"] = _store_facade()._task_publication_summary_in(c, t["task_id"])
            t["session_health"] = _store_facade()._task_session_health_in(c, t, project=project)
            tasks.append(t)
        return tasks

def list_tasks_slim(workstream: Optional[str] = None, status: Optional[str] = None,
                    assignee: Optional[str] = None,
                    project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """Slim, filtered task rows with provenance loaded in one batched query.

    This is the card/agent-list path.  It deliberately omits the per-task
    ``external_ci``, ``publication``, and ``session_health`` enrichment reserved
    for task detail.  Optional filters stay in SQL so a lane-scoped MCP search
    does not hydrate or even materialize every task on the board.
    """
    q = "SELECT * FROM tasks WHERE 1=1"
    params: List[Any] = []
    if workstream:
        q += " AND workstream_id=?"; params.append(workstream)
    if status:
        q += " AND status=?"; params.append(status)
    if assignee:
        q += " AND assignee=?"; params.append(assignee)
    q += " ORDER BY sort_order"
    with _conn(project) as c:
        rows = c.execute(q, params).fetchall()
        tasks = [_task_row(r) for r in rows]
        provenance = _store_facade()._provenance_by_task(c, [t["task_id"] for t in tasks])
        for t in tasks:
            t["provenance"] = provenance.get(t["task_id"])
        return tasks

def list_tasks_for_board(project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """Slim, batched task list for the board/kanban and its rollups (HARDEN-34).

    Returns base task rows plus `provenance` (every card's Done-proof badge),
    with provenance loaded in ONE batched query. It deliberately skips the
    per-task external_ci / publication / session_health enrichment that full
    list_tasks() runs — the board never renders those (the task-detail modal
    re-fetches them via get_task). That turns the board's ~4-queries-per-task
    (≈1600 for a 400-task board, ~73s under swap) into 2 queries total.
    """
    return list_tasks_slim(project=project)

def board_rollups(project: str = DEFAULT_PROJECT,
                  tasks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Compute board-level counts from live task rows, not seed metadata."""
    rows = tasks if tasks is not None else list_tasks_for_board(project=project)
    status_counts: Dict[str, int] = {}
    workstream_counts: Dict[str, int] = {}
    effort = 0.0
    for t in rows:
        status = t.get("status") or "Unknown"
        ws_id = t.get("_wsId") or t.get("workstream_id") or "UNKNOWN"
        status_counts[status] = status_counts.get(status, 0) + 1
        workstream_counts[ws_id] = workstream_counts.get(ws_id, 0) + 1
        raw_effort = t.get("effort_days")
        if raw_effort in (None, ""):
            continue
        try:
            effort += float(raw_effort)
        except (TypeError, ValueError):
            continue
    effort_value: Any = int(effort) if effort.is_integer() else round(effort, 2)
    return {
        "total_tasks": len(rows),
        "total_workstreams": len(workstream_counts),
        "total_effort_days": effort_value,
        "status_counts": dict(sorted(status_counts.items())),
        "workstream_counts": dict(sorted(workstream_counts.items())),
    }

def get_task(task_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _conn(project) as c:
        r = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not r:
            # Task ids are user-visible codes whose stored casing can be mixed
            # (CONTRACT-5b), while mission/deliverable callers normalize ids to
            # uppercase. Resolve case-insensitively, then re-read under the
            # canonical id so every sub-query (activity, git_state, claims, ...)
            # matches the stored casing.
            row = c.execute("SELECT task_id FROM tasks WHERE task_id=? COLLATE NOCASE",
                            (task_id,)).fetchone()
            if not row or row["task_id"] == task_id:
                return None
            return get_task(row["task_id"], project=project)
        t = _task_row(r)
        t["activity"] = [dict(a) | {"payload": _json_payload(a["payload"])}
                         for a in c.execute(
                             "SELECT * FROM activity WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]
        t["git_state"] = _store_facade()._load_git_state(c, task_id)
        t["provenance"] = _provenance_summary(t["git_state"])
        t["active_claims"] = _store_facade()._active_task_claims_in(c, task_id)
        t["identity"] = _task_identity_state_in(c, task_id, now)
        t["dependency_state"] = _dependency_state_in(c, t)
        t["human_gate"] = _task_human_gate_state(t)
        t["external_ci"] = _store_facade()._external_ci_review_gate(t, c=c, project=project)
        t["publication"] = _store_facade()._publication_review_gate(t, c=c, project=project)
        t["session_health"] = _store_facade()._task_session_health_in(
            c, t, project=project, active_claims=t["active_claims"], git_state=t["git_state"])
        s = c.execute("SELECT rationale FROM task_summaries WHERE task_id=?", (task_id,)).fetchone()
        if s:
            raw_rationale = s["rationale"]
            rationale_state = _rationale_state(raw_rationale, t, t["dependency_state"])
            t["rationale_state"] = rationale_state
            if rationale_state["stale"]:
                t["rationale_raw"] = raw_rationale
                t["rationale"] = None
            else:
                t["rationale"] = raw_rationale
        n = c.execute(
            "SELECT narration, source_fingerprint, generated_at FROM task_narrations "
            "WHERE task_id=?", (task_id,)).fetchone()
        if n:
            narration_state = _store_facade()._narration_state(dict(n), t)
            t["narration_state"] = narration_state
            if narration_state["stale"]:
                t["narration_raw"] = n["narration"]
                t["narration"] = None
            else:
                t["narration"] = n["narration"]
        _apply_terminal_done_view(t)
        _store_facade()._enrich_task_project_context(t, project=project)
        return t

def _update_task_impl(task_id: str, fields: Dict[str, Any], actor: str = "user",
                      project: str = DEFAULT_PROJECT) -> Any:
    sets, vals, changed = [], [], {}
    for k, v in fields.items():
        if k not in EDITABLE:
            continue
        if k == "is_blocking":
            v = 1 if v else 0
        if k == "depends_on":
            v = _normalize_depends_on(v)
            sets.append(f"{k}=?"); vals.append(json.dumps(v)); changed[k] = v
            continue
        sets.append(f"{k}=?"); vals.append(v); changed[k] = v
    if not sets:
        return task_id
    if str(changed.get("status") or "").strip().lower() == "done":
        now = time.time()
        with _conn(project) as c:
            row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return None
            git_state = _store_facade()._load_git_state(c, task_id)
            if not _has_done_provenance(git_state):
                payload = block_done_without_provenance()
                c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (task_id, actor, "task.done_blocked",
                           json.dumps(payload, sort_keys=True), now))
                task = _task_row(row)
                task["git_state"] = git_state
                task["error"] = "done_requires_merge_provenance"
                task["message"] = payload["message"]
                return task
    sets.append("updated_at=?"); vals.append(time.time())
    vals.append(task_id)
    with _conn(project) as c:
        cur = c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?", vals)
        if cur.rowcount == 0:
            return None
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "edit", json.dumps(changed), time.time()))
        # NARRATE-8: emit the narration intent inside the SAME transaction as the mutation
        # (ADR-0008). Materiality is decided by the source projection hash, so a cosmetic
        # edit bumps no revision and emits nothing. Commit binds mutation + intent atomically.
        emitted = narration_outbox.emit_task_narration_request(
            c, task_id, project=project, cause_kind="task.updated", actor=actor)
    # NARRATE-9: wake a worker only after the emit transaction has committed (boundary 1 → wake).
    # Best-effort acceleration; durable outbox state is the source of truth, so a lost wake is
    # recovered by the sweep. No-op until the daemon registers a sink.
    if emitted:
        narration_outbox.request_wake(project, entity_type="task", entity_id=task_id)
    # NARRATE-2: enqueue CEO-narration only on a real status transition, never on cosmetic
    # edits — this is the cost guarantee. The drain job applies the trigger-status filter.
    # Kept as a post-commit shadow marker alongside the outbox until NARRATE-14 cuts over.
    return {"task_id": task_id, "changed": changed, "emitted": bool(emitted)}

def update_task(task_id: str, fields: Dict[str, Any], actor: str = "user",
                project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    s = _store_facade()
    result = s._write_through(project,
        lambda: s._update_task_impl(task_id, fields, actor=actor, project=project))
    if result is None or (isinstance(result, dict) and result.get("error")):
        return result
    changed = result.get("changed", {}) if isinstance(result, dict) else {}
    if "status" in changed:
        s.enqueue_narration(task_id, status=str(changed.get("status") or ""),
                          reason="status_change", project=project)
    # NARRATE-11: when the task's narration source actually moved, invalidate ONLY the
    # deliverables directly linked to it whose narrative inputs changed (bounded, no full scan).
    # Runs fully post-commit on the caller thread so it never nests inside the task writer.
    if isinstance(result, dict) and result.get("emitted"):
        narration_outbox.invalidate_linked_deliverables(task_id, project, actor=actor)
    # Via facade so tests / callers that monkeypatch store.get_task are honored.
    return s.get_task(task_id, project)

def _create_task_impl(data: Dict[str, Any], actor: str = "user",
                      project: str = DEFAULT_PROJECT) -> Optional[str]:
    ws = (data.get("workstream_id") or "").strip()
    title = (data.get("title") or "").strip()
    if not ws or not title:
        return None
    with _conn(project) as c:
        wsname = data.get("workstream_name")
        if not wsname:
            r = c.execute("SELECT workstream_name FROM tasks WHERE workstream_id=? LIMIT 1", (ws,)).fetchone()
            wsname = r[0] if r else ws
        ids = [row[0] for row in c.execute("SELECT task_id FROM tasks WHERE workstream_id=?", (ws,)).fetchall()]
        mx = 0
        for t in ids:
            tail = t.rsplit("-", 1)[-1]
            if tail.isdigit():
                mx = max(mx, int(tail))
        tid = f"{ws}-{mx + 1}"
        while c.execute("SELECT 1 FROM tasks WHERE task_id=?", (tid,)).fetchone():
            mx += 1
            tid = f"{ws}-{mx + 1}"
        order = c.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM tasks").fetchone()[0]
        now = time.time()
        c.execute(
            """INSERT INTO tasks (task_id, workstream_id, workstream_name, title, description,
                 owner_org, owner_person_or_role, assignee, phase, status, effort_days, duration_days,
                 start_date, finish_date, start_day, depends_on, entry_criteria, exit_criteria,
                 deliverable, risk_level, is_blocking, sort_order, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, ws, wsname, title, data.get("description"), data.get("owner_org"),
             data.get("owner_person_or_role"), data.get("assignee"), (data.get("phase") or "Build"),
             (data.get("status") or "Not Started"), data.get("effort_days"), data.get("duration_days"),
             data.get("start_date"), data.get("finish_date"), 0,
             json.dumps(_normalize_depends_on(data.get("depends_on"))),
             data.get("entry_criteria"), data.get("exit_criteria"),
             data.get("deliverable"), (data.get("risk_level") or "Medium"),
             1 if data.get("is_blocking") else 0, order, now, now))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (tid, actor, "create", json.dumps({"title": title}), now))
        # NARRATE-8: a new task is revision 1 — emit its narration intent atomically with
        # the insert (ADR-0008). Rollback of the create drops the outbox row too.
        emitted = narration_outbox.emit_task_narration_request(
            c, tid, project=project, cause_kind="task.created", actor=actor)
    # NARRATE-9: post-commit best-effort wake (see update path).
    if emitted:
        narration_outbox.request_wake(project, entity_type="task", entity_id=tid)
    return tid

def create_task(data: Dict[str, Any], actor: str = "user",
                project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    s = _store_facade()
    tid = s._write_through(project,
        lambda: s._create_task_impl(data, actor=actor, project=project))
    if not tid:
        return None
    # NARRATE-2: a newly created task is a meaningful transition — enqueue its first narration.
    s.enqueue_narration(tid, status=(data.get("status") or "Not Started"),
                      reason="create", project=project)
    # NARRATE-11: bounded fan-out to any deliverables this new task is already linked to
    # (usually none at creation) — post-commit, idempotent, no full-project scan.
    narration_outbox.invalidate_linked_deliverables(tid, project, actor=actor)
    # Via facade so tests / callers that monkeypatch store.get_task are honored.
    return s.get_task(tid, project)

def _deps_done(task: Dict[str, Any], by_id: Dict[str, Dict[str, Any]]) -> bool:
    for dep in task.get("depends_on") or []:
        if by_id.get(dep, {}).get("status") != "Done":
            return False
    return True


def _task_tally_snapshot(c: sqlite3.Connection, task_id: str) -> Dict[str, Any]:
    outcomes = [_store_facade()._outcome_row(r) for r in c.execute(
        "SELECT * FROM outcomes WHERE task_id=?", (task_id,)).fetchall()]
    return {"spend": _store_facade()._spend_summary(_store_facade()._spend_for_task(c, task_id, outcomes)),
            "outcomes": outcomes}

def task_tally(task_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    with _conn(project) as c:
        outcome_rows = c.execute("SELECT * FROM outcomes WHERE task_id=? ORDER BY created_at",
                                 (task_id,)).fetchall()
        outcomes = [_store_facade()._outcome_row(r) for r in outcome_rows]
        rows = _store_facade()._spend_for_task(c, task_id, outcomes)
        links: List[Dict[str, Any]] = []
        if outcomes:
            outcome_ids = [o["id"] for o in outcomes]
            link_rows = c.execute(
                "SELECT l.*, k.name, k.unit, k.direction FROM outcome_kpi_links l "
                "JOIN kpis k ON k.id=l.kpi_id WHERE l.outcome_id IN (%s)"
                % ",".join("?" for _ in outcome_ids), outcome_ids).fetchall()
            links = [dict(r) for r in link_rows]
    spend = _store_facade()._spend_summary(rows)
    outcome_counts = {"verified": 0, "proposed": 0, "rejected": 0, "superseded": 0}
    by_outcome = {o["id"]: o for o in outcomes}
    for outcome in outcomes:
        outcome_counts[outcome["status"]] = outcome_counts.get(outcome["status"], 0) + 1
    verified_count = outcome_counts.get("verified", 0)
    cost_per_outcome = (round(spend["cost_usd"] / verified_count, 6)
                        if verified_count else None)
    kpi_groups: Dict[str, Dict[str, Any]] = {}
    for link in links:
        outcome = by_outcome.get(link["outcome_id"]) or {}
        group = kpi_groups.setdefault(link["kpi_id"], {
            "kpi_id": link["kpi_id"],
            "name": link["name"],
            "unit": link["unit"],
            "direction": link["direction"],
            "verified_contribution": 0.0,
            "links": [],
            "cost_per_contribution_unit": None,
        })
        link_payload = {k: link.get(k) for k in ("id", "outcome_id", "contribution",
                                                 "contribution_unit", "confidence", "rationale")}
        link_payload["outcome_status"] = outcome.get("status")
        group["links"].append(link_payload)
        if outcome.get("status") == "verified" and link.get("contribution") is not None:
            group["verified_contribution"] += float(link["contribution"] or 0.0)
    for group in kpi_groups.values():
        if group["verified_contribution"]:
            group["cost_per_contribution_unit"] = round(
                spend["cost_usd"] / group["verified_contribution"], 6)
    return {"task_id": task_id, "spend": spend,
            "unit_cost": {"cost_per_verified_outcome": cost_per_outcome},
            "outcomes": outcome_counts,
            "outcome_records": outcomes,
            "kpis": list(kpi_groups.values())}

def project_tally(project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Project-level economic surface for TALLY-3.

    This intentionally derives from task_tally/kpi_tally so the board UI and API present the
    same semantics as the lower-level OXP/Tally primitives: verified outcomes are the denominator,
    proposed outcomes stay visible but do not count, and spend remains separated by source.
    """
    tasks = list_tasks(project=project)
    totals = {
        "task_count": len(tasks),
        "tasks_with_spend": 0,
        "tasks_with_verified_outcomes": 0,
        "verified_outcomes": 0,
        "proposed_outcomes": 0,
        "rejected_outcomes": 0,
        "superseded_outcomes": 0,
        "verified_kpi_contribution": 0.0,
        "spend": {"cost_usd": 0.0, "total_tokens": 0, "by_source": {}, "by_model": {}},
        "unit_cost": {
            "cost_per_verified_outcome": None,
            "cost_per_kpi_contribution_unit": None,
        },
    }
    by_workstream: Dict[str, Dict[str, Any]] = {}
    by_task: List[Dict[str, Any]] = []

    for task in tasks:
        tid = task["task_id"]
        tally = task_tally(tid, project=project)
        spend = tally.get("spend") or {}
        outcomes = tally.get("outcomes") or {}
        verified = int(outcomes.get("verified") or 0)
        proposed = int(outcomes.get("proposed") or 0)
        rejected = int(outcomes.get("rejected") or 0)
        superseded = int(outcomes.get("superseded") or 0)
        cost = float(spend.get("cost_usd") or 0.0)
        tokens = int(spend.get("total_tokens") or 0)
        kpi_groups = tally.get("kpis") or []
        kpi_contribution = round(sum(float(k.get("verified_contribution") or 0.0)
                                     for k in kpi_groups), 6)
        _store_facade()._merge_spend_totals(totals["spend"], spend)
        totals["verified_outcomes"] += verified
        totals["proposed_outcomes"] += proposed
        totals["rejected_outcomes"] += rejected
        totals["superseded_outcomes"] += superseded
        totals["verified_kpi_contribution"] = round(
            totals["verified_kpi_contribution"] + kpi_contribution, 6)
        if cost:
            totals["tasks_with_spend"] += 1
        if verified:
            totals["tasks_with_verified_outcomes"] += 1

        ws_id = task.get("_wsId") or task.get("workstream_id") or "UNKNOWN"
        ws = by_workstream.setdefault(ws_id, {
            "workstream_id": ws_id,
            "name": task.get("_wsName") or task.get("workstream_name") or ws_id,
            "task_count": 0,
            "tasks_with_spend": 0,
            "verified_outcomes": 0,
            "proposed_outcomes": 0,
            "verified_kpi_contribution": 0.0,
            "spend": {"cost_usd": 0.0, "total_tokens": 0, "by_source": {}, "by_model": {}},
            "unit_cost": {"cost_per_verified_outcome": None},
        })
        ws["task_count"] += 1
        if cost:
            ws["tasks_with_spend"] += 1
        ws["verified_outcomes"] += verified
        ws["proposed_outcomes"] += proposed
        ws["verified_kpi_contribution"] = round(ws["verified_kpi_contribution"] + kpi_contribution, 6)
        _store_facade()._merge_spend_totals(ws["spend"], spend)

        if cost or tokens or verified or proposed or rejected or superseded or kpi_groups:
            by_task.append({
                "task_id": tid,
                "title": task.get("title"),
                "workstream_id": ws_id,
                "workstream_name": task.get("_wsName") or task.get("workstream_name"),
                "status": task.get("status"),
                "spend": spend,
                "outcomes": outcomes,
                "unit_cost": tally.get("unit_cost") or {},
                "verified_kpi_contribution": kpi_contribution,
                "kpis": kpi_groups,
            })

    if totals["verified_outcomes"]:
        totals["unit_cost"]["cost_per_verified_outcome"] = round(
            totals["spend"]["cost_usd"] / totals["verified_outcomes"], 6)
    if totals["verified_kpi_contribution"]:
        totals["unit_cost"]["cost_per_kpi_contribution_unit"] = round(
            totals["spend"]["cost_usd"] / totals["verified_kpi_contribution"], 6)
    for ws in by_workstream.values():
        if ws["verified_outcomes"]:
            ws["unit_cost"]["cost_per_verified_outcome"] = round(
                ws["spend"]["cost_usd"] / ws["verified_outcomes"], 6)

    with _conn(project) as c:
        kpi_ids = [r["id"] for r in c.execute("SELECT id FROM kpis ORDER BY name").fetchall()]
    kpis = []
    for kpi_id in kpi_ids:
        kt = _store_facade().kpi_tally(kpi_id, project=project)
        kpis.append({
            "kpi": kt.get("kpi"),
            "spend": kt.get("spend"),
            "outcomes": kt.get("outcomes"),
            "verified_contribution": kt.get("verified_contribution"),
            "unit_cost": kt.get("unit_cost"),
        })

    return {
        "project": project,
        "totals": totals,
        "by_workstream": sorted(by_workstream.values(),
                                key=lambda x: (-float(x["spend"]["cost_usd"] or 0.0),
                                               x["workstream_id"])),
        "by_task": sorted(by_task, key=lambda x: (-float(x["spend"]["cost_usd"] or 0.0),
                                                  x["task_id"])),
        "kpis": kpis,
    }


def delete_task(task_id: str, project: str = DEFAULT_PROJECT) -> bool:
    with _conn(project) as c:
        cur = c.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        c.execute("DELETE FROM activity WHERE task_id=?", (task_id,))
        return cur.rowcount > 0

def _rows_for_task(c: sqlite3.Connection, table: str, task_id: str) -> List[Dict[str, Any]]:
    return [dict(r) for r in c.execute(f"SELECT * FROM {table} WHERE task_id=?",
                                       (task_id,)).fetchall()]

def _task_snapshot_in(c: sqlite3.Connection, task_id: str) -> Optional[Dict[str, Any]]:
    task = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not task:
        return None
    snapshot: Dict[str, Any] = {"task": dict(task)}
    for table in TASK_MOVE_TABLES:
        snapshot[table] = _rows_for_task(c, table, task_id)
    outcome_ids = [r["id"] for r in snapshot.get("outcomes", [])]
    if outcome_ids:
        placeholders = ",".join("?" for _ in outcome_ids)
        snapshot["outcome_kpi_links"] = [
            dict(r) for r in c.execute(
                f"SELECT * FROM outcome_kpi_links WHERE outcome_id IN ({placeholders})",
                outcome_ids,
            ).fetchall()
        ]
    else:
        snapshot["outcome_kpi_links"] = []
    kpi_ids = sorted({r["kpi_id"] for r in snapshot.get("outcome_kpi_links", [])
                      if r.get("kpi_id")})
    if kpi_ids:
        placeholders = ",".join("?" for _ in kpi_ids)
        snapshot["kpis"] = [
            dict(r) for r in c.execute(
                f"SELECT * FROM kpis WHERE id IN ({placeholders})", kpi_ids,
            ).fetchall()
        ]
    else:
        snapshot["kpis"] = []
    snapshot["agent_messages"] = _rows_for_task(c, "agent_messages", task_id)
    snapshot["coordination_monitors"] = _rows_for_task(c, "coordination_monitors", task_id)
    return snapshot

def _active_task_state_in(c: sqlite3.Connection, task_id: str, now: float) -> Dict[str, Any]:
    active_claims = [dict(r) for r in c.execute(
        "SELECT id, agent_id, expires_at FROM task_claims "
        "WHERE task_id=? AND status='active' AND expires_at>?",
        (task_id, now),
    ).fetchall()]
    active_resource_leases = [dict(r) for r in c.execute(
        "SELECT id, agent_id, resource_type, names, claimed_at, ttl_seconds FROM resource_leases "
        "WHERE task_id=? AND released_at IS NULL AND claimed_at + ttl_seconds > ?",
        (task_id, now),
    ).fetchall()]
    active_file_leases = [dict(r) for r in c.execute(
        "SELECT id, agent_id, files, claimed_at, ttl_minutes FROM file_leases "
        "WHERE task_id=? AND released_at IS NULL AND claimed_at + (ttl_minutes * 60) > ?",
        (task_id, now),
    ).fetchall()]
    return {"claims": active_claims, "resource_leases": active_resource_leases,
            "file_leases": active_file_leases}

def _insert_archive_in(c: sqlite3.Connection, task_id: str, operation: str, actor: str,
                       reason: str, source_project: str, destination_project: str,
                       snapshot: Dict[str, Any], now: float) -> str:
    archive_id = "archive-" + uuid.uuid4().hex[:16]
    c.execute(
        "INSERT INTO archived_tasks(archive_id, task_id, operation, actor, reason, "
        "source_project, destination_project, snapshot_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (archive_id, task_id, operation, actor, reason or None, source_project,
         destination_project or None, json.dumps(snapshot, sort_keys=True), now),
    )
    return archive_id

def _delete_task_related_in(c: sqlite3.Connection, task_id: str, snapshot: Dict[str, Any]) -> None:
    outcome_ids = [r["id"] for r in snapshot.get("outcomes", [])]
    if outcome_ids:
        placeholders = ",".join("?" for _ in outcome_ids)
        c.execute(f"DELETE FROM outcome_kpi_links WHERE outcome_id IN ({placeholders})",
                  outcome_ids)
    for table in (
        "activity",
        "task_git_state",
        "task_summaries",
        "task_narrations",
        "pending_narrations",
        "llm_spend",
        "outcomes",
        "task_claims",
        "file_leases",
        "resource_leases",
        "decisions",
        "agent_messages",
        "coordination_monitors",
    ):
        c.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
    c.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))

def _apply_task_id(row: Dict[str, Any], old_task_id: str, new_task_id: str) -> Dict[str, Any]:
    out = dict(row)
    if out.get("task_id") == old_task_id:
        out["task_id"] = new_task_id
    return out

def _missing_dependencies(depends_on: List[str], project: str) -> List[str]:
    return [dep for dep in depends_on if not get_task(dep, project=project)]

def get_archived_task(archive_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    with _conn(project) as c:
        row = c.execute("SELECT * FROM archived_tasks WHERE archive_id=?",
                        (archive_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["snapshot"] = json.loads(out.pop("snapshot_json") or "{}")
        return out

def archive_task(task_id: str, reason: str = "", actor: str = "system",
                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    if not has_project(project):
        return {"error": f"unknown project: {project}", "project": project}
    now = time.time()
    with _conn(project) as c:
        snapshot = _task_snapshot_in(c, task_id)
        if not snapshot:
            return {"error": "task not found", "task_id": task_id, "project": project}
        active = _active_task_state_in(c, task_id, now)
        if active["claims"] or active["resource_leases"] or active["file_leases"]:
            return {"error": "task has active claims or leases", "task_id": task_id,
                    "project": project, "active": active}
        archive_id = _insert_archive_in(
            c, task_id, "archive", actor, reason, project, "", snapshot, now)
        _delete_task_related_in(c, task_id, snapshot)
    return {"archived": True, "archive_id": archive_id, "task_id": task_id,
            "project": project, "reason": reason or None}

def _is_cleanup_proof_task(task: Dict[str, Any]) -> bool:
    task_id = (task.get("task_id") or "").upper()
    ws = (task.get("workstream_id") or "").upper()
    title = (task.get("title") or "").lower()
    return (
        task_id.startswith("PROOF-")
        or ws in {"PROOF", "SENTINEL"}
        or "proof" in title
        or "sentinel" in title
    )

def move_task(task_id: str, project_from: str, project_to: str, reason: str = "",
              actor: str = "system", new_task_id: str = "",
              dependency_policy: str = "fail") -> Dict[str, Any]:
    if not has_project(project_from):
        return {"error": f"unknown source project: {project_from}", "project": project_from}
    if not has_project(project_to):
        return {"error": f"unknown destination project: {project_to}", "project": project_to}
    if project_from == project_to:
        return {"error": "source and destination projects must differ",
                "project": project_from, "task_id": task_id}
    now = time.time()
    new_task_id = (new_task_id or task_id).strip()
    dependency_policy = (dependency_policy or "fail").strip().lower()
    if dependency_policy not in {"fail", "clear"}:
        return {"error": "dependency_policy must be 'fail' or 'clear'",
                "dependency_policy": dependency_policy}

    with _conn(project_from) as source:
        snapshot = _task_snapshot_in(source, task_id)
        if not snapshot:
            return {"error": "task not found", "task_id": task_id,
                    "project": project_from}
        active = _active_task_state_in(source, task_id, now)
        if active["claims"] or active["resource_leases"] or active["file_leases"]:
            return {"error": "task has active claims or leases", "task_id": task_id,
                    "project": project_from, "active": active}

    task_row = dict(snapshot["task"])
    depends_on = json.loads(task_row.get("depends_on") or "[]")
    missing_deps = _missing_dependencies(depends_on, project_to)
    cleared_deps: List[str] = []
    if missing_deps:
        if dependency_policy == "fail":
            return {"error": "destination is missing dependency id(s)",
                    "task_id": task_id, "project_from": project_from,
                    "project_to": project_to, "missing_dependencies": missing_deps,
                    "hint": "create dependencies first or pass dependency_policy='clear'"}
        cleared_deps = missing_deps
        depends_on = [dep for dep in depends_on if dep not in set(missing_deps)]

    try:
        with _conn(project_to) as dest:
            if dest.execute("SELECT 1 FROM tasks WHERE task_id=?",
                            (new_task_id,)).fetchone():
                return {"error": "destination task id already exists",
                        "task_id": new_task_id, "project_to": project_to}
            outcome_ids = [r["id"] for r in snapshot.get("outcomes", [])]
            if outcome_ids:
                placeholders = ",".join("?" for _ in outcome_ids)
                conflicts = [r["id"] for r in dest.execute(
                    f"SELECT id FROM outcomes WHERE id IN ({placeholders})",
                    outcome_ids,
                ).fetchall()]
                if conflicts:
                    return {"error": "destination outcome id conflict",
                            "project_to": project_to, "outcome_ids": conflicts}
            moved_task = _apply_task_id(task_row, task_id, new_task_id)
            moved_task["depends_on"] = json.dumps(depends_on)
            moved_task["updated_at"] = now
            _insert_row(dest, "tasks", moved_task)
            for table in TASK_MOVE_TABLES:
                skip = {"id"} if table in AUTOINCREMENT_TASK_TABLES else set()
                for row in snapshot.get(table, []):
                    moved_row = _apply_task_id(row, task_id, new_task_id)
                    if table == "outcomes":
                        moved_row["project"] = project_to
                    _insert_row(dest, table, moved_row, skip_columns=skip)
            for row in snapshot.get("kpis", []):
                if dest.execute("SELECT 1 FROM kpis WHERE id=?", (row["id"],)).fetchone():
                    continue
                moved_kpi = dict(row)
                moved_kpi["project"] = project_to
                _insert_row(dest, "kpis", moved_kpi)
            for row in snapshot.get("outcome_kpi_links", []):
                moved_link = dict(row)
                moved_link["project"] = project_to
                _insert_row(dest, "outcome_kpi_links", moved_link)
            dest.execute(
                "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (new_task_id, actor, "task.moved_in", json.dumps({
                    "from_project": project_from,
                    "original_task_id": task_id,
                    "task_id": new_task_id,
                    "reason": reason or None,
                    "cleared_dependencies": cleared_deps,
                }, sort_keys=True), now),
            )
    except sqlite3.IntegrityError as e:
        return {"error": "destination insert failed", "detail": str(e),
                "task_id": task_id, "project_to": project_to}

    with _conn(project_from) as source:
        source_snapshot = _task_snapshot_in(source, task_id)
        if not source_snapshot:
            return {"moved": True, "warning": "source task already absent after destination copy",
                    "task_id": task_id, "new_task_id": new_task_id,
                    "project_from": project_from, "project_to": project_to}
        archive_id = _insert_archive_in(
            source, task_id, "move_out", actor, reason, project_from,
            project_to, source_snapshot, now)
        _delete_task_related_in(source, task_id, source_snapshot)

    return {"moved": True, "archive_id": archive_id, "task_id": task_id,
            "new_task_id": new_task_id, "project_from": project_from,
            "project_to": project_to, "cleared_dependencies": cleared_deps}

def _task_looks_like_code_work(task: Dict[str, Any]) -> bool:
    text = _store_facade()._session_profile_text(task).lower()
    if re.search(r"(?:policy_profile|session_profile)\s*[:=]", text):
        return False
    if re.search(r"\b(non[- ]code|offline evidence|docs[- ]only|review[- ]only)\b", text):
        return False
    code_terms = (
        "code", "repo", "branch", "worktree", "clone", "git ", "github", "pr ",
        "pull request", "merge", "rebase", "commit", "ci", "tests", "test suite",
        "deploy", "server", "api", "mcp", "rest", "ui", "runtime", "adapter",
    )
    return any(term in text for term in code_terms)

def _task_hierarchy_breadcrumb(task: Dict[str, Any], project: str,
                               links: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    breadcrumb = [
        {"level": "project", "id": project},
        {"level": "workstream", "id": task.get("_wsId"), "label": task.get("_wsName")},
        {"level": "task", "id": task.get("task_id"), "title": task.get("title")},
    ]
    if links is None:
        links = list_task_deliverable_links(task.get("task_id") or "", project=project)
    if links:
        first = links[0]
        board = first.get("board") or {}
        breadcrumb.insert(1, {
            "level": board.get("kind") or "mission",
            "id": first.get("board_id"),
            "title": (board.get("title") if isinstance(board, dict) else None) or first.get("board_id"),
            "deliverable_id": first.get("deliverable_id"),
            "deliverable_title": first.get("deliverable_title"),
        })
    return breadcrumb

def project_task_stamp(project: str) -> Any:
    """Cheap cache stamp for a project: (row count, latest task mutation).

    Any create/edit/claim/complete on a task bumps tasks.updated_at, so MAX(updated_at)
    catches inserts and edits — but NOT a deletion: removing a non-newest task leaves
    the max untouched, so a MAX-only stamp would keep serving the deleted card from
    the board/signals caches for up to the TTL (HARDEN-41). Folding in COUNT(*) makes
    the stamp move on deletes too, so a removed task drops out the instant it's gone.
    Missing/unopenable projects stamp as 0.
    """
    if not has_project(project):
        return 0
    with _conn(project) as c:
        count, latest = c.execute(
            "SELECT COUNT(*), COALESCE(MAX(updated_at), 0) FROM tasks").fetchone()
        return (count, latest or 0)

def _build_board_payload(project: str, lite: bool) -> Dict[str, Any]:
    # The lite path uses the batched, enrichment-free loader (HARDEN-34); rollups
    # read only base fields (status/workstream/effort), so slim rows are enough.
    tasks = list_tasks_for_board(project) if lite else list_tasks(project=project)
    payload: Dict[str, Any] = {k: _store_facade().get_meta(k, project=project) for k in META_SECTIONS}
    payload["project"] = next((p for p in projects() if p["id"] == project), {
        "id": project,
        "label": project,
        "pretitle": "",
        "purpose": project_access(project).get("purpose") or "",
        "boundary": project_access(project).get("boundary") or "",
    })
    payload["rollups"] = board_rollups(project=project, tasks=tasks)
    ws_tasks = tasks
    if lite:
        ws_tasks = [{k: v for k, v in t.items() if k not in _BOARD_LITE_DROP} for t in tasks]
    by_ws: Dict[str, Dict[str, Any]] = {}
    for t in ws_tasks:
        ws = by_ws.setdefault(t["_wsId"], {"workstream_id": t["_wsId"], "name": t["_wsName"], "tasks": []})
        ws["tasks"].append(t)
    payload["workstreams"] = list(by_ws.values())
    # HARDEN-35: project_context (repo roles, hierarchy, policy profiles) used to
    # ride on every board load — a near-static ~9KB blob the board never renders.
    # It now has its own endpoint (GET /api/projects/{id}/context) that the UI
    # fetches once and the browser caches; keep it out of the board payload.
    return payload

def board_payload(project: str = DEFAULT_PROJECT, lite: bool = False) -> Dict[str, Any]:
    s = _store_facade()
    if not lite:
        # Via facade so tests that monkeypatch store._build_board_payload are honored.
        return s._build_board_payload(project, lite=False)
    try:
        stamp = project_task_stamp(project)
    except Exception:
        return s._build_board_payload(project, lite=True)
    return s.ttl_read_cache("board", project, stamp,
                          lambda: s._build_board_payload(project, lite=True))


class StoreTaskRepository:
    """SQL-backed :class:`~switchboard.storage.repositories.protocols.TaskRepository`."""

    def get_task(self, task_id: str, project: str = DEFAULT_PROJECT) -> Optional[dict[str, Any]]:
        return get_task(task_id, project=project)

    def create_task(
            self,
            data: dict[str, Any],
            actor: str = "user",
            project: str = DEFAULT_PROJECT) -> Optional[dict[str, Any]]:
        return create_task(data, actor=actor, project=project)

    def update_task(
            self,
            task_id: str,
            fields: dict[str, Any],
            actor: str = "user",
            project: str = DEFAULT_PROJECT) -> Optional[dict[str, Any]]:
        return update_task(task_id, fields, actor=actor, project=project)


def default_task_repository() -> StoreTaskRepository:
    """Canonical Phase-1B task repository (SQL in this module)."""
    return StoreTaskRepository()
