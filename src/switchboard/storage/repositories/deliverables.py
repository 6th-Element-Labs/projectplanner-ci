"""Deliverables / mission repository (ARCH-MS-35).

Owns project boards, deliverable CRUD/links/milestones, breakdown proposals,
closure/outcomes, mission status/brief/coordinator, and deliverable tallies
previously living in ``store.py``. Cross-cutting store helpers (write queue,
idempotency, git/CI snapshots, activity) are reached via ``_store_facade()``
during the strangler. ``store.py`` re-exports these symbols; root
``deliverables_store.py`` is a compatibility shim.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import deliverable_gates
import deliverable_policy
import evidence_claims
import narration_outbox
from constants import *  # noqa: F401,F403
from db.connection import _conn
from read_cache import _READ_CACHE, ttl_read_cache  # noqa: F401
from switchboard.domain.deliverables.lifecycle import (
    BREAKDOWN_PROPOSAL_STATUSES,
    DELIVERABLE_ID_RE,
    DELIVERABLE_MILESTONE_STATUSES,
    DELIVERABLE_STATUSES,
    PROJECT_BOARD_ID_RE,
    PROJECT_BOARD_KINDS,
    PROJECT_BOARD_STATUSES,
    normalize_deliverable_id,
    normalize_project_board_id,
    validate_deliverable_status,
)
from switchboard.storage.repositories.tasks import _task_row


def _store_facade():
    """Resolve transitional store helpers after store.py is initialized."""
    import store
    return store


def _deliverable_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for key in (
        "acceptance_criteria_json",
        "policy_constraints_json",
        "proof_requirements_json",
        "kpi_links_json",
        "metadata_json",
    ):
        out_key = key[:-5] if key.endswith("_json") else key
        d[out_key] = _store_facade()._json_payload(d.pop(key, ""))
    d["mission_id"] = d.get("board_id") or None
    return d


def _project_board_row(row: sqlite3.Row, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    d = dict(row)
    d["project_id"] = project
    d["mission_id"] = d.get("id")
    d["metadata"] = _store_facade()._json_payload(d.pop("metadata_json", ""))
    return d


def _deliverable_milestone_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for key in ("acceptance_criteria_json", "proof_requirements_json"):
        d[key[:-5]] = _store_facade()._json_payload(d.pop(key, ""))
    return d


def _deliverable_link_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["mission_id"] = d.get("board_id") or None
    d["blocks_deliverable"] = bool(d.get("blocks_deliverable"))
    d["proof_required"] = _store_facade()._json_payload(d.pop("proof_required_json", ""))
    d["metadata"] = _store_facade()._json_payload(d.pop("metadata_json", ""))
    return d


def _project_board_exists_in(c: sqlite3.Connection, board_id: str) -> bool:
    return bool(c.execute("SELECT 1 FROM project_boards WHERE id=?",
                          (board_id,)).fetchone())


def create_project_board(data: Dict[str, Any], actor: str = "user",
                         project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Create or update a first-class Board/Mission child under a Project."""
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    title = (data.get("title") or "").strip()
    if not title:
        return {"error": "title required"}
    try:
        board_id = normalize_project_board_id(
            data.get("id") or data.get("board_id") or data.get("mission_id"), title)
    except ValueError as exc:
        return {"error": str(exc)}
    kind = (data.get("kind") or "mission").strip().lower()
    if kind not in PROJECT_BOARD_KINDS:
        return {"error": "invalid board kind", "allowed": sorted(PROJECT_BOARD_KINDS)}
    status = (data.get("status") or "active").strip().lower()
    if status not in PROJECT_BOARD_STATUSES:
        return {"error": "invalid board status", "allowed": sorted(PROJECT_BOARD_STATUSES)}
    now = time.time()
    with _store_facade()._conn(project) as c:
        c.execute(
            """INSERT INTO project_boards
               (id, title, kind, status, owner_org, owner_person_or_role, purpose,
                end_state, description, metadata_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                kind=excluded.kind,
                status=excluded.status,
                owner_org=excluded.owner_org,
                owner_person_or_role=excluded.owner_person_or_role,
                purpose=excluded.purpose,
                end_state=excluded.end_state,
                description=excluded.description,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at""",
            (
                board_id, title, kind, status, data.get("owner_org"),
                data.get("owner_person_or_role"), data.get("purpose"),
                data.get("end_state"), data.get("description"),
                _store_facade()._json_object_field(data.get("metadata", data.get("metadata_json"))),
                now, now,
            ),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "project_board.upsert",
                   json.dumps({"project_id": project, "board_id": board_id, "kind": kind,
                               "title": title}, sort_keys=True), now))
    return get_project_board(board_id, project=project) or {"error": "board not found"}


def get_project_board(board_id: str, project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    if not _store_facade().has_project(project):
        return None
    bid = (board_id or "").strip()
    if not bid:
        return None
    with _store_facade()._conn(project) as c:
        row = c.execute("SELECT * FROM project_boards WHERE id=?", (bid,)).fetchone()
    return _project_board_row(row, project=project) if row else None


def list_project_boards(project: str = DEFAULT_PROJECT, kind: str = "",
                        status: str = "") -> List[Dict[str, Any]]:
    if not _store_facade().has_project(project):
        return []
    clauses = []
    args: List[Any] = []
    if (kind or "").strip():
        clauses.append("kind=?")
        args.append(kind.strip().lower())
    if (status or "").strip():
        clauses.append("status=?")
        args.append(status.strip().lower())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _store_facade()._conn(project) as c:
        rows = c.execute(
            f"SELECT * FROM project_boards{where} ORDER BY updated_at DESC, id",
            args,
        ).fetchall()
    return [_project_board_row(row, project=project) for row in rows]


def _deliverable_exists_in(c: sqlite3.Connection, deliverable_id: str) -> bool:
    return bool(c.execute("SELECT 1 FROM deliverables WHERE id=?",
                          (deliverable_id,)).fetchone())


def _deliverable_milestone_exists_in(
        c: sqlite3.Connection, deliverable_id: str, milestone_id: str) -> bool:
    return bool(c.execute(
        "SELECT 1 FROM deliverable_milestones WHERE id=? AND deliverable_id=?",
        (milestone_id, deliverable_id),
    ).fetchone())


# DELIVERABLES-13: intake contract enforced when a deliverable ENTERS in_progress.
# See docs/DELIVERABLE-CLOSURE-GATE.md ("Intake at creation"). Gated off by default so
# existing deliverables and legacy flows are unaffected until operators opt in per-prod
# (mirrors PM_VERIFY_COMPLETION_PUSH / PM_RETIRE_MERGED_BRANCHES rollout style); DELIVERABLES-22
# flips it on after backfill.
PROOF_REQUIREMENTS_SCHEMA = "switchboard.deliverable_proof_requirements.v1"


def _enforce_deliverable_intake() -> bool:
    return (os.environ.get("PM_ENFORCE_DELIVERABLE_INTAKE") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _validate_proof_requirements(proof: Any) -> List[str]:
    """Validate proof contract shape and resolve every declared gate fail-closed."""
    if not isinstance(proof, dict) or not proof:
        return ["proof_requirements must be an object with a non-empty gates list"]
    errors: List[str] = []
    schema = (proof.get("schema") or "").strip()
    if schema and schema != PROOF_REQUIREMENTS_SCHEMA:
        errors.append(f"proof_requirements.schema must be {PROOF_REQUIREMENTS_SCHEMA}")
    gates = proof.get("gates")
    if not isinstance(gates, list) or not gates:
        errors.append("proof_requirements.gates must be a non-empty list of gate refs")
        return errors
    seen: set = set()
    for i, gate in enumerate(gates):
        if not isinstance(gate, dict):
            errors.append(f"proof_requirements.gates[{i}] must be an object")
            continue
        gid = gate.get("id")
        gid = gid.strip() if isinstance(gid, str) else ""
        if not gid:
            errors.append(f"proof_requirements.gates[{i}].id is required")
        elif gid in seen:
            errors.append(f"proof_requirements.gates[{i}].id '{gid}' is duplicated")
        else:
            seen.add(gid)
        if not isinstance(gate.get("required"), bool):
            errors.append(f"proof_requirements.gates[{i}].required must be true or false")
    if not errors:
        try:
            deliverable_gates.resolve_gates(proof)
        except deliverable_gates.GateResolutionError as exc:
            errors.append(str(exc))
    return errors


def _validate_deliverable_intake(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return an error dict if a deliverable entering in_progress is missing its intake
    contract (end_state + acceptance_criteria + well-formed proof_requirements), else None.
    Parses list/object fields exactly as they are stored so validation matches persistence.
    """
    details: List[str] = []
    if not (data.get("end_state") or "").strip():
        details.append("end_state is required: a plain-English success statement")
    criteria = json.loads(_store_facade()._json_list_field(data.get("acceptance_criteria")))
    if not [c for c in criteria if str(c).strip()]:
        details.append("acceptance_criteria must be a non-empty list of success statements")
    proof = json.loads(_store_facade()._json_object_field(data.get("proof_requirements")))
    details.extend(_validate_proof_requirements(proof))
    if details:
        return {
            "error": "deliverable intake incomplete",
            "details": details,
            "required": ["end_state", "acceptance_criteria", "proof_requirements"],
            "proof_requirements_schema": PROOF_REQUIREMENTS_SCHEMA,
            "spec": "docs/DELIVERABLE-CLOSURE-GATE.md",
        }
    return None


def _create_deliverable_impl(data: Dict[str, Any], actor: str = "user",
                             project: str = DEFAULT_PROJECT) -> Any:
    """Create or update a project-owned product outcome/mission record."""
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    title = (data.get("title") or "").strip()
    if not title:
        return {"error": "title required"}
    try:
        deliverable_id = normalize_deliverable_id(data.get("id") or data.get("deliverable_id"), title)
    except ValueError as exc:
        return {"error": str(exc)}
    status = (data.get("status") or "proposed").strip().lower()
    status_error = validate_deliverable_status(status)
    if status_error:
        return status_error
    confidence = data.get("confidence")
    if confidence in ("", None):
        confidence_value = None
    else:
        try:
            confidence_value = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            return {"error": "confidence must be a number between 0 and 1"}
    board_id = (data.get("board_id") or data.get("mission_id") or "").strip() or None
    now = time.time()
    with _store_facade()._conn(project) as c:
        if board_id and not _project_board_exists_in(c, board_id):
            return {"error": "unknown board", "board_id": board_id, "project_id": project}
        prior, incoming_metadata, policy_error = deliverable_policy.prepare_upsert(
            c, deliverable_id, status, data.get("metadata", data.get("metadata_json")))
        if policy_error:
            return policy_error
        # DELIVERABLES-13: validate the intake contract only when entering in_progress.
        if status == "in_progress" and _enforce_deliverable_intake():
            if (prior["status"] if prior else None) != "in_progress":
                intake_error = _validate_deliverable_intake(data)
                if intake_error:
                    return intake_error
        c.execute(
            """INSERT INTO deliverables
               (id, board_id, title, status, owner_org, owner_person_or_role, end_state,
                why_it_matters, confidence, acceptance_criteria_json,
                policy_constraints_json, proof_requirements_json, kpi_links_json,
                metadata_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                board_id=COALESCE(excluded.board_id, deliverables.board_id),
                title=excluded.title,
                status=excluded.status,
                owner_org=excluded.owner_org,
                owner_person_or_role=excluded.owner_person_or_role,
                end_state=excluded.end_state,
                why_it_matters=excluded.why_it_matters,
                confidence=excluded.confidence,
                acceptance_criteria_json=excluded.acceptance_criteria_json,
                policy_constraints_json=excluded.policy_constraints_json,
                proof_requirements_json=excluded.proof_requirements_json,
                kpi_links_json=excluded.kpi_links_json,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at""",
            (
                deliverable_id, board_id, title, status, data.get("owner_org"),
                data.get("owner_person_or_role"), data.get("end_state"),
                data.get("why_it_matters"), confidence_value,
                _store_facade()._json_list_field(data.get("acceptance_criteria")),
                _store_facade()._json_object_field(data.get("policy_constraints")),
                _store_facade()._json_object_field(data.get("proof_requirements")),
                _store_facade()._json_list_field(data.get("kpi_links")),
                json.dumps(incoming_metadata, sort_keys=True),
                now, now,
            ),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.upsert",
                   json.dumps({"deliverable_id": deliverable_id, "board_id": board_id,
                               "title": title},
                              sort_keys=True), now))
    return deliverable_id


def create_deliverable(data: Dict[str, Any], actor: str = "user",
                       project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    result = _store_facade()._write_through(project,
        lambda: _store_facade()._create_deliverable_impl(
            data, actor=actor, project=project))
    if isinstance(result, dict):
        return result
    # NARRATE-11: a new deliverable is revision 1 — enqueue only it (post-commit, idempotent).
    narration_outbox.emit_deliverable_narration_request(
        project, result, cause_kind="deliverable.created", actor=actor)
    return get_deliverable(result, project=project) or {"error": "deliverable not found"}


def add_deliverable_milestone(deliverable_id: str, data: Dict[str, Any],
                              actor: str = "user",
                              project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    title = (data.get("title") or "").strip()
    if not title:
        return {"error": "title required"}
    try:
        raw_mid = data.get("id") or data.get("milestone_id")
        if raw_mid:
            mid = normalize_deliverable_id(raw_mid, title)
        else:
            mid = normalize_deliverable_id(
                f"{deliverable_id}:{_store_facade().normalize_project_id(title)}", title)
    except ValueError as exc:
        return {"error": str(exc)}
    status = (data.get("status") or "not_started").strip().lower()
    if status not in DELIVERABLE_MILESTONE_STATUSES:
        return {"error": "invalid milestone status",
                "allowed": sorted(DELIVERABLE_MILESTONE_STATUSES)}
    now = time.time()
    with _store_facade()._conn(project) as c:
        if not _deliverable_exists_in(c, deliverable_id):
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        order = data.get("sort_order")
        if order in ("", None):
            order = c.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 "
                "FROM deliverable_milestones WHERE deliverable_id=?",
                (deliverable_id,),
            ).fetchone()[0]
        c.execute(
            """INSERT INTO deliverable_milestones
               (id, deliverable_id, title, description, status, sort_order,
                acceptance_criteria_json, proof_requirements_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                status=excluded.status,
                sort_order=excluded.sort_order,
                acceptance_criteria_json=excluded.acceptance_criteria_json,
                proof_requirements_json=excluded.proof_requirements_json,
                updated_at=excluded.updated_at""",
            (
                mid, deliverable_id, title, data.get("description"), status, int(order),
                _store_facade()._json_list_field(data.get("acceptance_criteria")),
                _store_facade()._json_object_field(data.get("proof_requirements")),
                now, now,
            ),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.milestone_upsert",
                   json.dumps({"deliverable_id": deliverable_id, "milestone_id": mid,
                               "title": title}, sort_keys=True), now))
        _touch_deliverable(c, deliverable_id, now)
    # NARRATE-11: a milestone upsert changes the deliverable's projection — enqueue only it.
    narration_outbox.emit_deliverable_narration_request(
        project, deliverable_id, cause_kind="deliverable.milestone_upsert", actor=actor)
    return get_deliverable(deliverable_id, project=project) or {"error": "deliverable not found"}


def _touch_deliverable(c: sqlite3.Connection, deliverable_id: str, ts: float) -> None:
    """Bump the deliverable row's updated_at.

    mission_status / dependency-graph caches are stamped on deliverables.updated_at
    (see _mission_cache_stamp), but link/milestone rows live in child tables whose
    edits don't touch the parent. Without this bump a freshly-linked or -unlinked
    task, or a new milestone, only appears after the cache TTL — so the editable
    mission page would look like it dropped the change. Call this on every
    link/unlink/milestone mutation so the operator sees edits immediately.
    """
    c.execute("UPDATE deliverables SET updated_at=? WHERE id=?", (ts, deliverable_id))


def _link_task_to_deliverable_impl(
        deliverable_id: str, task_project: str, task_id: str,
        milestone_id: str = "", data: Optional[Dict[str, Any]] = None,
        actor: str = "user",
        project: str = DEFAULT_PROJECT) -> Any:
    """Link an explicitly routed board task to a deliverable without moving or editing it."""
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    task_project = (task_project or "").strip()
    task_id = (task_id or "").strip().upper()
    if not _store_facade().has_project(task_project):
        return {"error": f"unknown linked project: {task_project}"}
    target = _store_facade()._deliverable_task_snapshots(task_project, [task_id]).get(task_id)
    if not target:
        return {"error": "unknown linked task", "project_id": task_project, "task_id": task_id}
    payload = data or {}
    requested_board_id = (payload.get("board_id") or payload.get("mission_id") or "").strip() or None
    link_id = (payload.get("id") or payload.get("link_id") or
               f"link-{deliverable_id}-{task_project}-{task_id}")
    role = (payload.get("role") or "").strip()
    if not role or role.lower() == "auto":
        # Auto-classify when the caller doesn't pick a role: a task that is
        # already Done at link time cannot be future flow work for this
        # deliverable — it is groundwork, so it lands in the mission map's
        # context row ('foundation') instead of cluttering the execution DAG.
        # mission_graph still promotes it into the graph if a flow task
        # depends_on it, and an explicit role always wins over this default.
        role = "foundation" if (target.get("status") or "").strip() == "Done" else "contributes"
    now = time.time()
    with _store_facade()._conn(project) as c:
        if not _deliverable_exists_in(c, deliverable_id):
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        deliverable_row = c.execute("SELECT board_id FROM deliverables WHERE id=?",
                                    (deliverable_id,)).fetchone()
        deliverable_board_id = (deliverable_row["board_id"] if deliverable_row else "") or None
        if requested_board_id and not _project_board_exists_in(c, requested_board_id):
            return {"error": "unknown board", "board_id": requested_board_id,
                    "project_id": project}
        if requested_board_id and deliverable_board_id and requested_board_id != deliverable_board_id:
            return {"error": "board mismatch", "board_id": requested_board_id,
                    "deliverable_board_id": deliverable_board_id,
                    "deliverable_id": deliverable_id}
        board_id = requested_board_id or deliverable_board_id
        mid = (milestone_id or payload.get("milestone_id") or "").strip() or None
        if mid and not _deliverable_milestone_exists_in(c, deliverable_id, mid):
            return {"error": "unknown milestone", "deliverable_id": deliverable_id,
                    "milestone_id": mid}
        c.execute(
            """INSERT INTO deliverable_task_links
               (id, deliverable_id, board_id, milestone_id, project_id, task_id, role,
                blocks_deliverable, proof_required_json, metadata_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(deliverable_id, project_id, task_id) DO UPDATE SET
                board_id=excluded.board_id,
                milestone_id=excluded.milestone_id,
                role=excluded.role,
                blocks_deliverable=excluded.blocks_deliverable,
                proof_required_json=excluded.proof_required_json,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at""",
            (
                link_id, deliverable_id, board_id, mid, task_project, task_id, role,
                1 if payload.get("blocks_deliverable") else 0,
                _store_facade()._json_object_field(payload.get("proof_required")),
                _store_facade()._json_object_field(payload.get("metadata", payload.get("metadata_json"))),
                now, now,
            ),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.task_linked",
                   json.dumps({"deliverable_id": deliverable_id, "board_id": board_id,
                               "project_id": task_project,
                               "task_id": task_id, "milestone_id": mid},
                              sort_keys=True), now))
        _touch_deliverable(c, deliverable_id, now)
    return deliverable_id


def link_task_to_deliverable(deliverable_id: str, task_project: str, task_id: str,
                             milestone_id: str = "", data: Optional[Dict[str, Any]] = None,
                             actor: str = "user",
                             project: str = DEFAULT_PROJECT,
                             include_task_snapshots: bool = True,
                             run_closure: bool = False) -> Dict[str, Any]:
    """Link an explicitly routed board task to a deliverable without moving or editing it.

    run_closure is opt-in (default False) so the hot single-link write path stays slim —
    it must not fan out into full get_task/get_deliverable reads (see test_mcp_link_task_slim
    and test_deliverable_link_snapshots). When a caller does pass run_closure=True, the task's
    transitive not-Done depends_on frontier is auto-linked as blockers, so an intentional
    bundling never silently omits work it depends on. Batch bundling (approve_deliverable_breakdown)
    runs the closure explicitly after materializing, independent of this flag.
    """
    result = _store_facade()._write_through(project, lambda: _store_facade()._link_task_to_deliverable_impl(
        deliverable_id, task_project, task_id, milestone_id=milestone_id,
        data=data, actor=actor, project=project))
    if isinstance(result, dict):
        return result
    # result is the deliverable_id of a successful link. Only when a caller opts in do we
    # pull the task's transitive not-Done dependency frontier in as blockers; shape the
    # response afterwards so it already reflects any auto-linked work.
    closure = (_ensure_deliverable_dependency_closure(deliverable_id, project, actor=actor)
               if run_closure else None)
    # NARRATE-11: a new link changes the deliverable's linked-task projection — enqueue ONLY
    # this deliverable (idempotent; no-op if the projection is unchanged). Post-commit.
    narration_outbox.emit_deliverable_narration_request(
        project, deliverable_id, cause_kind="deliverable.link_added", actor=actor)
    if not include_task_snapshots:
        normalized_task_project = (task_project or "").strip()
        normalized_task_id = (task_id or "").strip().upper()
        with _store_facade()._conn(project) as c:
            linked_row = c.execute(
                "SELECT * FROM deliverable_task_links "
                "WHERE deliverable_id=? AND project_id=? AND task_id=?",
                (deliverable_id, normalized_task_project, normalized_task_id),
            ).fetchone()
            linked_task_count = c.execute(
                "SELECT COUNT(*) FROM deliverable_task_links WHERE deliverable_id=?",
                (deliverable_id,),
            ).fetchone()[0]
        if not linked_row:
            return {"error": "linked task acknowledgement unavailable",
                    "deliverable_id": deliverable_id,
                    "project_id": normalized_task_project,
                    "task_id": normalized_task_id}
        ack = {
            "schema": "switchboard.deliverable_task_link_ack.v1",
            "linked": True,
            "project": project,
            "deliverable_id": deliverable_id,
            "task_project": normalized_task_project,
            "task_id": normalized_task_id,
            "milestone_id": linked_row["milestone_id"],
            "task_link": _deliverable_link_row(linked_row),
            "progress": {"linked_task_count": int(linked_task_count)},
            "full_deliverable_tool": "get_deliverable",
        }
        if closure is not None:
            ack["dependency_closure"] = closure
        return ack
    full = get_deliverable(result, project=project) or {"error": "deliverable not found"}
    if closure is not None and not full.get("error"):
        full["dependency_closure"] = closure
    return full


def _link_tasks_to_deliverable_impl(
        deliverable_id: str, links: List[Dict[str, Any]], actor: str = "user",
        project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Validate and persist a batch of deliverable links in one home transaction."""
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    if not isinstance(links, list) or not links:
        return {"error": "links must be a non-empty list"}

    normalized: List[Dict[str, Any]] = []
    unique: Dict[tuple, Dict[str, Any]] = {}
    skipped: List[Dict[str, Any]] = []
    task_ids_by_project: Dict[str, List[str]] = {}
    for index, raw in enumerate(links):
        if not isinstance(raw, dict):
            return {"error": "each link must be an object", "link_index": index}
        task_project = (raw.get("task_project") or raw.get("project_id") or "").strip()
        task_id = (raw.get("task_id") or "").strip().upper()
        if not task_project or not task_id:
            return {"error": "task_project and task_id required", "link_index": index}
        if not _store_facade().has_project(task_project):
            return {"error": f"unknown linked project: {task_project}",
                    "link_index": index}
        item = dict(raw)
        item["task_project"] = task_project
        item["task_id"] = task_id
        key = (task_project, task_id)
        prior = unique.get(key)
        if prior is not None:
            if item != prior:
                return {"error": "conflicting duplicate link", "link_index": index,
                        "project_id": task_project, "task_id": task_id}
            skipped.append({"task_project": task_project, "task_id": task_id,
                            "reason": "duplicate_input"})
            continue
        unique[key] = item
        normalized.append(item)
        task_ids_by_project.setdefault(task_project, []).append(task_id)

    targets: Dict[tuple, Dict[str, Any]] = {}
    for task_project, task_ids in task_ids_by_project.items():
        snapshots = _store_facade()._deliverable_task_snapshots(task_project, task_ids)
        for task_id in task_ids:
            target = snapshots.get(task_id)
            if not target:
                return {"error": "unknown linked task", "project_id": task_project,
                        "task_id": task_id}
            targets[(task_project, task_id)] = target

    now = time.time()
    linked: List[Dict[str, Any]] = []
    with _store_facade()._conn(project, timeout_s=5.0) as c:
        deliverable_row = c.execute(
            "SELECT board_id FROM deliverables WHERE id=?", (deliverable_id,)).fetchone()
        if not deliverable_row:
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        deliverable_board_id = (deliverable_row["board_id"] or "").strip() or None

        prepared: List[Dict[str, Any]] = []
        for index, item in enumerate(normalized):
            requested_board_id = (
                item.get("board_id") or item.get("mission_id") or "").strip() or None
            if requested_board_id and not _project_board_exists_in(c, requested_board_id):
                return {"error": "unknown board", "board_id": requested_board_id,
                        "project_id": project, "link_index": index}
            if (requested_board_id and deliverable_board_id and
                    requested_board_id != deliverable_board_id):
                return {"error": "board mismatch", "board_id": requested_board_id,
                        "deliverable_board_id": deliverable_board_id,
                        "deliverable_id": deliverable_id, "link_index": index}
            milestone_id = (item.get("milestone_id") or "").strip() or None
            if (milestone_id and
                    not _deliverable_milestone_exists_in(c, deliverable_id, milestone_id)):
                return {"error": "unknown milestone", "deliverable_id": deliverable_id,
                        "milestone_id": milestone_id, "link_index": index}
            role = (item.get("role") or "").strip()
            if not role or role.lower() == "auto":
                target = targets[(item["task_project"], item["task_id"])]
                role = ("foundation" if (target.get("status") or "").strip() == "Done"
                        else "contributes")
            prepared.append({**item, "board_id": requested_board_id or deliverable_board_id,
                             "milestone_id": milestone_id, "role": role})

        for item in prepared:
            task_project = item["task_project"]
            task_id = item["task_id"]
            link_id = (item.get("id") or item.get("link_id") or
                       f"link-{deliverable_id}-{task_project}-{task_id}")
            c.execute(
                """INSERT INTO deliverable_task_links
                   (id, deliverable_id, board_id, milestone_id, project_id, task_id, role,
                    blocks_deliverable, proof_required_json, metadata_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(deliverable_id, project_id, task_id) DO UPDATE SET
                    board_id=excluded.board_id,
                    milestone_id=excluded.milestone_id,
                    role=excluded.role,
                    blocks_deliverable=excluded.blocks_deliverable,
                    proof_required_json=excluded.proof_required_json,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at""",
                (link_id, deliverable_id, item["board_id"], item["milestone_id"],
                 task_project, task_id, item["role"],
                 1 if item.get("blocks_deliverable") else 0,
                 _store_facade()._json_object_field(item.get("proof_required",
                                             item.get("proof_required_json"))),
                 _store_facade()._json_object_field(item.get("metadata", item.get("metadata_json"))),
                 now, now),
            )
            activity_payload = {
                "deliverable_id": deliverable_id, "board_id": item["board_id"],
                "project_id": task_project, "task_id": task_id,
                "milestone_id": item["milestone_id"],
            }
            c.execute(
                "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (None, actor, "deliverable.task_linked",
                 json.dumps(activity_payload, sort_keys=True), now),
            )
            linked.append({"task_project": task_project, "task_id": task_id,
                           "role": item["role"], "milestone_id": item["milestone_id"]})
        _touch_deliverable(c, deliverable_id, now)
        linked_task_count = c.execute(
            "SELECT COUNT(*) FROM deliverable_task_links WHERE deliverable_id=?",
            (deliverable_id,),
        ).fetchone()[0]

    return {
        "schema": "switchboard.deliverable_task_links_ack.v1",
        "project": project,
        "deliverable_id": deliverable_id,
        "linked": linked,
        "skipped": skipped,
        "progress_counts": {
            "requested": len(links), "linked": len(linked), "skipped": len(skipped),
            "linked_task_count": int(linked_task_count),
        },
        "full_deliverable_tool": "get_deliverable",
    }


def link_tasks_to_deliverable(deliverable_id: str, links: List[Dict[str, Any]],
                              actor: str = "user",
                              project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Link N explicitly routed tasks with one retryable home-database transaction."""
    result = _store_facade()._write_through(project, lambda: _store_facade()._link_tasks_to_deliverable_impl(
        deliverable_id, links, actor=actor, project=project))
    # NARRATE-11: batch relink changes the deliverable's linked-task projection — enqueue only it.
    if not (isinstance(result, dict) and result.get("error")):
        narration_outbox.emit_deliverable_narration_request(
            project, deliverable_id, cause_kind="deliverable.links_updated", actor=actor)
    return result


def _rows_for_task_ids(c: sqlite3.Connection, table: str, task_ids: List[str],
                       order_by: str) -> List[sqlite3.Row]:
    """Read task-scoped rows in bounded batches without exceeding SQLite variables."""
    rows: List[sqlite3.Row] = []
    for i in range(0, len(task_ids), 400):
        batch = task_ids[i:i + 400]
        placeholders = ",".join("?" * len(batch))
        rows.extend(c.execute(
            f"SELECT * FROM {table} WHERE task_id COLLATE NOCASE IN ({placeholders}) "
            f"ORDER BY task_id, {order_by}",
            batch,
        ).fetchall())
    return rows


def _deliverable_task_snapshots(project: str,
                                task_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load the compact task shape used by deliverable links in bounded queries.

    This deliberately avoids _store_facade().get_task(): deliverable responses do not consume activity,
    claims, session health, dependency state, or project context.  Base task rows,
    provenance, external-CI runs, and publication evidence are each loaded in batches.
    """
    requested = list(dict.fromkeys(str(t or "").strip() for t in task_ids if str(t or "").strip()))
    if not requested or not _store_facade().has_project(project):
        return {}
    folded = {task_id.upper(): task_id for task_id in requested}
    with _store_facade()._conn(project) as c:
        task_rows = [_task_row(row) for row in _rows_for_task_ids(
            c, "tasks", requested, "sort_order")]
        tasks = {task["task_id"].upper(): task for task in task_rows}
        canonical_ids = [task["task_id"] for task in tasks.values()]
        git_states = _store_facade()._git_states_by_task(c, canonical_ids)
        external_rows: Dict[str, List[Dict[str, Any]]] = {}
        for row in _rows_for_task_ids(c, "external_ci_runs", canonical_ids,
                                      "updated_at DESC, run_id"):
            external_rows.setdefault(row["task_id"].upper(), []).append(_store_facade()._external_ci_row(row))
        publication_rows: Dict[str, List[Dict[str, Any]]] = {}
        for row in _rows_for_task_ids(c, "publication_evidence", canonical_ids,
                                      "updated_at DESC, publication_id"):
            publication_rows.setdefault(row["task_id"].upper(), []).append(_store_facade()._publication_row(row))
    contract = _store_facade()._external_ci_topology_contract(project)
    snapshots: Dict[str, Dict[str, Any]] = {}
    for folded_id, task in tasks.items():
        requested_id = folded.get(folded_id, task["task_id"])
        git_state = git_states.get(task["task_id"], _store_facade()._git_state_row(None))
        task["git_state"] = git_state
        external_ci = _store_facade()._external_ci_summary(
            external_rows.get(folded_id, []), source_sha=git_state.get("head_sha") or "",
            project=project, contract=contract)
        publication = _store_facade()._publication_summary(
            publication_rows.get(folded_id, []),
            source_sha=git_state.get("merged_sha") or git_state.get("head_sha") or "")
        snapshots[requested_id] = {
            "task_id": task["task_id"],
            "title": task.get("title"),
            "status": task.get("status"),
            "workstream": task.get("_wsId"),
            "provenance": _store_facade()._provenance_summary(git_state),
            "external_ci": _store_facade()._external_ci_review_gate(
                task, project=project, summary=external_ci),
            "publication": _store_facade()._publication_review_gate(
                task, project=project, summary=publication),
        }
    return snapshots


def _decorate_deliverable_task_links(links: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    links_by_project: Dict[str, List[Dict[str, Any]]] = {}
    for link in links:
        project_id = link.get("project_id") or ""
        if not _store_facade().has_project(project_id):
            link["task"] = {"error": "unknown project", "project_id": project_id}
            continue
        links_by_project.setdefault(project_id, []).append(link)
    for project_id, project_links in links_by_project.items():
        snapshots = _store_facade()._deliverable_task_snapshots(
            project_id, [link["task_id"] for link in project_links])
        for link in project_links:
            task = snapshots.get(link["task_id"])
            link["task"] = task or {
                "error": "unknown task", "project_id": project_id, "task_id": link["task_id"]}
    return links


def _deliverable_dependency_closure(deliverable_id: str,
                                    project: str,
                                    max_nodes: int = 500) -> Dict[str, Any]:
    """Walk the transitive depends_on frontier of a deliverable's linked tasks.

    Returns the not-Done dependencies that are NOT yet linked to the deliverable
    (the real 'missing blockers') plus already-satisfied (Done) deps for context.
    Traversal stops at Done tasks: a satisfied dependency is not remaining work and
    its history must not be dragged in. Because the walk expands every not-Done node
    regardless of whether it is linked yet, a single call yields the full transitive
    not-Done closure. depends_on ids are same-project as the task that declares them.
    """
    deliverable = get_deliverable(deliverable_id, project=project)
    if not deliverable or deliverable.get("error"):
        return {"missing": [], "satisfied": [], "linked_task_count": 0, "capped": False}
    links = deliverable.get("task_links") or []

    def _key(proj: str, tid: str) -> Tuple[str, str]:
        return ((proj or project).strip(), (tid or "").strip().upper())

    linked_keys = {_key(l.get("project_id"), l.get("task_id")) for l in links}
    seen: set = set()
    queue: List[Tuple[str, str]] = []
    for l in links:
        k = _key(l.get("project_id"), l.get("task_id"))
        if k not in seen:
            seen.add(k)
            queue.append(k)

    missing: List[Dict[str, Any]] = []
    satisfied: List[Dict[str, Any]] = []
    reported: set = set()
    capped = False
    while queue:
        if len(seen) > max_nodes:
            capped = True
            break
        proj, tid = queue.pop()
        task = _store_facade().get_task(tid, project=proj)
        # Only expand outstanding work: a Done task's deps are already satisfied
        # groundwork and must not pull historical tasks into the deliverable.
        if not task or (task.get("status") or "").strip() == "Done":
            continue
        for dep in task.get("depends_on") or []:
            dep_key = _key(proj, dep)
            dep_task = _store_facade().get_task(dep, project=proj)
            if not dep_task:
                continue  # broken edge; add_dependency guards new ones, skip dangling
            dep_status = (dep_task.get("status") or "").strip()
            if dep_key not in reported and dep_key not in linked_keys:
                entry = {"project_id": dep_key[0], "task_id": dep_key[1],
                         "title": dep_task.get("title"), "status": dep_status,
                         "via_task_id": tid}
                (satisfied if dep_status == "Done" else missing).append(entry)
                reported.add(dep_key)
            if dep_key not in seen and dep_status != "Done":
                seen.add(dep_key)
                queue.append(dep_key)
    return {"missing": missing, "satisfied": satisfied,
            "linked_task_count": len(links), "capped": capped}


def _ensure_deliverable_dependency_closure(deliverable_id: str,
                                           project: str,
                                           actor: str = "system") -> Dict[str, Any]:
    """Auto-link a deliverable's not-Done transitive dependencies as blockers.

    Runs after link/approve so a bundled deliverable always carries the work it
    actually depends on. Missing blockers are linked with blocks_deliverable=1 (they
    gate completion and draw in the dependency graph); already-satisfied Done deps are
    reported but left out. Idempotent: re-running links nothing new.
    """
    closure = _deliverable_dependency_closure(deliverable_id, project)
    auto_linked: List[Dict[str, Any]] = []
    for dep in closure.get("missing") or []:
        res = link_task_to_deliverable(
            deliverable_id, dep["project_id"], dep["task_id"],
            data={"role": "contributes", "blocks_deliverable": True,
                  "metadata": {"auto_linked": "dependency_closure",
                               "via_task_id": dep.get("via_task_id")}},
            actor=actor, project=project, include_task_snapshots=False,
            run_closure=False,
        )
        if not res.get("error"):
            auto_linked.append({"project_id": dep["project_id"], "task_id": dep["task_id"],
                                "title": dep.get("title"), "via_task_id": dep.get("via_task_id")})
    return {
        "auto_linked": auto_linked,
        "auto_linked_count": len(auto_linked),
        "already_satisfied": closure.get("satisfied") or [],
        "capped": closure.get("capped", False),
    }


def _decorate_deliverable_task_link(link: Dict[str, Any]) -> Dict[str, Any]:
    """Compatibility wrapper for callers decorating one link."""
    return _decorate_deliverable_task_links([link])[0]


def get_deliverable(deliverable_id: str, project: str = DEFAULT_PROJECT,
                    include_task_snapshots: bool = True) -> Optional[Dict[str, Any]]:
    if not _store_facade().has_project(project):
        return None
    with _store_facade()._conn(project) as c:
        row = c.execute("SELECT * FROM deliverables WHERE id=?",
                        (deliverable_id,)).fetchone()
        if not row:
            return None
        deliverable = _deliverable_row(row)
        if deliverable.get("board_id"):
            board_row = c.execute("SELECT * FROM project_boards WHERE id=?",
                                  (deliverable["board_id"],)).fetchone()
            deliverable["board"] = (_project_board_row(board_row, project=project)
                                    if board_row else {"error": "unknown board",
                                                       "board_id": deliverable["board_id"],
                                                       "project_id": project})
        milestones = [
            _deliverable_milestone_row(r)
            for r in c.execute(
                "SELECT * FROM deliverable_milestones WHERE deliverable_id=? "
                "ORDER BY sort_order, created_at, id",
                (deliverable_id,),
            ).fetchall()
        ]
        links = [
            _deliverable_link_row(r)
            for r in c.execute(
                "SELECT * FROM deliverable_task_links WHERE deliverable_id=? "
                "ORDER BY created_at, id",
                (deliverable_id,),
            ).fetchall()
        ]
    if include_task_snapshots:
        links = _decorate_deliverable_task_links(links)
    deliverable["milestones"] = milestones
    deliverable["task_links"] = links
    deliverable["progress"] = deliverable_progress(deliverable)
    return deliverable


def list_deliverables(project: str = DEFAULT_PROJECT, board_id: str = "",
                      include_task_snapshots: bool = True) -> List[Dict[str, Any]]:
    if not _store_facade().has_project(project):
        return []
    board_id = (board_id or "").strip()
    with _store_facade()._conn(project) as c:
        if board_id:
            if not _project_board_exists_in(c, board_id):
                return []
            rows = c.execute(
                "SELECT id FROM deliverables WHERE board_id=? ORDER BY updated_at DESC, id",
                (board_id,),
            ).fetchall()
        else:
            rows = c.execute("SELECT id FROM deliverables ORDER BY updated_at DESC, id").fetchall()
    # Build every row without snapshots first, then decorate the flattened link set
    # once. This keeps list latency proportional to linked projects rather than
    # N deliverables x M links, while still producing truthful progress counts.
    deliverables = [d for d in (
        get_deliverable(r["id"], project=project, include_task_snapshots=False)
        for r in rows
    ) if d]
    all_links = [
        link
        for deliverable in deliverables
        for link in (deliverable.get("task_links") or [])
    ]
    if all_links:
        _decorate_deliverable_task_links(all_links)
    for deliverable in deliverables:
        deliverable["progress"] = deliverable_progress(deliverable)
        if not include_task_snapshots:
            for link in deliverable.get("task_links") or []:
                link.pop("task", None)
    return deliverables


def list_deliverable_summaries(project: str = DEFAULT_PROJECT,
                               board_id: str = "") -> List[Dict[str, Any]]:
    """Return the metadata needed by project-scoped deliverable pickers.

    This deliberately does not call ``get_deliverable``: picker rendering needs
    no milestones, task links, cross-project task snapshots, or progress proof.
    Keeping this as one local SQL read prevents a navigation control from
    competing with the mission cockpit's much heavier live status reads.
    """
    if not _store_facade().has_project(project):
        return []
    board_id = (board_id or "").strip()
    with _store_facade()._conn(project) as c:
        if board_id:
            if not _project_board_exists_in(c, board_id):
                return []
            rows = c.execute(
                "SELECT id, board_id, title, status, updated_at FROM deliverables "
                "WHERE board_id=? ORDER BY updated_at DESC, id",
                (board_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, board_id, title, status, updated_at FROM deliverables "
                "ORDER BY updated_at DESC, id"
            ).fetchall()
    return [dict(row, mission_id=row["board_id"] or None) for row in rows]


def archive_deliverable(deliverable_id: str, project: str = DEFAULT_PROJECT,
                        actor: str = "user", archived: bool = True) -> Dict[str, Any]:
    """UI-11: archive a deliverable (or restore it) by flipping its status to/from
    'archived'. Archived deliverables are hidden from the picker by default but remain
    fully readable — nothing is deleted. Restore lands in 'in_review' (the operator can
    re-mark it Done)."""
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    deliverable = get_deliverable(deliverable_id, project=project)
    if not deliverable:
        return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
    new_status = "archived" if archived else "in_review"
    now = time.time()
    with _store_facade()._conn(project) as c:
        c.execute("UPDATE deliverables SET status=?, updated_at=? WHERE id=?",
                  (new_status, now, deliverable_id))
    return {"ok": True, "deliverable_id": deliverable_id, "status": new_status,
            "archived": archived, "actor": actor}


def deliverable_progress(deliverable: Dict[str, Any]) -> Dict[str, Any]:
    links = deliverable.get("task_links") or []
    status_counts: Dict[str, int] = {}
    done = in_review = blocked = 0
    external_ci_required = external_ci_passed = external_ci_blocked = 0
    publication_required = publication_passed = publication_blocked = 0
    for link in links:
        task = link.get("task") or {}
        status = task.get("status") or "Unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "Done" and ((task.get("provenance") or {}).get("terminal")):
            done += 1
        elif status == "In Review":
            in_review += 1
        elif status == "Blocked":
            blocked += 1
        proof_required = link.get("proof_required") or {}
        external_ci = task.get("external_ci") or {}
        gate = external_ci.get("gate") or {}
        if proof_required.get("external_ci_passed") or gate.get("required"):
            external_ci_required += 1
            if external_ci.get("passed"):
                external_ci_passed += 1
            else:
                external_ci_blocked += 1
        publication = task.get("publication") or {}
        publication_gate = publication.get("gate") or {}
        if (proof_required.get("publication_evidence")
                or proof_required.get("public_mirror_published")
                or proof_required.get("publish_evidence")
                or publication_gate.get("required")):
            publication_required += 1
            if publication.get("passed"):
                publication_passed += 1
            else:
                publication_blocked += 1
    total = len(links)
    return {
        "linked_task_count": total,
        "done_with_proof_count": done,
        "in_review_count": in_review,
        "blocked_count": blocked,
        "external_ci_required_count": external_ci_required,
        "external_ci_passed_count": external_ci_passed,
        "external_ci_blocked_count": external_ci_blocked,
        "publication_required_count": publication_required,
        "publication_passed_count": publication_passed,
        "publication_blocked_count": publication_blocked,
        "status_counts": dict(sorted(status_counts.items())),
        "done_with_proof_ratio": (done / total) if total else 0.0,
    }


def _breakdown_proposal_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["payload"] = _store_facade()._json_payload(d.pop("payload_json", ""))
    return d


def _validate_breakdown_task_spec(milestone_idx: int, task_idx: int,
                                  task: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    task_project = (task.get("project_id") or task.get("task_project") or "").strip()
    if not task_project:
        return None, f"milestones[{milestone_idx}].tasks[{task_idx}] requires project_id"
    if not _store_facade().has_project(task_project):
        return None, f"unknown linked project: {task_project}"
    action = (task.get("action") or "create").strip().lower()
    if action == "link":
        task_id = (task.get("task_id") or "").strip().upper()
        if not task_id:
            return None, f"milestones[{milestone_idx}].tasks[{task_idx}] link requires task_id"
        if not _store_facade().get_task(task_id, project=task_project):
            return None, (
                f"unknown linked task {task_id} on project {task_project}"
            )
        return dict(task, action="link", project_id=task_project, task_id=task_id), None
    workstream_id = (task.get("workstream_id") or "").strip()
    task_title = (task.get("title") or "").strip()
    if not workstream_id or not task_title:
        return None, (
            f"milestones[{milestone_idx}].tasks[{task_idx}] create requires "
            "workstream_id and title"
        )
    return dict(task, action="create", project_id=task_project,
                workstream_id=workstream_id, title=task_title), None


def _validate_breakdown_payload(payload: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    parsed = _store_facade()._parse_jsonish(payload)
    if not isinstance(parsed, dict):
        return None, "breakdown payload must be a JSON object"
    milestones = parsed.get("milestones")
    if isinstance(milestones, str):
        milestones = _store_facade()._parse_jsonish(milestones)
    if not isinstance(milestones, list) or not milestones:
        return None, "breakdown payload requires a non-empty milestones array"
    normalized: List[Dict[str, Any]] = []
    for idx, milestone in enumerate(milestones):
        if not isinstance(milestone, dict):
            return None, f"milestones[{idx}] must be an object"
        title = (milestone.get("title") or "").strip()
        if not title:
            return None, f"milestones[{idx}] requires title"
        tasks = milestone.get("tasks") or []
        if tasks and not isinstance(tasks, list):
            return None, f"milestones[{idx}].tasks must be an array"
        normalized_tasks: List[Dict[str, Any]] = []
        for t_idx, task in enumerate(tasks):
            if not isinstance(task, dict):
                return None, f"milestones[{idx}].tasks[{t_idx}] must be an object"
            normalized_task, err = _validate_breakdown_task_spec(idx, t_idx, task)
            if err:
                return None, err
            normalized_tasks.append(normalized_task)
        normalized.append({
            "id": (milestone.get("id") or "").strip() or None,
            "title": title,
            "description": milestone.get("description"),
            "status": (milestone.get("status") or "not_started").strip().lower(),
            "sort_order": milestone.get("sort_order"),
            "acceptance_criteria": milestone.get("acceptance_criteria") or [],
            "proof_requirements": milestone.get("proof_requirements") or {},
            "tasks": normalized_tasks,
        })
    target_projects = parsed.get("target_projects") or []
    if isinstance(target_projects, str):
        target_projects = _store_facade()._parse_jsonish(target_projects)
    if target_projects and not isinstance(target_projects, list):
        return None, "target_projects must be an array"
    for tp_idx, target in enumerate(target_projects or []):
        if isinstance(target, str):
            if not _store_facade().has_project(target.strip()):
                return None, f"unknown target project: {target.strip()}"
            continue
        if not isinstance(target, dict):
            return None, f"target_projects[{tp_idx}] must be an object or project id string"
        pid = (target.get("project_id") or target.get("project") or "").strip()
        if not pid or not _store_facade().has_project(pid):
            return None, f"unknown target project: {pid or target}"
    return {
        "schema": parsed.get("schema") or "switchboard.deliverable_breakdown_draft.v1",
        "outcome": parsed.get("outcome"),
        "target_projects": target_projects or [],
        "policy_constraints": parsed.get("policy_constraints") or {},
        "acceptance_criteria": parsed.get("acceptance_criteria") or [],
        "milestones": normalized,
        "notes": parsed.get("notes"),
        "generation": parsed.get("generation") or {},
    }, None


def unlink_task_from_deliverable(deliverable_id: str, task_project: str, task_id: str,
                                 actor: str = "user",
                                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Remove a cross-project task link from a deliverable without mutating the task."""
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    task_project = (task_project or "").strip()
    task_id = (task_id or "").strip().upper()
    if not task_project or not task_id:
        return {"error": "task_project and task_id are required"}
    now = time.time()
    with _store_facade()._conn(project) as c:
        if not _deliverable_exists_in(c, deliverable_id):
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        row = c.execute(
            "SELECT id FROM deliverable_task_links "
            "WHERE deliverable_id=? AND project_id=? AND task_id=?",
            (deliverable_id, task_project, task_id),
        ).fetchone()
        if not row:
            return {"error": "unknown task link", "deliverable_id": deliverable_id,
                    "project_id": task_project, "task_id": task_id}
        c.execute(
            "DELETE FROM deliverable_task_links "
            "WHERE deliverable_id=? AND project_id=? AND task_id=?",
            (deliverable_id, task_project, task_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.task_unlinked",
                   json.dumps({"deliverable_id": deliverable_id, "project_id": task_project,
                               "task_id": task_id}, sort_keys=True), now))
        _touch_deliverable(c, deliverable_id, now)
    # NARRATE-11: unlinking changes the deliverable's linked-task projection — enqueue only it.
    narration_outbox.emit_deliverable_narration_request(
        project, deliverable_id, cause_kind="deliverable.link_removed", actor=actor)
    return get_deliverable(deliverable_id, project=project) or {"error": "deliverable not found"}


def update_mission_narrative(deliverable_id: str, narrative: str, actor: str = "user",
                             project: str = DEFAULT_PROJECT,
                             append: bool = False) -> Dict[str, Any]:
    """Store or append the operator-facing mission narrative on a deliverable."""
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    text = (narrative or "").strip()
    if not text:
        return {"error": "narrative is required"}
    now = time.time()
    with _store_facade()._conn(project) as c:
        row = c.execute("SELECT metadata_json FROM deliverables WHERE id=?",
                        (deliverable_id,)).fetchone()
        if not row:
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        metadata = _store_facade()._json_payload(row["metadata_json"])
        if append and metadata.get("narrative"):
            metadata["narrative"] = f"{metadata['narrative'].rstrip()}\n\n{text}"
        else:
            metadata["narrative"] = text
        metadata["narrative_updated_at"] = now
        metadata["narrative_updated_by"] = actor
        metadata["narrative_source"] = "manual"
        c.execute(
            "UPDATE deliverables SET metadata_json=?, updated_at=? WHERE id=?",
            (json.dumps(metadata, sort_keys=True), now, deliverable_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.narrative_updated",
                   json.dumps({"deliverable_id": deliverable_id}, sort_keys=True), now))
    return get_deliverable(deliverable_id, project=project) or {"error": "deliverable not found"}


# DELIVERABLES-16: persisted closure reports live in deliverable metadata. We keep
# the newest N full reports plus a last_closure_* summary for the mission header;
# grading itself happens in deliverable_closure (this only stores the graded result).
CLOSURE_REPORT_HISTORY_LIMIT = 10


def _closure_report_id(report: Dict[str, Any], now: float) -> str:
    existing = (report.get("report_id") or "").strip()
    if existing:
        return existing
    stamp = int(report.get("generated_at") or now)
    digest = (report.get("evidence_hash") or "").split(":")[-1][:12] or f"{stamp:x}"
    return f"closure-{stamp}-{digest}"


def _closure_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "report_id": report.get("report_id"),
        "grade": report.get("grade"),
        "recommendation": report.get("recommendation"),
        "generated_at": report.get("generated_at"),
        "generated_by": report.get("generated_by"),
        "evidence_hash": report.get("evidence_hash"),
    }


def _record_deliverable_closure_impl(deliverable_id: str, report: Dict[str, Any],
                                     actor: str, project: str) -> Dict[str, Any]:
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    if not isinstance(report, dict) or not report.get("grade"):
        return {"error": "report must be a closure report object with a grade"}
    now = time.time()
    report = dict(report)
    report["report_id"] = _closure_report_id(report, now)
    with _store_facade()._conn(project) as c:
        row = c.execute("SELECT metadata_json FROM deliverables WHERE id=?",
                        (deliverable_id,)).fetchone()
        if not row:
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        metadata = _store_facade()._json_payload(row["metadata_json"])
        history = [r for r in (metadata.get("closure_reports") or [])
                   if isinstance(r, dict) and r.get("report_id") != report["report_id"]]
        metadata["closure_reports"] = ([report] + history)[:CLOSURE_REPORT_HISTORY_LIMIT]
        metadata["last_closure_report"] = report
        metadata["last_closure_grade"] = report.get("grade")
        metadata["last_closure_at"] = now
        c.execute("UPDATE deliverables SET metadata_json=?, updated_at=? WHERE id=?",
                  (json.dumps(metadata, sort_keys=True), now, deliverable_id))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.closure_verified",
                   json.dumps({"deliverable_id": deliverable_id, **_closure_summary(report)},
                              sort_keys=True), now))
    return {"ok": True, "deliverable_id": deliverable_id, "report_id": report["report_id"],
            "grade": report.get("grade"), "recommendation": report.get("recommendation"),
            "report": report}


def record_deliverable_closure(deliverable_id: str, report: Dict[str, Any],
                               actor: str = "verifier",
                               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Persist a graded closure report on the deliverable and stamp
    ``deliverable.closure_verified``. Retains the newest
    ``CLOSURE_REPORT_HISTORY_LIMIT`` full reports plus a ``last_closure_*`` summary
    for the mission header. Atomic (report write + audit stamp) via _write_through.
    Grading lives in :mod:`deliverable_closure`; this only stores the result."""
    return _store_facade()._write_through(project,
        lambda: _store_facade()._record_deliverable_closure_impl(
            deliverable_id, report, actor, project))


def get_deliverable_closure_report(deliverable_id: str, project: str = DEFAULT_PROJECT,
                                   report_id: str = "") -> Dict[str, Any]:
    """Return the latest (or a specific ``report_id``) persisted closure report plus
    a summary of the retained grade history."""
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    with _store_facade()._conn(project) as c:
        row = c.execute("SELECT metadata_json FROM deliverables WHERE id=?",
                        (deliverable_id,)).fetchone()
    if not row:
        return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
    metadata = _store_facade()._json_payload(row["metadata_json"])
    reports = [r for r in (metadata.get("closure_reports") or []) if isinstance(r, dict)]
    history = [_closure_summary(r) for r in reports]
    if report_id:
        report = next((r for r in reports if r.get("report_id") == report_id), None)
        if report is None:
            return {"error": "closure report not found", "deliverable_id": deliverable_id,
                    "report_id": report_id, "history": history}
    else:
        report = metadata.get("last_closure_report") or (reports[0] if reports else None)
    return {"deliverable_id": deliverable_id, "report": report,
            "grade": (report or {}).get("grade"), "history": history, "count": len(reports)}


def propose_deliverable_breakdown(deliverable_id: str, payload: Any, actor: str = "user",
                                  project: str = DEFAULT_PROJECT,
                                  proposal_id: str = "",
                                  outcome_text: str = "") -> Dict[str, Any]:
    """Store a milestone/task breakdown proposal without creating board tasks."""
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    normalized, err = _validate_breakdown_payload(payload)
    if err:
        return {"error": err}
    pid = (proposal_id or "").strip() or f"proposal-{deliverable_id}-{uuid.uuid4().hex[:10]}"
    outcome = (outcome_text or normalized.get("outcome") or "").strip() or None
    now = time.time()
    with _store_facade()._conn(project) as c:
        if not _deliverable_exists_in(c, deliverable_id):
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        c.execute(
            "UPDATE deliverable_breakdown_proposals SET status='superseded', updated_at=? "
            "WHERE deliverable_id=? AND status='proposed'",
            (now, deliverable_id),
        )
        c.execute(
            """INSERT INTO deliverable_breakdown_proposals
               (id, deliverable_id, status, proposed_by, approved_by, reviewed_by,
                outcome_text, review_reason, deferred_until, payload_json,
                created_at, updated_at, approved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, deliverable_id, "proposed", actor, None, None, outcome, None, None,
             json.dumps(normalized, sort_keys=True), now, now, None),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.breakdown_proposed",
                   json.dumps({"deliverable_id": deliverable_id, "proposal_id": pid,
                               "outcome": outcome}, sort_keys=True), now))
    return get_deliverable_breakdown_proposal(pid, project=project) or {
        "error": "proposal not found", "proposal_id": pid}


def get_deliverable_breakdown_proposal(proposal_id: str,
                                       project: str = DEFAULT_PROJECT) -> Optional[Dict[str, Any]]:
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    proposal_id = (proposal_id or "").strip()
    if not proposal_id:
        return {"error": "proposal_id is required"}
    with _store_facade()._conn(project) as c:
        row = c.execute("SELECT * FROM deliverable_breakdown_proposals WHERE id=?",
                        (proposal_id,)).fetchone()
    if not row:
        return None
    proposal = _breakdown_proposal_row(row)
    return {
        "schema": "switchboard.deliverable_breakdown_proposal.v1",
        "project_id": project,
        "deliverable_id": proposal["deliverable_id"],
        "proposal": proposal,
        "deliverable": get_deliverable(proposal["deliverable_id"], project=project),
        "tasks_created": proposal.get("status") == "approved",
    }


def list_deliverable_breakdown_proposals(deliverable_id: str = "",
                                         project: str = DEFAULT_PROJECT,
                                         status: str = "") -> List[Dict[str, Any]]:
    if not _store_facade().has_project(project):
        return []
    deliverable_id = (deliverable_id or "").strip()
    status = (status or "").strip().lower()
    query = "SELECT * FROM deliverable_breakdown_proposals WHERE 1=1"
    params: List[Any] = []
    if deliverable_id:
        query += " AND deliverable_id=?"
        params.append(deliverable_id)
    if status:
        if status not in BREAKDOWN_PROPOSAL_STATUSES:
            return []
        query += " AND status=?"
        params.append(status)
    query += " ORDER BY updated_at DESC, created_at DESC, id"
    with _store_facade()._conn(project) as c:
        rows = c.execute(query, params).fetchall()
    return [_breakdown_proposal_row(r) for r in rows]


def update_deliverable_breakdown_proposal(proposal_id: str, payload: Any,
                                          actor: str = "user",
                                          project: str = DEFAULT_PROJECT,
                                          outcome_text: str = "") -> Dict[str, Any]:
    """Edit a pending breakdown proposal before approval."""
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    proposal_id = (proposal_id or "").strip()
    if not proposal_id:
        return {"error": "proposal_id is required"}
    normalized, err = _validate_breakdown_payload(payload)
    if err:
        return {"error": err}
    now = time.time()
    with _store_facade()._conn(project) as c:
        row = c.execute("SELECT * FROM deliverable_breakdown_proposals WHERE id=?",
                        (proposal_id,)).fetchone()
        if not row:
            return {"error": "unknown proposal", "proposal_id": proposal_id}
        if row["status"] != "proposed":
            return {"error": "only proposed breakdowns can be edited",
                    "proposal_id": proposal_id, "status": row["status"]}
        outcome = (outcome_text or normalized.get("outcome") or row["outcome_text"] or "").strip()
        c.execute(
            "UPDATE deliverable_breakdown_proposals "
            "SET payload_json=?, outcome_text=?, updated_at=? WHERE id=?",
            (json.dumps(normalized, sort_keys=True), outcome or None, now, proposal_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.breakdown_updated",
                   json.dumps({"proposal_id": proposal_id,
                               "deliverable_id": row["deliverable_id"]}, sort_keys=True), now))
    return get_deliverable_breakdown_proposal(proposal_id, project=project) or {
        "error": "proposal not found", "proposal_id": proposal_id}


def _finalize_breakdown_review(proposal_id: str, status: str, actor: str,
                               project: str, reason: str = "",
                               deferred_until: Optional[float] = None) -> Dict[str, Any]:
    if status not in BREAKDOWN_PROPOSAL_STATUSES:
        return {"error": "invalid proposal status", "allowed": sorted(BREAKDOWN_PROPOSAL_STATUSES)}
    reason = (reason or "").strip()
    if status in ("rejected", "deferred") and not reason:
        return {"error": f"{status} requires reason"}
    now = time.time()
    with _store_facade()._conn(project) as c:
        row = c.execute("SELECT * FROM deliverable_breakdown_proposals WHERE id=?",
                        (proposal_id,)).fetchone()
        if not row:
            return {"error": "unknown proposal", "proposal_id": proposal_id}
        if row["status"] != "proposed":
            return {"error": "proposal is not pending review", "proposal_id": proposal_id,
                    "status": row["status"]}
        c.execute(
            "UPDATE deliverable_breakdown_proposals "
            "SET status=?, review_reason=?, reviewed_by=?, deferred_until=?, updated_at=? "
            "WHERE id=?",
            (status, reason or None, actor, deferred_until, now, proposal_id),
        )
        kind = f"deliverable.breakdown_{status}"
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, kind,
                   json.dumps({"proposal_id": proposal_id,
                               "deliverable_id": row["deliverable_id"],
                               "reason": reason,
                               "deferred_until": deferred_until}, sort_keys=True), now))
    return get_deliverable_breakdown_proposal(proposal_id, project=project) or {
        "error": "proposal not found", "proposal_id": proposal_id}


def reject_deliverable_breakdown(proposal_id: str, reason: str, actor: str = "user",
                                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Reject a pending breakdown proposal with an audited reason."""
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    return _finalize_breakdown_review(
        (proposal_id or "").strip(), "rejected", actor, project, reason=reason)


def defer_deliverable_breakdown(proposal_id: str, reason: str, actor: str = "user",
                                project: str = DEFAULT_PROJECT,
                                defer_until: Optional[float] = None) -> Dict[str, Any]:
    """Defer a pending breakdown proposal with an audited reason."""
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    return _finalize_breakdown_review(
        (proposal_id or "").strip(), "deferred", actor, project,
        reason=reason, deferred_until=defer_until)


def submit_deliverable_outcome(deliverable_id: str, outcome: str, actor: str = "user",
                               project: str = DEFAULT_PROJECT,
                               target_projects: Any = None,
                               policy_constraints: Any = None,
                               acceptance_criteria: Any = None,
                               use_llm: bool = False) -> Dict[str, Any]:
    """Generate and store a breakdown proposal from a coordinator outcome statement."""
    import deliverable_breakdown

    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    outcome = (outcome or "").strip()
    if not outcome:
        return {"error": "outcome is required"}
    deliverable = get_deliverable(deliverable_id, project=project)
    if not deliverable:
        return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
    try:
        draft = deliverable_breakdown.generate_breakdown_draft(
            outcome,
            deliverable=deliverable,
            target_projects=target_projects,
            policy_constraints=policy_constraints,
            acceptance_criteria=acceptance_criteria,
            project=project,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    if use_llm:
        draft = deliverable_breakdown.maybe_enrich_with_llm(draft, project=project)
    return propose_deliverable_breakdown(
        deliverable_id, draft, actor=actor, project=project, outcome_text=outcome)


def approve_deliverable_breakdown(proposal_id: str, actor: str = "user",
                                  project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Materialize an approved breakdown into milestones, tasks, and deliverable links."""
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    proposal_id = (proposal_id or "").strip()
    if not proposal_id:
        return {"error": "proposal_id is required"}
    with _store_facade()._conn(project) as c:
        row = c.execute("SELECT * FROM deliverable_breakdown_proposals WHERE id=?",
                        (proposal_id,)).fetchone()
        if not row:
            return {"error": "unknown proposal", "proposal_id": proposal_id}
        proposal = _breakdown_proposal_row(row)
    if proposal["status"] != "proposed":
        return {"error": "proposal is not pending approval", "proposal_id": proposal_id,
                "status": proposal["status"]}
    deliverable_id = proposal["deliverable_id"]
    payload = proposal.get("payload") or {}
    created_tasks: List[Dict[str, Any]] = []
    linked_tasks: List[Dict[str, Any]] = []
    for milestone in payload.get("milestones") or []:
        milestone_result = add_deliverable_milestone(
            deliverable_id,
            milestone,
            actor=actor,
            project=project,
        )
        if milestone_result.get("error"):
            return milestone_result
        milestone_id = milestone.get("id")
        for item in milestone_result.get("milestones") or []:
            if milestone_id and item.get("id") == milestone_id:
                break
            if item.get("title") == milestone.get("title"):
                milestone_id = item.get("id")
                break
        if not milestone_id:
            return {"error": "failed to resolve created milestone id",
                    "milestone_title": milestone.get("title")}
        for task_spec in milestone.get("tasks") or []:
            task_project = task_spec["project_id"]
            action = task_spec.get("action") or "create"
            if action == "link":
                task_id = task_spec["task_id"]
            else:
                created = _store_facade().create_task({
                    "workstream_id": task_spec["workstream_id"],
                    "workstream_name": task_spec.get("workstream_name"),
                    "title": task_spec["title"],
                    "description": task_spec.get("description"),
                    "owner_org": task_spec.get("owner_org"),
                    "owner_person_or_role": task_spec.get("owner_person_or_role"),
                    "assignee": task_spec.get("assignee"),
                    "phase": task_spec.get("phase"),
                    "status": task_spec.get("status") or "Not Started",
                    "depends_on": task_spec.get("depends_on") or [],
                }, actor=actor, project=task_project)
                if not created:
                    return {"error": "failed to create proposed task",
                            "project_id": task_project,
                            "workstream_id": task_spec["workstream_id"],
                            "title": task_spec["title"]}
                task_id = created["task_id"]
                created_tasks.append({
                    "project_id": task_project,
                    "task_id": task_id,
                    "milestone_id": milestone_id,
                    "action": "create",
                })
            link_result = link_task_to_deliverable(
                deliverable_id,
                task_project,
                task_id,
                milestone_id=milestone_id,
                data={
                    "role": task_spec.get("role") or "contributes",
                    "blocks_deliverable": bool(task_spec.get("blocks_deliverable")),
                    "proof_required": task_spec.get("proof_required") or {},
                    "metadata": task_spec.get("metadata") or {},
                },
                actor=actor,
                project=project,
                run_closure=False,
            )
            if link_result.get("error"):
                return link_result
            if action == "link":
                linked_tasks.append({
                    "project_id": task_project,
                    "task_id": task_id,
                    "milestone_id": milestone_id,
                    "action": "link",
                })
    deliverable_existing = get_deliverable(deliverable_id, project=project) or {}
    deliverable_patch: Dict[str, Any] = {
        "id": deliverable_id,
        "title": deliverable_existing.get("title") or deliverable_id,
    }
    if payload.get("acceptance_criteria"):
        deliverable_patch["acceptance_criteria"] = payload["acceptance_criteria"]
    if payload.get("policy_constraints"):
        merged_policy = dict(deliverable_existing.get("policy_constraints") or {})
        merged_policy.update(payload["policy_constraints"])
        deliverable_patch["policy_constraints"] = merged_policy
    if payload.get("outcome"):
        deliverable_patch["end_state"] = payload["outcome"]
    if len(deliverable_patch) > 2:
        patched = create_deliverable(deliverable_patch, actor=actor, project=project)
        if patched.get("error"):
            return patched
    # Pull in any not-Done transitive dependency of the materialized tasks that the
    # breakdown didn't name, so an approved deliverable can't ship missing its blockers.
    dependency_closure = _ensure_deliverable_dependency_closure(
        deliverable_id, project, actor=actor)
    now = time.time()
    with _store_facade()._conn(project) as c:
        c.execute(
            "UPDATE deliverable_breakdown_proposals "
            "SET status='approved', approved_by=?, reviewed_by=?, approved_at=?, updated_at=? "
            "WHERE id=?",
            (actor, actor, now, now, proposal_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.breakdown_approved",
                   json.dumps({"deliverable_id": deliverable_id, "proposal_id": proposal_id,
                               "created_task_count": len(created_tasks),
                               "linked_task_count": len(linked_tasks),
                               "auto_linked_dependency_count":
                                   dependency_closure.get("auto_linked_count", 0)},
                              sort_keys=True), now))
    return {
        "schema": "switchboard.deliverable_breakdown_approval.v1",
        "project_id": project,
        "proposal_id": proposal_id,
        "deliverable_id": deliverable_id,
        "created_tasks": created_tasks,
        "linked_tasks": linked_tasks,
        "dependency_closure": dependency_closure,
        "deliverable": get_deliverable(deliverable_id, project=project),
        "mission_status": get_mission_status(project=project, deliverable_id=deliverable_id),
    }


def _resolve_mission_deliverable(project: str, deliverable_id: str = "",
                               board_id: str = "", mission_id: str = "",
                               include_task_snapshots: bool = True) -> Dict[str, Any]:
    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    deliverable_id = (deliverable_id or "").strip()
    board_id = (board_id or mission_id or "").strip()
    if deliverable_id:
        deliverable = get_deliverable(deliverable_id, project=project,
                                      include_task_snapshots=include_task_snapshots)
        if not deliverable:
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id,
                    "project_id": project}
        return {"deliverable": deliverable, "board_id": deliverable.get("board_id")}
    if board_id:
        board = get_project_board(board_id, project=project)
        if not board:
            return {"error": "unknown board", "board_id": board_id, "project_id": project}
        deliverables = list_deliverables(project=project, board_id=board_id,
                                         include_task_snapshots=include_task_snapshots)
        if not deliverables:
            return {"error": "no deliverable for board", "board_id": board_id,
                    "project_id": project, "board": board}
        if len(deliverables) > 1:
            return {
                "error": "multiple deliverables for board; pass deliverable_id",
                "board_id": board_id,
                "project_id": project,
                "board": board,
                "deliverable_ids": [d["id"] for d in deliverables],
            }
        return {"deliverable": deliverables[0], "board": board, "board_id": board_id}
    return {"error": "deliverable_id or board_id/mission_id is required", "project_id": project}


def _registry_project_ids() -> List[str]:
    _store_facade().init_project_registry()
    with _store_facade()._registry_conn() as c:
        rows = c.execute("SELECT id FROM projects ORDER BY id").fetchall()
    return [r["id"] for r in rows if _store_facade().has_project(r["id"])]


def _find_deliverable_links_for_task(task_project: str, task_id: str,
                                     mission_project: str = "",
                                     deliverable_id: str = "") -> List[Dict[str, Any]]:
    """Return deliverable links for claim/mission rollup using the same scan as task detail."""
    links = list_task_deliverable_links(task_id, project=task_project)
    mission_project = (mission_project or "").strip()
    deliverable_id = (deliverable_id or "").strip()
    if mission_project:
        links = [link for link in links
                 if (link.get("deliverable_home_project") or "") == mission_project]
    if deliverable_id:
        links = [link for link in links if (link.get("deliverable_id") or "") == deliverable_id]
    for link in links:
        link["mission_project"] = link.get("deliverable_home_project")
    return links


def _enriched_mission_task_link(link: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(link)
    task_project = link.get("project_id")
    task_id = link.get("task_id")
    if not _store_facade().has_project(task_project):
        enriched["task_detail"] = {"error": "unknown project", "project_id": task_project}
        return enriched
    task = _store_facade().get_task(task_id, project=task_project)
    if not task:
        enriched["task_detail"] = {"error": "unknown task", "project_id": task_project,
                                   "task_id": task_id}
        return enriched
    with _store_facade()._conn(task_project) as c:
        claims = _store_facade()._active_task_claims_in(c, task_id)
    enriched["task_detail"] = {
        "task_id": task["task_id"],
        "title": task.get("title"),
        "status": task.get("status"),
        "assignee": task.get("assignee"),
        "workstream": task.get("_wsId"),
        "depends_on": task.get("depends_on") or [],
        "dependency_state": task.get("dependency_state"),
        "provenance": task.get("provenance"),
        "git_state": task.get("git_state"),
        "external_ci": task.get("external_ci"),
        "publication": task.get("publication"),
        "human_gate": _store_facade()._task_human_gate_state(task),
        "session_health": task.get("session_health"),
        "active_claims": claims,
        # CEO-voice summary for map-node hover tooltips. narration is None while a live task
        # is transiently stale; narration_raw keeps the last prose so the tooltip still shows.
        "narration": task.get("narration"),
        "narration_raw": task.get("narration_raw"),
        "narration_stale": (task.get("narration_state") or {}).get("stale"),
    }
    return enriched


def _mission_blockers(deliverable: Dict[str, Any],
                      linked_tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    blockers: List[Dict[str, Any]] = []
    if deliverable.get("status") == "blocked":
        blockers.append({
            "kind": "deliverable_blocked",
            "deliverable_id": deliverable.get("id"),
            "message": "Deliverable status is blocked",
        })
    for link in linked_tasks:
        detail = link.get("task_detail") or link.get("task") or {}
        if detail.get("error"):
            blockers.append({
                "kind": "missing_task_snapshot",
                "project_id": link.get("project_id"),
                "task_id": link.get("task_id"),
                "message": detail.get("error"),
            })
            continue
        if detail.get("status") == "Blocked":
            blockers.append({
                "kind": "task_blocked",
                "project_id": link.get("project_id"),
                "task_id": detail.get("task_id"),
                "title": detail.get("title"),
                "blocks_deliverable": bool(link.get("blocks_deliverable")),
            })
        dep = detail.get("dependency_state") or {}
        if not dep.get("satisfied"):
            for blocking in dep.get("blocking") or []:
                blockers.append({
                    "kind": "dependency_unsatisfied",
                    "project_id": link.get("project_id"),
                    "task_id": detail.get("task_id"),
                    "title": detail.get("title"),
                    "blocking_task_id": blocking.get("task_id"),
                    "blocking_status": blocking.get("status"),
                })
        gate = detail.get("human_gate") or {}
        if gate.get("blocked"):
            blockers.append({
                "kind": "human_gate",
                "project_id": link.get("project_id"),
                "task_id": detail.get("task_id"),
                "title": detail.get("title"),
                "reason": gate.get("reason"),
            })
        session_health = detail.get("session_health") or {}
        if session_health.get("status") == "unsafe":
            for finding in session_health.get("findings") or []:
                if not finding.get("blocking"):
                    continue
                blockers.append({
                    "kind": "unsafe_session",
                    "project_id": link.get("project_id"),
                    "task_id": detail.get("task_id"),
                    "title": detail.get("title"),
                    "failure_class": finding.get("failure_class"),
                    "finding_code": finding.get("code"),
                    "work_session_id": finding.get("work_session_id"),
                    "severity": finding.get("severity"),
                    "message": finding.get("message"),
                    "repair": finding.get("repair"),
                })
        proof_required = link.get("proof_required") or {}
        external_ci = detail.get("external_ci") or {}
        if (proof_required.get("external_ci_passed")
                or (external_ci.get("gate") or {}).get("required")):
            if not external_ci.get("passed"):
                blockers.append({
                    "kind": "external_ci",
                    "project_id": link.get("project_id"),
                    "task_id": detail.get("task_id"),
                    "title": detail.get("title"),
                })
        publication = detail.get("publication") or {}
        if (proof_required.get("publication_evidence")
                or proof_required.get("public_mirror_published")
                or (publication.get("gate") or {}).get("required")):
            if not publication.get("passed"):
                blockers.append({
                    "kind": "publication_evidence",
                    "project_id": link.get("project_id"),
                    "task_id": detail.get("task_id"),
                    "title": detail.get("title"),
                })
        if link.get("blocks_deliverable"):
            provenance = detail.get("provenance") or {}
            if not (detail.get("status") == "Done" and provenance.get("terminal")):
                blockers.append({
                    "kind": "blocking_task_incomplete",
                    "project_id": link.get("project_id"),
                    "task_id": detail.get("task_id"),
                    "title": detail.get("title"),
                    "status": detail.get("status"),
                })
    return blockers


# Every next-action carries an OWNER and an ATTENTION model so the UI can stop presenting agent
# housekeeping, coordinator automation, and human decisions as one undifferentiated to-do list.
#   owner_type       who acts: agent | coordinator | reviewer | project_owner
#   attention        True only when a HUMAN with authority must decide (→ "Decisions needed from you")
#   automatic        True when the control plane handles it without anyone lifting a finger
#   delivery_impact  none | at_risk | blocking — does the deliverable actually suffer if untouched?
#   label            plain-English imperative for humans (the raw `action` verb stays for machines)
def _action(action, *, owner, label, reason, attention=False, automatic=False,
            delivery_impact="none", **extra):
    a = {"action": action, "owner_type": owner, "label": label, "reason": reason,
         "attention": bool(attention), "automatic": bool(automatic),
         "delivery_impact": delivery_impact}
    a.update({k: v for k, v in extra.items() if v is not None})
    return a


def _task_blocks_others(detail: Dict[str, Any]) -> bool:
    if detail.get("is_blocking"):
        return True
    return bool((detail.get("dependency_state") or {}).get("blocking"))


def _mission_next_actions(deliverable: Dict[str, Any],
                          linked_tasks: List[Dict[str, Any]],
                          pending_proposal: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    if pending_proposal and pending_proposal.get("status") == "proposed":
        actions.append(_action(
            "approve_breakdown", owner="project_owner", attention=True, delivery_impact="at_risk",
            label="Approve the proposed breakdown",
            reason="A milestone/task breakdown is waiting for your approval",
            proposal_id=pending_proposal.get("id")))
    for link in linked_tasks:
        detail = link.get("task_detail") or {}
        if detail.get("error"):
            actions.append(_action(
                "repair_task_link", owner="coordinator", delivery_impact="at_risk",
                label="Repair a broken task link", reason=detail.get("error"),
                project_id=link.get("project_id"), task_id=link.get("task_id")))
            continue
        status = detail.get("status")
        claims = detail.get("active_claims") or []
        dep = detail.get("dependency_state") or {}
        blocks = _task_blocks_others(detail)
        if status == "Not Started" and dep.get("ready") and not claims:
            actions.append(_action(
                "claim_task", owner="agent", automatic=True,
                delivery_impact="blocking" if blocks else "none",
                label="Agent will claim a ready task", reason="Ready and unclaimed",
                project_id=link.get("project_id"), task_id=detail.get("task_id"),
                title=detail.get("title")))
        elif status == "In Review":
            actions.append(_action(
                "verify_merge_provenance", owner="coordinator", automatic=True,
                delivery_impact="none",
                label="Coordinator will verify merge provenance",
                reason="Awaiting merge/default-branch provenance for Done",
                project_id=link.get("project_id"), task_id=detail.get("task_id"),
                title=detail.get("title")))
        elif status == "In Progress" and not claims:
            actions.append(_action(
                "resume_or_claim", owner="agent", automatic=True,
                delivery_impact="at_risk" if blocks else "none",
                label="Agent will resume dropped work",
                reason="In progress without an active claim",
                project_id=link.get("project_id"), task_id=detail.get("task_id"),
                title=detail.get("title")))
        gate = detail.get("human_gate") or {}
        if gate.get("blocked"):
            actions.append(_action(
                "request_human_approval", owner="project_owner", attention=True,
                delivery_impact="blocking",
                label="Approve to unblock a gated task",
                reason=gate.get("reason") or "Human gate blocked",
                project_id=link.get("project_id"), task_id=detail.get("task_id"),
                title=detail.get("title")))
        session_health = detail.get("session_health") or {}
        # A stale/unsafe Work Session is COORDINATOR housekeeping that resolves automatically. It only
        # touches delivery when the underlying task hasn't already merged — an unsafe session on an
        # In Review/Done task (the classic "old blocked session on shipped work") has no impact.
        terminal = status in ("In Review", "Done")
        if session_health.get("status") == "unsafe":
            actions.append(_action(
                "repair_work_session", owner="coordinator", automatic=True,
                delivery_impact="none" if terminal else "at_risk",
                label="Coordinator will clean up an unsafe agent workspace",
                reason=session_health.get("recommended_repair") or "Unsafe Work Session",
                project_id=link.get("project_id"), task_id=detail.get("task_id"),
                title=detail.get("title")))
        elif session_health.get("status") == "warning":
            actions.append(_action(
                "refresh_work_session_health", owner="coordinator", automatic=True,
                delivery_impact="none",
                label="Coordinator will refresh a Work Session's health",
                reason=session_health.get("recommended_repair") or "Work Session warning",
                project_id=link.get("project_id"), task_id=detail.get("task_id"),
                title=detail.get("title")))
    if not linked_tasks and not (deliverable.get("milestones") or []):
        actions.append(_action(
            "propose_breakdown", owner="coordinator", automatic=True, delivery_impact="at_risk",
            label="Propose a milestone/task breakdown",
            reason="No milestones or linked tasks yet"))
    return actions


def get_mission_status(project: str = DEFAULT_PROJECT, deliverable_id: str = "",
                       board_id: str = "", mission_id: str = "") -> Dict[str, Any]:
    """Return a mission cockpit rollup: end state, milestones, proof, blockers, next actions.

    HARDEN-36: the live cockpit polls this on a timer and each build re-fetches every
    linked task via _store_facade().get_task(the enrichment fan-out). We resolve the deliverable once
    WITHOUT per-task snapshots (cheap — just the links) to key a short-TTL cache; a hit
    skips the whole fan-out. The stamp folds in every involved project's task state, so
    any linked-task change (even cross-project) invalidates immediately.
    """
    light = _resolve_mission_deliverable(project, deliverable_id=deliverable_id,
                                         board_id=board_id, mission_id=mission_id,
                                         include_task_snapshots=False)
    if light.get("error"):
        return light
    deliverable = light["deliverable"]
    stamp = _store_facade()._mission_cache_stamp(project, deliverable)
    ident = f"{project}\x00{deliverable.get('id')}"
    return _store_facade().ttl_read_cache(
        "mission_status", ident, stamp,
        lambda: _store_facade()._build_mission_status(
            project, deliverable_id=deliverable_id,
            board_id=board_id, mission_id=mission_id))


def _build_mission_status(project: str = DEFAULT_PROJECT, deliverable_id: str = "",
                          board_id: str = "", mission_id: str = "") -> Dict[str, Any]:
    scope = _resolve_mission_deliverable(project, deliverable_id=deliverable_id,
                                          board_id=board_id, mission_id=mission_id)
    if scope.get("error"):
        return scope
    deliverable = scope["deliverable"]
    board = scope.get("board") or deliverable.get("board")
    metadata = deliverable.get("metadata") or {}
    linked_tasks = [_enriched_mission_task_link(link)
                      for link in (deliverable.get("task_links") or [])]
    milestone_task_counts: Dict[str, int] = {}
    for link in deliverable.get("task_links") or []:
        mid = link.get("milestone_id")
        if mid:
            milestone_task_counts[mid] = milestone_task_counts.get(mid, 0) + 1
    milestones = []
    for milestone in deliverable.get("milestones") or []:
        item = dict(milestone)
        item["linked_task_count"] = milestone_task_counts.get(milestone.get("id"), 0)
        milestones.append(item)
    active_work = []
    done_with_proof = []
    active_agents: Dict[str, Dict[str, Any]] = {}
    for link in linked_tasks:
        detail = link.get("task_detail") or {}
        if detail.get("error"):
            continue
        status = detail.get("status")
        claims = detail.get("active_claims") or []
        provenance = detail.get("provenance") or {}
        if status == "Done" and provenance.get("terminal"):
            done_with_proof.append({
                "project_id": link.get("project_id"),
                "task_id": detail.get("task_id"),
                "title": detail.get("title"),
                "provenance": provenance,
                "git_state": detail.get("git_state"),
            })
        elif status in ("In Progress", "In Review") or claims:
            active_work.append({
                "project_id": link.get("project_id"),
                "task_id": detail.get("task_id"),
                "title": detail.get("title"),
                "status": status,
                "assignee": detail.get("assignee"),
                "active_claims": claims,
                "session_health": detail.get("session_health"),
                "milestone_id": link.get("milestone_id"),
                "role": link.get("role"),
            })
        for claim in claims:
            agent_id = claim.get("agent_id")
            if agent_id and agent_id not in active_agents:
                active_agents[agent_id] = {
                    "agent_id": agent_id,
                    "claim_id": claim.get("claim_id"),
                    "task_id": detail.get("task_id"),
                    "project_id": link.get("project_id"),
                }
    # Enrich each active agent with its advertised runtime/platform + model, so map-node
    # hover tooltips can show WHO (and on which platform) is working the task.
    if active_agents:
        with _store_facade()._conn(project) as c:
            for agent_id, info in active_agents.items():
                prow = c.execute("SELECT * FROM agent_presence WHERE agent_id=?",
                                 (agent_id,)).fetchone()
                if prow:
                    pres = _store_facade()._presence_row(prow)
                    info["runtime"] = pres.get("runtime")
                    info["model"] = pres.get("model")
                    info["stale"] = pres.get("stale")
    pending_proposal = None
    with _store_facade()._conn(project) as c:
        row = c.execute(
            "SELECT * FROM deliverable_breakdown_proposals "
            "WHERE deliverable_id=? AND status='proposed' "
            "ORDER BY updated_at DESC LIMIT 1",
            (deliverable.get("id"),),
        ).fetchone()
        if row:
            pending_proposal = _breakdown_proposal_row(row)
    blockers = _mission_blockers(deliverable, linked_tasks)
    economics = deliverable_tally(deliverable.get("id"), project=project)
    result = {
        "schema": "switchboard.mission_status.v1",
        "project_id": project,
        "board_id": scope.get("board_id") or deliverable.get("board_id"),
        "mission_id": scope.get("board_id") or deliverable.get("board_id"),
        "deliverable_id": deliverable.get("id"),
        "board": board,
        "deliverable": {
            "id": deliverable.get("id"),
            "title": deliverable.get("title"),
            "status": deliverable.get("status"),
            "end_state": deliverable.get("end_state") or (board or {}).get("end_state"),
            "why_it_matters": deliverable.get("why_it_matters"),
            "acceptance_criteria": deliverable.get("acceptance_criteria"),
            "policy_constraints": deliverable.get("policy_constraints"),
            "proof_requirements": deliverable.get("proof_requirements"),
        },
        "narrative": metadata.get("narrative"),
        "narrative_updated_at": metadata.get("narrative_updated_at"),
        "progress": deliverable.get("progress") or deliverable_progress(deliverable),
        "milestones": milestones,
        "linked_tasks": linked_tasks,
        "blockers": blockers,
        "active_work": active_work,
        "done_with_proof": done_with_proof,
        "active_agents": list(active_agents.values()),
        "pending_proposal": pending_proposal,
        "next_actions": _mission_next_actions(deliverable, linked_tasks, pending_proposal),
        "economics": economics if not economics.get("error") else economics,
    }
    return _attach_mission_brief_fields(result, project=project)


def get_deliverable_dependency_graph(project: str = DEFAULT_PROJECT, deliverable_id: str = "",
                                     board_id: str = "", mission_id: str = "") -> Dict[str, Any]:
    """Return task-level depends_on graph for a deliverable (strategic map layer).

    The map only needs title/status/depends_on/workstream/provenance per task. It used to
    resolve the deliverable WITH full task snapshots and call the heavy get_task ~once per
    node/edge (session_health/external_ci/publication gates and all) — helm-vulkan (144
    links) took ~23s AND, because the endpoint ran on the event loop, froze every other
    request (boards, /health) for that whole time. We now resolve without snapshots and
    serve every lookup from a lazy per-project index (raw row + git provenance), which
    produces byte-identical graphs in ~0.2s.
    """
    scope = _resolve_mission_deliverable(project, deliverable_id=deliverable_id,
                                          board_id=board_id, mission_id=mission_id,
                                          include_task_snapshots=False)
    if scope.get("error"):
        return scope
    deliverable = scope["deliverable"]
    deliverable_id = deliverable.get("id") or deliverable_id
    # HARDEN-36: the mission map polls this on a timer; cache the built graph keyed
    # by every involved project's task state (the resolve above is already the cheap,
    # snapshot-free one, so a hit skips only the index build + graph walk).
    stamp = _store_facade()._mission_cache_stamp(project, deliverable)
    ident = f"{project}\x00{deliverable_id}"
    return _store_facade().ttl_read_cache(
        "dep_graph", ident, stamp,
        lambda: _store_facade()._build_deliverable_dependency_graph(
            project, deliverable, deliverable_id))


def _build_deliverable_dependency_graph(project: str, deliverable: Dict[str, Any],
                                        deliverable_id: str) -> Dict[str, Any]:
    import mission_graph

    _indexes: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def _project_index(proj: str) -> Dict[str, Dict[str, Any]]:
        if proj not in _indexes:
            index: Dict[str, Dict[str, Any]] = {}
            if _store_facade().has_project(proj):
                with _store_facade()._conn(proj) as c:
                    for r in c.execute("SELECT * FROM tasks").fetchall():
                        t = _task_row(r)
                        t["provenance"] = _store_facade()._provenance_summary(_store_facade()._load_git_state(c, t["task_id"]))
                        index[(t.get("task_id") or "").strip().upper()] = t
            _indexes[proj] = index
        return _indexes[proj]

    def _light_detail(proj: str, task_id: str) -> Optional[Dict[str, Any]]:
        hit = _project_index(proj).get((task_id or "").strip().upper())
        if not hit:
            return None
        return {
            "task_id": hit.get("task_id"),
            "title": hit.get("title"),
            "status": hit.get("status"),
            "workstream": hit.get("_wsId"),
            "_wsId": hit.get("_wsId"),
            "depends_on": hit.get("depends_on") or [],
            "provenance": hit.get("provenance"),
        }

    linked_tasks = []
    for link in (deliverable.get("task_links") or []):
        task_project = link.get("project_id") or project
        enriched = dict(link)
        if not _store_facade().has_project(task_project):
            enriched["task_detail"] = {"error": "unknown project", "project_id": task_project}
        else:
            detail = _light_detail(task_project, link.get("task_id"))
            enriched["task_detail"] = detail or {
                "error": "unknown task", "project_id": task_project,
                "task_id": link.get("task_id"),
            }
        linked_tasks.append(enriched)

    def _lookup(task_project: str, task_id: str, fallback: bool = False) -> Optional[Dict[str, Any]]:
        proj = project if fallback else (task_project or project)
        detail = _light_detail(proj, task_id)
        if not detail:
            return None
        detail = dict(detail)
        detail["_project_id"] = proj
        return detail

    return mission_graph.build_dependency_graph(
        linked_tasks,
        deliverable_id=deliverable_id,
        project_id=project,
        task_lookup=_lookup,
    )


def _deliverable_activity(project: str, deliverable_id: str, limit: int = 12) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with _store_facade()._conn(project) as c:
        for row in c.execute(
            "SELECT actor, kind, payload, created_at FROM activity "
            "WHERE kind LIKE 'deliverable.%' ORDER BY created_at DESC LIMIT ?",
            (max(limit * 8, 40),),
        ).fetchall():
            payload = _store_facade()._json_payload(row["payload"])
            if isinstance(payload, dict) and payload.get("deliverable_id") not in (
                None, deliverable_id,
            ):
                continue
            rows.append({
                "actor": row["actor"],
                "kind": row["kind"],
                "payload": payload,
                "created_at": row["created_at"],
            })
            if len(rows) >= limit:
                break
    return rows


def _attach_mission_brief_fields(mission_status: Dict[str, Any],
                                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    if mission_status.get("error"):
        return mission_status
    import mission_narrative

    deliverable_id = mission_status.get("deliverable_id") or ""
    deliverable = get_deliverable(deliverable_id, project=project) if deliverable_id else None
    metadata = (deliverable or {}).get("metadata") or {}
    stored_brief = metadata.get("generated_brief") or {}
    mission_status["mission_brief"] = stored_brief or None
    mission_status["narrative_state"] = mission_narrative.narrative_state(
        mission_status, metadata=metadata, stored_brief=stored_brief)
    mission_status["brief_generated_at"] = metadata.get("brief_generated_at")
    mission_status["narrative_source"] = metadata.get("narrative_source")
    # NARRATE-3: CEO-voice header, rewritten from the structured brief. Stale when the current
    # mission fingerprint no longer matches the one it was written from (same discipline as the
    # generated brief). See docs/CEO-NARRATOR-CONTRACT.md.
    ceo_text = metadata.get("ceo_narrative")
    if ceo_text:
        current_fp = mission_narrative.brief_source_fingerprint(mission_status)
        stored_fp = metadata.get("ceo_narrative_fingerprint")
        ceo_stale = bool(stored_fp) and stored_fp != current_fp
        mission_status["ceo_narrative_state"] = {
            "stale": ceo_stale,
            "source_fingerprint": current_fp,
            "stored_fingerprint": stored_fp,
            "message": ("CEO narration is regenerating; trust mission_status and provenance."
                        if ceo_stale else None),
        }
        mission_status["ceo_narrative"] = None if ceo_stale else ceo_text
        if ceo_stale:
            mission_status["ceo_narrative_raw"] = ceo_text
        mission_status["ceo_narrative_generated_at"] = metadata.get("ceo_narrative_generated_at")
    return mission_status


def generate_mission_brief(project: str = DEFAULT_PROJECT, deliverable_id: str = "",
                           board_id: str = "", mission_id: str = "",
                           actor: str = "system", persist: bool = True) -> Dict[str, Any]:
    """Generate a structured mission brief from durable events and optionally persist it."""
    import mission_narrative

    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    status = get_mission_status(project=project, deliverable_id=deliverable_id,
                                board_id=board_id, mission_id=mission_id)
    if status.get("error"):
        return status
    deliverable_id = status.get("deliverable_id") or deliverable_id
    activity = _deliverable_activity(project, deliverable_id)
    brief = mission_narrative.build_mission_brief(status, recent_activity=activity)
    narrative_state = mission_narrative.narrative_state(status, stored_brief=brief)
    result = {
        "schema": "switchboard.mission_brief_result.v1",
        "project_id": project,
        "deliverable_id": deliverable_id,
        "mission_brief": brief,
        "narrative_state": narrative_state,
        "mission_status": status,
    }
    if not persist:
        return result
    now = time.time()
    with _store_facade()._conn(project) as c:
        row = c.execute("SELECT metadata_json FROM deliverables WHERE id=?",
                        (deliverable_id,)).fetchone()
        if not row:
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        metadata = _store_facade()._json_payload(row["metadata_json"])
        metadata["generated_brief"] = brief
        metadata["brief_generated_at"] = now
        metadata["brief_generated_by"] = actor
        metadata["brief_fingerprint"] = brief.get("source_fingerprint")
        metadata["narrative"] = brief.get("summary_markdown")
        metadata["narrative_updated_at"] = now
        metadata["narrative_updated_by"] = actor
        metadata["narrative_source"] = "generated"
        c.execute(
            "UPDATE deliverables SET metadata_json=?, updated_at=? WHERE id=?",
            (json.dumps(metadata, sort_keys=True), now, deliverable_id),
        )
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.brief_generated",
                   json.dumps({"deliverable_id": deliverable_id,
                               "source_fingerprint": brief.get("source_fingerprint")},
                              sort_keys=True), now))
    result["mission_status"] = get_mission_status(
        project=project, deliverable_id=deliverable_id)
    return result


def set_deliverable_narration(deliverable_id: str, narration: str, source_fingerprint: str = "",
                              model: str = "", actor: str = "narrator",
                              project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """NARRATE-3: persist the CEO-voice header for a deliverable in its metadata. Stored under
    ceo_narrative* keys, kept separate from the structured `generated_brief`/`narrative` so the
    two never clobber each other."""
    now = time.time()
    with _store_facade()._conn(project) as c:
        row = c.execute("SELECT metadata_json FROM deliverables WHERE id=?",
                        (deliverable_id,)).fetchone()
        if not row:
            return {"error": "unknown deliverable", "deliverable_id": deliverable_id}
        metadata = _store_facade()._json_payload(row["metadata_json"])
        metadata["ceo_narrative"] = narration
        metadata["ceo_narrative_fingerprint"] = source_fingerprint
        metadata["ceo_narrative_generated_at"] = now
        metadata["ceo_narrative_model"] = model
        metadata["ceo_narrative_by"] = actor
        c.execute("UPDATE deliverables SET metadata_json=?, updated_at=? WHERE id=?",
                  (json.dumps(metadata, sort_keys=True), now, deliverable_id))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.ceo_narrated",
                   json.dumps({"deliverable_id": deliverable_id,
                               "source_fingerprint": source_fingerprint}, sort_keys=True), now))
    return {"deliverable_id": deliverable_id, "ceo_narrative": narration,
            "source_fingerprint": source_fingerprint, "generated_at": now}


def run_mission_coordinator_tick(project: str = DEFAULT_PROJECT, deliverable_id: str = "",
                               board_id: str = "", mission_id: str = "",
                               coordinator_agent_id: str = "", actor: str = "system",
                               idem_key: str = "", policy: Any = None) -> Dict[str, Any]:
    """Run one deliverable-scoped coordinator tick: brief refresh, dispatch, or escalation."""
    import mission_coordinator

    if not _store_facade().has_project(project):
        return {"error": f"unknown project: {project}"}
    policy_obj = _store_facade()._parse_jsonish(policy) if policy not in (None, "") else None
    if policy_obj is not None and not isinstance(policy_obj, dict):
        return {"error": "policy must be a JSON object"}
    payload = {
        "deliverable_id": (deliverable_id or "").strip(),
        "board_id": (board_id or "").strip(),
        "mission_id": (mission_id or "").strip(),
        "coordinator_agent_id": (coordinator_agent_id or "").strip(),
        "policy": policy_obj or {},
    }
    with _store_facade()._conn(project) as c:
        hit = _store_facade()._idem_hit(c, "run_mission_coordinator_tick", idem_key, actor, payload)
        if hit is not None:
            return hit
        status = get_mission_status(
            project=project, deliverable_id=deliverable_id,
            board_id=board_id, mission_id=mission_id)
        if status.get("error"):
            _store_facade()._idem_store(c, "run_mission_coordinator_tick", idem_key, actor, payload, status)
            return status
        resolved_id = status.get("deliverable_id") or deliverable_id
        result = mission_coordinator.run_coordinator_tick(
            status,
            mission_project=project,
            coordinator_agent_id=coordinator_agent_id,
            actor=actor,
            policy=policy_obj,
            idem_key=idem_key,
        )
        now = time.time()
        c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (None, actor, "deliverable.coordinator_tick",
             json.dumps({
                 "schema": result.get("schema"),
                 "deliverable_id": resolved_id,
                 "coordinator_agent_id": coordinator_agent_id or None,
                 "status": result.get("status"),
                 "plan": result.get("plan"),
                 "executed": result.get("executed"),
                 "escalations": result.get("escalations"),
                 "dispatch": {
                     "claimed": bool((result.get("dispatch") or {}).get("claimed")),
                     "claim_id": (result.get("dispatch") or {}).get("claim_id"),
                     "task_id": ((result.get("dispatch") or {}).get("task") or {}).get("task_id"),
                 } if result.get("dispatch") else None,
                 "decision_id": result.get("decision_id"),
             }, sort_keys=True), now))
        result["mission_status"] = get_mission_status(
            project=project, deliverable_id=resolved_id)
        _store_facade()._idem_store(c, "run_mission_coordinator_tick", idem_key, actor, payload, result)
        return result


def _empty_economics_totals() -> Dict[str, Any]:
    return {
        "linked_task_count": 0,
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


def _finalize_economics_totals(totals: Dict[str, Any]) -> Dict[str, Any]:
    if totals["verified_outcomes"]:
        totals["unit_cost"]["cost_per_verified_outcome"] = round(
            totals["spend"]["cost_usd"] / totals["verified_outcomes"], 6)
    if totals["verified_kpi_contribution"]:
        totals["unit_cost"]["cost_per_kpi_contribution_unit"] = round(
            totals["spend"]["cost_usd"] / totals["verified_kpi_contribution"], 6)
    return totals


def _merge_kpi_group(target: Dict[str, Dict[str, Any]], tally: Dict[str, Any],
                     project_id: str) -> None:
    spend = tally.get("spend") or {}
    for group in tally.get("kpis") or []:
        kpi_id = group.get("kpi_id")
        if not kpi_id:
            continue
        key = f"{project_id}:{kpi_id}"
        entry = target.setdefault(key, {
            "project_id": project_id,
            "kpi_id": kpi_id,
            "name": group.get("name"),
            "unit": group.get("unit"),
            "direction": group.get("direction"),
            "verified_contribution": 0.0,
            "spend": {"cost_usd": 0.0, "total_tokens": 0, "by_source": {}, "by_model": {}},
            "unit_cost": {"cost_per_contribution_unit": None},
            "links": [],
        })
        entry["verified_contribution"] = round(
            entry["verified_contribution"] + float(group.get("verified_contribution") or 0.0), 6)
        _store_facade()._merge_spend_totals(entry["spend"], spend)
        entry["links"].extend(group.get("links") or [])


def deliverable_tally(deliverable_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Aggregate Tally economics across all tasks linked to a deliverable/mission.

    Proven spend (Done + terminal provenance) is separated from in-flight In Review / In Progress
    spend so mission operators can see cost-to-outcome for merged work vs unproven spend.
    """
    deliverable = get_deliverable(deliverable_id, project=project, include_task_snapshots=False)
    if not deliverable:
        return {"error": "unknown deliverable", "deliverable_id": deliverable_id,
                "project_id": project}
    combined = _empty_economics_totals()
    proven = _empty_economics_totals()
    in_review = _empty_economics_totals()
    by_milestone: Dict[str, Dict[str, Any]] = {}
    by_task: List[Dict[str, Any]] = []
    kpi_index: Dict[str, Dict[str, Any]] = {}
    milestone_titles = {m.get("id"): m.get("title")
                          for m in (deliverable.get("milestones") or [])}

    for link in deliverable.get("task_links") or []:
        task_project = link.get("project_id")
        task_id = link.get("task_id")
        milestone_id = link.get("milestone_id") or ""
        if not _store_facade().has_project(task_project):
            continue
        task = _store_facade().get_task(task_id, project=task_project)
        if not task:
            continue
        tally = _store_facade().task_tally(task_id, project=task_project)
        proof_bucket = _store_facade()._task_proof_bucket(task)
        _store_facade()._merge_task_tally_into_totals(combined, tally)
        if proof_bucket == "proven":
            _store_facade()._merge_task_tally_into_totals(proven, tally)
        elif proof_bucket in ("in_review", "active"):
            _store_facade()._merge_task_tally_into_totals(in_review, tally)
        _merge_kpi_group(kpi_index, tally, task_project)

        ms = by_milestone.setdefault(milestone_id or "__unassigned__", {
            "milestone_id": milestone_id or None,
            "title": milestone_titles.get(milestone_id) or ("Unassigned" if not milestone_id else milestone_id),
            "combined": _empty_economics_totals(),
            "proven": _empty_economics_totals(),
            "in_review": _empty_economics_totals(),
            "by_task": [],
        })
        _store_facade()._merge_task_tally_into_totals(ms["combined"], tally)
        if proof_bucket == "proven":
            _store_facade()._merge_task_tally_into_totals(ms["proven"], tally)
        elif proof_bucket in ("in_review", "active"):
            _store_facade()._merge_task_tally_into_totals(ms["in_review"], tally)
        task_row = {
            "project_id": task_project,
            "task_id": task_id,
            "title": task.get("title"),
            "status": task.get("status"),
            "proof_bucket": proof_bucket,
            "milestone_id": milestone_id or None,
            "role": link.get("role"),
            "spend": tally.get("spend") or {},
            "outcomes": tally.get("outcomes") or {},
            "unit_cost": tally.get("unit_cost") or {},
            "verified_kpi_contribution": round(sum(
                float(k.get("verified_contribution") or 0.0)
                for k in (tally.get("kpis") or [])), 6),
            "kpis": tally.get("kpis") or [],
        }
        by_task.append(task_row)
        ms["by_task"].append(task_row)

    for bucket in (combined, proven, in_review):
        _finalize_economics_totals(bucket)
    milestone_rows = []
    for ms in by_milestone.values():
        for key in ("combined", "proven", "in_review"):
            _finalize_economics_totals(ms[key])
        milestone_rows.append(ms)
    milestone_rows.sort(key=lambda x: (x.get("milestone_id") is None,
                                      x.get("title") or ""))
    kpis = []
    for entry in kpi_index.values():
        if entry["verified_contribution"]:
            entry["unit_cost"]["cost_per_contribution_unit"] = round(
                entry["spend"]["cost_usd"] / entry["verified_contribution"], 6)
        kpis.append(entry)
    kpis.sort(key=lambda x: (-float(x["spend"]["cost_usd"] or 0.0),
                             x.get("project_id") or "", x.get("kpi_id") or ""))
    by_task.sort(key=lambda x: (-float((x.get("spend") or {}).get("cost_usd") or 0.0),
                                x.get("project_id") or "", x.get("task_id") or ""))

    return {
        "schema": "switchboard.deliverable_tally.v1",
        "project_id": project,
        "deliverable_id": deliverable_id,
        "board_id": deliverable.get("board_id"),
        "totals": {
            "combined": combined,
            "proven": proven,
            "in_review": in_review,
        },
        "by_milestone": milestone_rows,
        "by_task": by_task,
        "kpis": kpis,
    }




def list_task_deliverable_links(task_id: str, project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """Return deliverable links for one task, including cross-project mission rollups.

    Link rows live in the deliverable home project database, so this scans every routable
    project for rows matching the explicit task_project + task_id pair.
    """
    tid = (task_id or "").strip().upper()
    task_project = (project or DEFAULT_PROJECT).strip()
    if not tid or not _store_facade().has_project(task_project):
        return []
    links: List[Dict[str, Any]] = []
    seen: set = set()
    query = (
        """SELECT l.*, d.title AS deliverable_title, d.status AS deliverable_status
           FROM deliverable_task_links l
           JOIN deliverables d ON d.id = l.deliverable_id
           WHERE l.task_id=? AND l.project_id=?
           ORDER BY l.updated_at DESC, l.id"""
    )
    for deliverable_project in _store_facade().project_ids():
        if not _store_facade().has_project(deliverable_project):
            continue
        with _store_facade()._conn(deliverable_project) as c:
            try:
                rows = c.execute(query, (tid, task_project)).fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                link_id = row["id"]
                dedupe = (deliverable_project, link_id)
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                link = _deliverable_link_row(row)
                link["deliverable_home_project"] = deliverable_project
                link["deliverable_title"] = row["deliverable_title"]
                link["deliverable_status"] = row["deliverable_status"]
                if link.get("board_id"):
                    board_row = c.execute("SELECT * FROM project_boards WHERE id=?",
                                          (link["board_id"],)).fetchone()
                    link["board"] = (_project_board_row(board_row, project=deliverable_project)
                                     if board_row else {"error": "unknown board",
                                                        "board_id": link["board_id"],
                                                        "project_id": deliverable_project})
                links.append(link)
    links.sort(key=lambda item: (-(item.get("updated_at") or 0), item.get("id") or ""))
    return links




def _mission_cache_stamp(project: str, deliverable: Dict[str, Any]) -> str:
    """Composite stamp for a deliverable's mission views (status/dependency graph).

    These views fan out across every project a linked task lives in, so the stamp
    folds in each involved project's task_stamp plus the deliverable row's own
    updated_at (links, milestones, status, narrative). A change to any linked task
    — even in another project — bumps the stamp and invalidates the cache.
    """
    involved = {project}
    for link in (deliverable.get("task_links") or []):
        proj = link.get("project_id")
        if proj:
            involved.add(proj)
    parts = [f"d:{deliverable.get('id')}:{deliverable.get('updated_at') or ''}"]
    for proj in sorted(involved):
        parts.append(f"{proj}:{_store_facade().project_task_stamp(proj)}")
    return "|".join(parts)



class StoreDeliverablesRepository:
    """SQL-backed deliverables / mission repository (ARCH-MS-35)."""

    def add_deliverable_milestone(self, *args, **kwargs):
        return add_deliverable_milestone(*args, **kwargs)

    def approve_deliverable_breakdown(self, *args, **kwargs):
        return approve_deliverable_breakdown(*args, **kwargs)

    def archive_deliverable(self, *args, **kwargs):
        return archive_deliverable(*args, **kwargs)

    def create_deliverable(self, *args, **kwargs):
        return create_deliverable(*args, **kwargs)

    def create_project_board(self, *args, **kwargs):
        return create_project_board(*args, **kwargs)

    def defer_deliverable_breakdown(self, *args, **kwargs):
        return defer_deliverable_breakdown(*args, **kwargs)

    def deliverable_progress(self, *args, **kwargs):
        return deliverable_progress(*args, **kwargs)

    def deliverable_tally(self, *args, **kwargs):
        return deliverable_tally(*args, **kwargs)

    def generate_mission_brief(self, *args, **kwargs):
        return generate_mission_brief(*args, **kwargs)

    def get_deliverable(self, *args, **kwargs):
        return get_deliverable(*args, **kwargs)

    def get_deliverable_breakdown_proposal(self, *args, **kwargs):
        return get_deliverable_breakdown_proposal(*args, **kwargs)

    def get_deliverable_closure_report(self, *args, **kwargs):
        return get_deliverable_closure_report(*args, **kwargs)

    def get_deliverable_dependency_graph(self, *args, **kwargs):
        return get_deliverable_dependency_graph(*args, **kwargs)

    def get_mission_status(self, *args, **kwargs):
        return get_mission_status(*args, **kwargs)

    def get_project_board(self, *args, **kwargs):
        return get_project_board(*args, **kwargs)

    def link_task_to_deliverable(self, *args, **kwargs):
        return link_task_to_deliverable(*args, **kwargs)

    def link_tasks_to_deliverable(self, *args, **kwargs):
        return link_tasks_to_deliverable(*args, **kwargs)

    def list_deliverable_breakdown_proposals(self, *args, **kwargs):
        return list_deliverable_breakdown_proposals(*args, **kwargs)

    def list_deliverable_summaries(self, *args, **kwargs):
        return list_deliverable_summaries(*args, **kwargs)

    def list_deliverables(self, *args, **kwargs):
        return list_deliverables(*args, **kwargs)

    def list_project_boards(self, *args, **kwargs):
        return list_project_boards(*args, **kwargs)

    def list_task_deliverable_links(self, *args, **kwargs):
        return list_task_deliverable_links(*args, **kwargs)

    def propose_deliverable_breakdown(self, *args, **kwargs):
        return propose_deliverable_breakdown(*args, **kwargs)

    def record_deliverable_closure(self, *args, **kwargs):
        return record_deliverable_closure(*args, **kwargs)

    def reject_deliverable_breakdown(self, *args, **kwargs):
        return reject_deliverable_breakdown(*args, **kwargs)

    def run_mission_coordinator_tick(self, *args, **kwargs):
        return run_mission_coordinator_tick(*args, **kwargs)

    def set_deliverable_narration(self, *args, **kwargs):
        return set_deliverable_narration(*args, **kwargs)

    def submit_deliverable_outcome(self, *args, **kwargs):
        return submit_deliverable_outcome(*args, **kwargs)

    def unlink_task_from_deliverable(self, *args, **kwargs):
        return unlink_task_from_deliverable(*args, **kwargs)

    def update_deliverable_breakdown_proposal(self, *args, **kwargs):
        return update_deliverable_breakdown_proposal(*args, **kwargs)

    def update_mission_narrative(self, *args, **kwargs):
        return update_mission_narrative(*args, **kwargs)


def default_deliverables_repository() -> StoreDeliverablesRepository:
    return StoreDeliverablesRepository()


__all__ = [
    "StoreDeliverablesRepository",
    "default_deliverables_repository",
    "PROOF_REQUIREMENTS_SCHEMA",
    "CLOSURE_REPORT_HISTORY_LIMIT",
    "create_project_board",
    "get_project_board",
    "list_project_boards",
    "create_deliverable",
    "add_deliverable_milestone",
    "link_task_to_deliverable",
    "link_tasks_to_deliverable",
    "get_deliverable",
    "list_deliverables",
    "list_deliverable_summaries",
    "archive_deliverable",
    "deliverable_progress",
    "unlink_task_from_deliverable",
    "update_mission_narrative",
    "record_deliverable_closure",
    "get_deliverable_closure_report",
    "propose_deliverable_breakdown",
    "get_deliverable_breakdown_proposal",
    "list_deliverable_breakdown_proposals",
    "update_deliverable_breakdown_proposal",
    "reject_deliverable_breakdown",
    "defer_deliverable_breakdown",
    "submit_deliverable_outcome",
    "approve_deliverable_breakdown",
    "get_mission_status",
    "get_deliverable_dependency_graph",
    "generate_mission_brief",
    "set_deliverable_narration",
    "run_mission_coordinator_tick",
    "deliverable_tally",
    "list_task_deliverable_links",
    "_deliverable_row",
    "_project_board_row",
    "_deliverable_milestone_row",
    "_deliverable_link_row",
    "_project_board_exists_in",
    "_deliverable_exists_in",
    "_deliverable_milestone_exists_in",
    "_enforce_deliverable_intake",
    "_validate_proof_requirements",
    "_validate_deliverable_intake",
    "_create_deliverable_impl",
    "_touch_deliverable",
    "_link_task_to_deliverable_impl",
    "_link_tasks_to_deliverable_impl",
    "_rows_for_task_ids",
    "_deliverable_task_snapshots",
    "_decorate_deliverable_task_links",
    "_deliverable_dependency_closure",
    "_ensure_deliverable_dependency_closure",
    "_decorate_deliverable_task_link",
    "_breakdown_proposal_row",
    "_validate_breakdown_task_spec",
    "_validate_breakdown_payload",
    "_closure_report_id",
    "_closure_summary",
    "_record_deliverable_closure_impl",
    "_finalize_breakdown_review",
    "_resolve_mission_deliverable",
    "_registry_project_ids",
    "_find_deliverable_links_for_task",
    "_enriched_mission_task_link",
    "_mission_blockers",
    "_action",
    "_task_blocks_others",
    "_mission_next_actions",
    "_build_mission_status",
    "_build_deliverable_dependency_graph",
    "_deliverable_activity",
    "_attach_mission_brief_fields",
    "_empty_economics_totals",
    "_finalize_economics_totals",
    "_merge_kpi_group",
    "_mission_cache_stamp",
]
