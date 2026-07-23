"""Claim lifecycle repository (ARCH-MS-32).

Owns TXP task claim persistence previously planned for ``claims_store.py``:
claim_task / claim_next / complete_claim / abandon_claim / revoke_claim and
helpers (work-session claim attach/gate, mission-scoped claim_next, active
claims enrichment), plus ARCH-MS-50 leftovers: file/resource leases and
completion-evidence / risk-capability helpers drained from ``shell.py``. Cross-cutting store helpers (write queue, work-session
validators, idempotency, dispatch scoring) are reached via ``_store_facade()``
during the strangler. ``store.py`` re-exports these symbols; root
``claims_store.py`` is a compatibility shim.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import evidence_claims
import push_verification
from constants import *  # noqa: F401,F403
from db.connection import _conn
from switchboard.domain.board.tasks import READY_TASK_STATUSES
from switchboard.domain.provenance.semantic import semantic_completion_gate
from switchboard.domain.validation_policy import (
    classify_task,
    ui_playwright_evidence_gate,
)
from switchboard.storage.repositories.tasks import (
    _deps_done,
    _heal_dependency_blocked_tasks_in,
    _task_row,
    _task_tally_snapshot,
)
from switchboard.storage.repositories.deliverables import (
    _link_automatic_dispatch_eligible,
    _link_automatic_dispatch_reason,
)


def _store_facade():
    """Resolve transitional store helpers after store.py is initialized."""
    import store
    return store


def _review_continuation_wake_for_claim_in(
        c: sqlite3.Connection, task_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
    """Return the exact live Resume-review wake authorizing an In Review claim.

    A replacement reviewer still needs the ordinary task lease, but acquiring
    that lease must not turn the workflow back into implementation work.  The
    durable wake is the authority: it binds the exact task and agent and carries
    the review-continuation contract created by ``dispatch.resume_review``.
    """
    rows = c.execute(
        "SELECT wake_id, selector_json, policy_json FROM wake_intents "
        "WHERE task_id=? AND status IN ('pending','claimed') "
        "AND archived_at IS NULL ORDER BY requested_at DESC, wake_id DESC LIMIT 8",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            selector = json.loads(row["selector_json"] or "{}")
            policy = json.loads(row["policy_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        assignment = policy.get("assignment") or {}
        continuation = assignment.get("continuation") or {}
        if (
            str(selector.get("task_id") or "") == task_id
            and str(selector.get("agent_id") or "") == agent_id
            and str(assignment.get("task_id") or "") == task_id
            and continuation.get("schema") == "switchboard.review_runner_continuation.v1"
            and str(continuation.get("previous_runner_session_id") or "")
        ):
            return {
                "wake_id": row["wake_id"],
                "previous_runner_session_id": continuation["previous_runner_session_id"],
                "mode": continuation.get("mode") or "replacement_handoff",
            }
    return None


def _record_mission_claim_completion(mission_project: str, deliverable_id: str,
                                     task_project: str, task_id: str,
                                     claim_id: str, status: str,
                                     milestone_id: str = "",
                                     actor: str = "system") -> Dict[str, Any]:
    """Refresh mission progress after a linked task claim completes."""
    deliverable = _store_facade().get_deliverable(deliverable_id, project=mission_project)
    if not deliverable:
        return {"error": "unknown deliverable", "deliverable_id": deliverable_id,
                "mission_project": mission_project}
    progress = deliverable.get("progress") or deliverable_progress(deliverable)
    now = time.time()
    payload = {
        "schema": "switchboard.mission_claim_completion.v1",
        "mission_project": mission_project,
        "deliverable_id": deliverable_id,
        "milestone_id": (milestone_id or "").strip() or None,
        "task_project": task_project,
        "task_id": task_id,
        "claim_id": claim_id,
        "task_status": status,
        "progress": progress,
    }
    with _conn(mission_project) as c:
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (None, actor, "deliverable.claim_completed",
                   json.dumps(payload, sort_keys=True), now))
    deliverable_status = deliverable.get("status")
    if (progress.get("in_review_count", 0) > 0
            and deliverable_status in ("approved", "in_progress", "proposed")):
        with _conn(mission_project) as c:
            c.execute("UPDATE deliverables SET status=?, updated_at=? WHERE id=?",
                      ("in_review", now, deliverable_id))
        deliverable = _store_facade().get_deliverable(deliverable_id, project=mission_project) or deliverable
        progress = deliverable.get("progress") or deliverable_progress(deliverable)
        payload["progress"] = progress
        payload["deliverable_status"] = deliverable.get("status")
    return payload

def _claim_next_mission_scoped(agent_id: str, lanes: Any = None,
                               capabilities: Any = None,
                               max_risk: str = "", max_budget_usd: Optional[float] = None,
                               principal_id: str = "", actor: str = "system",
                               ttl_seconds: int = 1800, idem_key: str = "",
                               override_identity_risk: bool = False,
                               work_session_id: str = "", work_session: Any = None,
                               session_policy_profile: str = "",
                               require_work_session: bool = False,
                               mission_project: str = DEFAULT_PROJECT,
                               deliverable_id: str = "", board_id: str = "",
                               mission_id: str = "", milestone_id: str = "") -> Dict[str, Any]:
    """Claim the next ready task linked to a deliverable/mission."""
    now = time.time()
    lanes = _store_facade().coerce_csv_list(lanes)
    capabilities = _store_facade().coerce_csv_list(capabilities)
    lane_set = {x.strip().upper() for x in lanes}
    cap_set = {x.strip().lower() for x in capabilities}
    max_risk_value = _store_facade()._risk_value(max_risk)
    milestone_id = (milestone_id or "").strip()
    payload = {"agent_id": agent_id, "lanes": sorted(lane_set),
               "capabilities": sorted(capabilities or []), "max_risk": max_risk,
               "max_budget_usd": max_budget_usd, "ttl_seconds": ttl_seconds,
               "override_identity_risk": bool(override_identity_risk),
               "deliverable_id": (deliverable_id or "").strip(),
               "board_id": (board_id or "").strip(),
               "mission_id": (mission_id or "").strip(),
               "milestone_id": milestone_id,
               "work_session_id": work_session_id,
               "work_session": work_session,
               "session_policy_profile": session_policy_profile,
               "require_work_session": bool(require_work_session),
               "mission_scope": True}
    with _conn(mission_project) as mission_c:
        hit = _store_facade()._idem_hit(mission_c, "claim_next", idem_key, actor, payload)
        if hit is not None:
            return hit
        scope = _store_facade()._resolve_mission_deliverable(
            mission_project, deliverable_id=deliverable_id,
            board_id=board_id, mission_id=mission_id)
        if scope.get("error"):
            _store_facade()._idem_store(mission_c, "claim_next", idem_key, actor, payload, scope)
            return scope
        deliverable = scope["deliverable"]
        resolved_deliverable_id = deliverable["id"]
        links = list(deliverable.get("task_links") or [])
        milestone_statuses = {
            str(row.get("id") or ""): str(row.get("status") or "").strip().lower()
            for row in (deliverable.get("milestones") or [])
        }
        if milestone_id:
            links = [l for l in links if (l.get("milestone_id") or "") == milestone_id]
        if not links:
            response = {
                "claimed": False,
                "reason": "no_milestone_tasks" if milestone_id else "no_linked_tasks",
                "deliverable_id": resolved_deliverable_id,
                "milestone_id": milestone_id or None,
                "mission_project": mission_project,
                "retry_after_seconds": 60,
                "dispatch_reason": {
                    "policy": "mission_scope.v1",
                    "deliverable_id": resolved_deliverable_id,
                    "milestone_id": milestone_id or None,
                    "linked_task_count": 0,
                    "skipped": {},
                    "candidate_count": 0,
                },
            }
            _store_facade()._idem_store(mission_c, "claim_next", idem_key, actor, payload, response)
            return response

        eligible: List[Tuple[Any, ...]] = []
        skipped = {"active_claim": 0, "status": 0, "lane": 0, "dependencies": 0,
                   "human_approval": 0, "capability_mismatch": 0, "risk": 0, "budget": 0,
                   "identity_unknown": 0, "missing_task": 0, "unknown_project": 0,
                   "work_session": 0, "link_policy": 0, "skipped_milestone": 0}
        human_gates: Dict[str, Dict[str, Any]] = {}
        identity_risks: Dict[str, Dict[str, Any]] = {}
        work_session_findings: Dict[str, Dict[str, Any]] = {}
        link_policy_findings: Dict[str, Dict[str, Any]] = {}

        for link in links:
            task_project = (link.get("project_id") or "").strip()
            task_id = (link.get("task_id") or "").strip()
            milestone_status = milestone_statuses.get(
                str(link.get("milestone_id") or ""), "")
            if not _link_automatic_dispatch_eligible(link, milestone_status):
                reason = _link_automatic_dispatch_reason(link, milestone_status)
                counter = "skipped_milestone" if reason == "milestone_skipped" else "link_policy"
                skipped[counter] += 1
                link_policy_findings[f"{task_project}:{task_id}"] = {
                    "reason": reason,
                    "role": link.get("role"),
                    "milestone_id": link.get("milestone_id"),
                    "milestone_status": milestone_status or None,
                }
                continue
            if not _store_facade().has_project(task_project):
                skipped["unknown_project"] += 1
                continue
            with _conn(task_project) as c:
                _heal_dependency_blocked_tasks_in(
                    c, task_ids=[task_id], actor="switchboard/claim-next")
                row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if not row:
                    skipped["missing_task"] += 1
                    continue
                active = c.execute(
                    "SELECT 1 FROM task_claims WHERE task_id=? AND status='active' AND expires_at>?",
                    (task_id, now),
                ).fetchone()
                if active:
                    skipped["active_claim"] += 1
                    continue
                task = _task_row(row)
                if task.get("status") not in READY_TASK_STATUSES:
                    skipped["status"] += 1
                    continue
                if lane_set and (task.get("_wsId") or "").upper() not in lane_set:
                    skipped["lane"] += 1
                    continue
                rows = c.execute("SELECT * FROM tasks ORDER BY sort_order, task_id").fetchall()
                by_id = {t["task_id"]: t for t in [_task_row(r) for r in rows]}
                if not _deps_done(task, by_id):
                    skipped["dependencies"] += 1
                    continue
                identity_risk = _store_facade()._identity_takeover_risk_in(c, task_id, now)
                if identity_risk and not override_identity_risk:
                    skipped["identity_unknown"] += 1
                    identity_risks[task_id] = identity_risk
                    continue
                session_verdict = _store_facade()._validate_work_session_claim_binding_in(
                    c, task, agent_id, project=task_project,
                    work_session_id=work_session_id,
                    work_session=work_session,
                    policy_profile=session_policy_profile,
                    require_work_session=require_work_session,
                    now=now)
                if not session_verdict.get("ok"):
                    skipped["work_session"] += 1
                    work_session_findings[f"{task_project}:{task_id}"] = session_verdict
                    continue
                required_caps = _store_facade()._task_required_capabilities(task)
                if required_caps and not set(required_caps).issubset(cap_set):
                    skipped["capability_mismatch"] += 1
                    continue
                if max_risk_value and _store_facade()._risk_value(task.get("risk_level") or "") > max_risk_value:
                    skipped["risk"] += 1
                    continue
                tally = _task_tally_snapshot(c, task_id)
                score = _store_facade()._dispatch_score(task, lane_set, cap_set, tally, max_budget_usd)
                if score["budget"]["status"] == "over_budget":
                    skipped["budget"] += 1
                    continue
                if identity_risk and override_identity_risk:
                    score["identity_override"] = identity_risk
                eligible.append((
                    score["score"], -int(task.get("sort_order") or 0), task_id,
                    task, score, task_project, link,
                ))

        dispatch_base = {
            "policy": "mission_scope.v1",
            "deliverable_id": resolved_deliverable_id,
            "milestone_id": milestone_id or None,
            "linked_task_count": len(links),
            "skipped": skipped,
            "human_gates": human_gates,
            "identity_risks": identity_risks,
            "work_session_findings": work_session_findings,
            "link_policy_findings": link_policy_findings,
        }
        if not eligible:
            response = {
                "claimed": False,
                "reason": "no_unblocked_work",
                "deliverable_id": resolved_deliverable_id,
                "milestone_id": milestone_id or None,
                "mission_project": mission_project,
                "retry_after_seconds": 60,
                "cursor": mission_c.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0],
                "dispatch_reason": {**dispatch_base, "candidate_count": 0},
            }
            _store_facade()._idem_store(mission_c, "claim_next", idem_key, actor, payload, response)
            return response

        _, _, task_id, task, selected_score, task_project, link = sorted(
            eligible, key=lambda x: (-x[0], -x[1], x[2]))[0]
        claim_id = "taskclaim-" + uuid.uuid4().hex[:16]
        lease_id = "lease-" + uuid.uuid4().hex[:16]
        expires_at = now + max(60, int(ttl_seconds or 1800))
        with _conn(task_project) as c:
            c.execute(
                "INSERT INTO task_claims(id, task_id, agent_id, principal_id, status, claimed_at, "
                "expires_at, idem_key) VALUES (?,?,?,?,?,?,?,?)",
                (claim_id, task_id, agent_id, principal_id or None, "active",
                 now, expires_at, idem_key or None),
            )
            c.execute(
                "INSERT INTO resource_leases(id, agent_id, principal_id, task_id, resource_type, "
                "names, claimed_at, ttl_seconds) VALUES (?,?,?,?,?,?,?,?)",
                (lease_id, agent_id, principal_id or None, task_id, "task",
                 json.dumps([task_id]), now, max(60, int(ttl_seconds or 1800))),
            )
            c.execute("UPDATE tasks SET status='In Progress', assignee=?, updated_at=? WHERE task_id=?",
                      (agent_id, now, task_id))
            dispatch_reason = {**dispatch_base,
                               "score": selected_score["score"],
                               "factors": selected_score["factors"],
                               "required_capabilities": selected_score["required_capabilities"],
                               "matched_capabilities": selected_score["matched_capabilities"],
                               "candidate_count": len(eligible),
                               "task_project": task_project}
            if selected_score.get("identity_override"):
                dispatch_reason["identity_override"] = selected_score["identity_override"]
            session_verdict = _store_facade()._validate_work_session_claim_binding_in(
                c, task, agent_id, project=task_project,
                work_session_id=work_session_id,
                work_session=work_session,
                policy_profile=session_policy_profile,
                require_work_session=require_work_session,
                now=now)
            work_session_binding = _attach_work_session_claim_in(
                c, session_verdict, claim_id, task_id, agent_id, actor,
                principal_id=principal_id, project=task_project, now=now)
            if work_session_binding.get("error"):
                response = {"claimed": False, "reason": "work_session_bind_failed",
                            "task_id": task_id, "task_project": task_project,
                            "work_session": work_session_binding}
                _store_facade()._idem_store(mission_c, "claim_next", idem_key, actor, payload, response)
                return response
            dispatch_reason["work_session"] = work_session_binding
            payload_event = {"claim_id": claim_id, "lease_id": lease_id,
                             "task_id": task_id, "task_project": task_project,
                             "agent_id": agent_id, "deliverable_id": resolved_deliverable_id,
                             "milestone_id": link.get("milestone_id"),
                             "dispatch_reason": dispatch_reason}
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (task_id, actor, "task.claimed",
                       json.dumps(payload_event, sort_keys=True), now))
            claimed_task = _task_row(c.execute("SELECT * FROM tasks WHERE task_id=?",
                                               (task_id,)).fetchone())
        mission_c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (None, actor, "deliverable.claim_started",
             json.dumps({"claim_id": claim_id, "task_id": task_id,
                         "task_project": task_project,
                         "deliverable_id": resolved_deliverable_id,
                         "milestone_id": link.get("milestone_id"),
                         "agent_id": agent_id}, sort_keys=True), now))
        response = {
            "claimed": True,
            "claim_id": claim_id,
            "task": claimed_task,
            "task_project": task_project,
            "mission_project": mission_project,
            "deliverable_id": resolved_deliverable_id,
            "milestone_id": link.get("milestone_id"),
            "lease": {"lease_id": lease_id, "resource_type": "task",
                      "names": [task_id], "expires_at": expires_at},
            "budget": selected_score["budget"],
            "dispatch_reason": dispatch_reason,
            "recommendation": _store_facade()._model_recommendation(task, selected_score),
            "work_session_id": work_session_binding.get("work_session_id"),
            "work_session": work_session_binding,
        }
        _store_facade()._idem_store(mission_c, "claim_next", idem_key, actor, payload, response)
        return response

def _active_task_claims_in(c: sqlite3.Connection, task_id: str,
                           now: Optional[float] = None) -> List[Dict[str, Any]]:
    now = time.time() if now is None else now
    rows = c.execute(
        "SELECT * FROM task_claims WHERE task_id=? AND status='active' "
        "AND expires_at>? ORDER BY claimed_at",
        (task_id, now),
    ).fetchall()
    return [{
        "claim_id": r["id"],
        "task_id": r["task_id"],
        "agent_id": r["agent_id"],
        "principal_id": r["principal_id"],
        "status": r["status"],
        "claimed_at": r["claimed_at"],
        "expires_at": r["expires_at"],
    } for r in rows]

def claim_binding_target(claim_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    claim_id = (claim_id or "").strip()
    if not claim_id:
        return {}
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM task_claims WHERE id=?", (claim_id,)).fetchone()
    if not row:
        return {}
    return {
        "claim_id": row["id"],
        "task_id": row["task_id"],
        "agent_id": row["agent_id"],
        "active": row["status"] == "active" and float(row["expires_at"] or 0) > now,
        "principal_id": row["principal_id"],
    }

def _attach_work_session_claim_in(c: sqlite3.Connection, verdict: Dict[str, Any],
                                  claim_id: str, task_id: str, agent_id: str,
                                  actor: str, principal_id: str = "",
                                  project: str = DEFAULT_PROJECT,
                                  now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    session = verdict.get("work_session")
    if not session:
        return {"work_session_id": None, "status": "not_required",
                "source": verdict.get("source"),
                "policy_profile": verdict.get("policy_profile"),
                "required": verdict.get("required", False)}
    if verdict.get("source") == "payload":
        data = dict(verdict.get("normalized_payload") or {})
        data["claim_id"] = claim_id
        data["task_id"] = data.get("task_id") or task_id
        data["agent_id"] = data.get("agent_id") or agent_id
        data["status"] = "active"
        created = _store_facade()._insert_work_session_in(
            c, data, actor=actor, principal_id=principal_id, project=project, now=now)
        if created.get("error"):
            return {"error": created.get("error"), "work_session_id": data.get("work_session_id")}
        session = created["work_session"]
    else:
        c.execute(
            "UPDATE work_sessions SET claim_id=?, status='active', updated_by=?, updated_at=? "
            "WHERE work_session_id=?",
            (claim_id, actor, now, session["work_session_id"]),
        )
        row = c.execute("SELECT * FROM work_sessions WHERE work_session_id=?",
                        (session["work_session_id"],)).fetchone()
        session = _store_facade()._work_session_row(row)
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "work_session.updated",
                   json.dumps({"work_session_id": session["work_session_id"],
                               "claim_id": claim_id,
                               "updated_fields": ["claim_id", "status"]},
                              sort_keys=True), now))
    return {"work_session_id": session.get("work_session_id"),
            "status": "bound",
            "source": verdict.get("source"),
            "policy_profile": verdict.get("policy_profile"),
            "required": verdict.get("required", False)}

def _complete_claim_work_session_gate_in(
        c: sqlite3.Connection, claim: sqlite3.Row, task: Dict[str, Any],
        evidence_obj: Dict[str, Any], project: str, now: float) -> Dict[str, Any]:
    row = c.execute(
        "SELECT * FROM work_sessions WHERE claim_id=? "
        "ORDER BY updated_at DESC, created_at DESC, work_session_id LIMIT 1",
        (claim["id"],),
    ).fetchone()
    if not row:
        row = _store_facade()._active_work_session_row_in(
            c, task_id=claim["task_id"], agent_id=claim["agent_id"], now=now)
    session = _store_facade()._work_session_row(row) if row else None
    requested_profile = (
        evidence_obj.get("session_policy_profile")
        or evidence_obj.get("policy_profile")
        or (session or {}).get("policy_profile")
        or evidence_obj.get("completion_profile")
        or ""
    )
    required, profile = _store_facade()._work_session_required(task, str(requested_profile or ""),
                                               project=project)
    rules = _store_facade()._session_policy_profile_rules(profile, project=project)
    if not rules:
        return _store_facade()._unknown_session_policy_profile(profile, project)
    if session and _store_facade()._session_policy_profile_rules(
            session.get("policy_profile") or profile, project=project).get("work_session_required"):
        required = True
        profile = _store_facade()._normalize_session_policy_profile(session.get("policy_profile") or profile)
        rules = _store_facade()._session_policy_profile_rules(profile, project=project)
    completion_profile = _store_facade()._normalize_session_policy_profile(
        str(evidence_obj.get("completion_profile") or ""))
    if completion_profile == "offline_evidence" and not required:
        if not (evidence_obj.get("offline_evidence") or evidence_obj.get("artifact_url") or evidence_obj.get("verification")):
            return _store_facade()._work_session_failure(
                "missing_offline_evidence",
                "Offline completion profile requires explicit evidence before claim completion.",
                "missing_data",
                details={"required": False, "policy_profile": completion_profile},
            )
        return {"ok": True, "required": False, "policy_profile": completion_profile,
                "source": "offline_profile", "work_session": None}
    if not required:
        return {"ok": True, "required": False, "policy_profile": profile,
                "source": "not_required", "work_session": session}
    if not session:
        return _store_facade()._work_session_failure(
            "work_session_required",
            "A bound Work Session is required before completing code-strict work.",
            "missing_data",
            details={"required": True, "policy_profile": profile},
        )

    allow_dirty = _store_facade()._evidence_truthy(evidence_obj.get("allow_dirty"))
    if allow_dirty and not str(evidence_obj.get("allow_dirty_reason") or "").strip():
        return _store_facade()._work_session_failure(
            "missing_dirty_allowance_reason",
            "Dirty completion requires allow_dirty_reason evidence.",
            "missing_data",
            details={"required": True, "policy_profile": profile,
                     "work_session_id": session.get("work_session_id")},
        )
    state = _store_facade()._validate_work_session_claim_state(
        session, task, claim["agent_id"], project, required=required, profile=profile,
        source="complete_claim", normalized_payload=None, now=now,
        allow_dirty=allow_dirty)
    if not state.get("ok"):
        return state

    problems: List[Dict[str, Any]] = []
    evidence_branch = str(evidence_obj.get("branch") or "").strip()
    evidence_head = str(evidence_obj.get("head_sha") or "").strip()
    session_branch = str(session.get("branch") or "").strip()
    session_head = str(session.get("head_sha") or "").strip()
    if rules.get("merge_authority") != "offline_verifier" and not evidence_branch:
        problems.append({"reason": "missing_completion_branch", "failure_class": "missing_data",
                         "message": "Completion evidence must include branch."})
    elif session_branch and evidence_branch != session_branch:
        problems.append({"reason": "stale_branch", "failure_class": "stale_branch",
                         "message": "Completion branch does not match the bound Work Session.",
                         "evidence_branch": evidence_branch, "work_session_branch": session_branch})
    if rules.get("merge_authority") != "offline_verifier" and not evidence_head:
        problems.append({"reason": "missing_completion_head_sha", "failure_class": "missing_data",
                         "message": "Completion evidence must include head_sha."})
    elif not session_head:
        problems.append({"reason": "missing_work_session_head_sha", "failure_class": "missing_data",
                         "message": "Bound Work Session must record head_sha before completion."})
    elif evidence_head != session_head:
        problems.append({"reason": "stale_head_sha", "failure_class": "stale_branch",
                         "message": "Completion head_sha does not match the bound Work Session.",
                         "evidence_head_sha": evidence_head, "work_session_head_sha": session_head})
    if rules.get("merge_authority") != "none" and not _store_facade()._completion_has_push_or_review_evidence(evidence_obj):
        problems.append({"reason": "missing_push_or_review_evidence",
                         "failure_class": "missing_data",
                         "message": "Completion evidence must include PR, pushed branch, or offline evidence."})
    executed_test_gate = None
    if rules.get("requires_executed_tests"):
        executed_test_gate = _store_facade()._executed_test_run_gate(evidence_obj, session)
        if not executed_test_gate.get("ok"):
            problems.append({"reason": executed_test_gate.get("reason") or "missing_executed_test_run",
                             "failure_class": "missing_data",
                             "message": executed_test_gate.get("message"),
                             "executed_test_gate": executed_test_gate})
    elif rules.get("requires_tests") and not _store_facade()._completion_evidence_has_tests(evidence_obj, session):
        problems.append({"reason": "missing_test_evidence", "failure_class": "missing_data",
                         "message": "Completion evidence must record relevant tests or verification."})
    if rules.get("requires_diff_check") and not _store_facade()._completion_evidence_has_diff_check(evidence_obj, session):
        problems.append({"reason": "missing_diff_check", "failure_class": "missing_data",
                         "message": "Completion evidence must record git diff --check as clean."})
    hygiene = (session or {}).get("hygiene") or {}
    preflight = hygiene.get("repo_preflight") or {}
    changed_files = (
        evidence_obj.get("changed_files")
        or preflight.get("changed_files")
        or hygiene.get("changed_files")
        or []
    )
    ui_gate = ui_playwright_evidence_gate(
        task, evidence_obj, session, project=project,
        head_sha=evidence_head or session_head, changed_files=changed_files)
    if not ui_gate.get("ok"):
        problems.append({
            "reason": ui_gate.get("reason") or ui_gate.get("error") or "missing_ui_playwright_evidence",
            "failure_class": "missing_data",
            "message": ui_gate.get("message") or "UI validation policy failed.",
            "ui_playwright_gate": ui_gate,
        })
    problems.extend(_store_facade()._work_session_stale_lease_problems(session, now))
    if problems:
        first = problems[0]
        return _store_facade()._work_session_failure(
            first["reason"], first["message"], first["failure_class"],
            details={"problems": problems, "required": required,
                     "policy_profile": profile,
                     "work_session_id": session.get("work_session_id")},
        )
    response = {"ok": True, "required": required, "policy_profile": profile,
            "policy": rules,
            "source": "complete_claim", "work_session": session,
            "allow_dirty": allow_dirty}
    if executed_test_gate:
        response["executed_test_gate"] = executed_test_gate
    return response

def _claim_task_impl(task_id: str, agent_id: str,
                     principal_id: str = "", actor: str = "system",
                     ttl_seconds: int = 1800, idem_key: str = "",
                     override_identity_risk: bool = False,
                     work_session_id: str = "", work_session: Any = None,
                     session_policy_profile: str = "",
                     require_work_session: bool = False,
                     project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Atomically claim one specific ready, unblocked task.

    Use this when a human/operator has already selected the task. Unlike claim_next,
    this never substitutes a different scheduler-preferred task.
    """
    now = time.time()
    task_id = (task_id or "").strip()
    payload = {"task_id": task_id, "agent_id": agent_id,
               "ttl_seconds": ttl_seconds,
               "override_identity_risk": bool(override_identity_risk),
               "work_session_id": work_session_id,
               "work_session": work_session,
               "session_policy_profile": session_policy_profile,
               "require_work_session": bool(require_work_session)}
    with _conn(project) as c:
        hit = _store_facade()._idem_hit(c, "claim_task", idem_key, actor, payload)
        if hit is not None:
            return hit
        _heal_dependency_blocked_tasks_in(
            c, task_ids=[task_id], actor="switchboard/claim-task")
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            response = {"claimed": False, "reason": "task_not_found", "task_id": task_id}
            _store_facade()._idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response
        task = _task_row(row)
        active = c.execute(
            "SELECT * FROM task_claims WHERE task_id=? AND status='active' AND expires_at>?",
            (task_id, now),
        ).fetchone()
        if active:
            response = {"claimed": False, "reason": "active_claim",
                        "task_id": task_id, "claim_id": active["id"],
                        "agent_id": active["agent_id"]}
            _store_facade()._idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response
        review_continuation = (
            _review_continuation_wake_for_claim_in(c, task_id, agent_id)
            if task.get("status") == "In Review" else None
        )
        orphan_adoption = task.get("status") == "In Progress"
        if (task.get("status") not in READY_TASK_STATUSES
                and not orphan_adoption and not review_continuation):
            response = {"claimed": False, "reason": "status_not_ready",
                        "task_id": task_id, "status": task.get("status")}
            _store_facade()._idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response
        if orphan_adoption and not (work_session_id or work_session):
            response = {"claimed": False, "reason": "orphan_work_session_required",
                        "task_id": task_id, "status": task.get("status")}
            _store_facade()._idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response
        rows = c.execute("SELECT * FROM tasks ORDER BY sort_order, task_id").fetchall()
        by_id = {t["task_id"]: t for t in [_task_row(r) for r in rows]}
        if not _deps_done(task, by_id):
            response = {"claimed": False, "reason": "dependencies_unmet",
                        "task_id": task_id, "depends_on": task.get("depends_on") or []}
            _store_facade()._idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response
        risk = _store_facade()._identity_takeover_risk_in(c, task_id, now)
        if risk and not override_identity_risk:
            response = {"claimed": False, **risk,
                        "override_field": "override_identity_risk",
                        "override_required": True}
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (task_id, "switchboard/identity", "task.claim_blocked_identity",
                       json.dumps({"agent_id": agent_id, **response}, sort_keys=True), now))
            _store_facade()._idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response

        session_verdict = _store_facade()._validate_work_session_claim_binding_in(
            c, task, agent_id, project=project,
            work_session_id=work_session_id,
            work_session=work_session,
            policy_profile=session_policy_profile,
            require_work_session=require_work_session,
            now=now)
        if not session_verdict.get("ok"):
            response = {"claimed": False,
                        "reason": session_verdict.get("reason") or "invalid_work_session",
                        "failure_class": session_verdict.get("failure_class"),
                        "severity": session_verdict.get("severity"),
                        "message": session_verdict.get("message"),
                        "task_id": task_id,
                        "work_session": session_verdict,
                        "override_field": "work_session",
                        "override_required": True}
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (task_id, actor, "task.claim_blocked_work_session",
                       json.dumps({"agent_id": agent_id, **response}, sort_keys=True), now))
            _store_facade()._idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response

        claim_id = "taskclaim-" + uuid.uuid4().hex[:16]
        lease_id = "lease-" + uuid.uuid4().hex[:16]
        ttl = max(60, int(ttl_seconds or 1800))
        expires_at = now + ttl
        c.execute(
            "INSERT INTO task_claims(id, task_id, agent_id, principal_id, status, claimed_at, "
            "expires_at, idem_key) VALUES (?,?,?,?,?,?,?,?)",
            (claim_id, task_id, agent_id, principal_id or None, "active",
             now, expires_at, idem_key or None),
        )
        c.execute(
            "INSERT INTO resource_leases(id, agent_id, principal_id, task_id, resource_type, "
            "names, claimed_at, ttl_seconds) VALUES (?,?,?,?,?,?,?,?)",
            (lease_id, agent_id, principal_id or None, task_id, "task",
             json.dumps([task_id]), now, ttl),
        )
        next_status = "In Review" if review_continuation else "In Progress"
        c.execute("UPDATE tasks SET status=?, assignee=?, updated_at=? WHERE task_id=?",
                  (next_status, agent_id, now, task_id))
        dispatch_reason = {"policy": "exact.v1", "requested_task_id": task_id,
                           "dependency_checked": True}
        if review_continuation:
            dispatch_reason["review_continuation"] = review_continuation
            dispatch_reason["workflow_status_preserved"] = "In Review"
        if orphan_adoption:
            dispatch_reason["orphan_adopted"] = True
        if risk and override_identity_risk:
            dispatch_reason["identity_override"] = risk
        work_session_binding = _attach_work_session_claim_in(
            c, session_verdict, claim_id, task_id, agent_id, actor,
            principal_id=principal_id, project=project, now=now)
        if work_session_binding.get("error"):
            response = {"claimed": False, "reason": "work_session_bind_failed",
                        "task_id": task_id, "work_session": work_session_binding}
            _store_facade()._idem_store(c, "claim_task", idem_key, actor, payload, response)
            return response
        dispatch_reason["work_session"] = work_session_binding
        payload_event = {"claim_id": claim_id, "lease_id": lease_id,
                         "task_id": task_id, "agent_id": agent_id,
                         "dispatch_reason": dispatch_reason}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, ("task.orphan_claim_adopted" if orphan_adoption
                                    else "task.claimed"),
                   json.dumps(payload_event, sort_keys=True), now))
        claimed_task = _task_row(c.execute("SELECT * FROM tasks WHERE task_id=?",
                                           (task_id,)).fetchone())
        response = {
            "claimed": True,
            "claim_id": claim_id,
            "task": claimed_task,
            "lease": {"lease_id": lease_id, "resource_type": "task",
                      "names": [task_id], "expires_at": expires_at},
            "dispatch_reason": dispatch_reason,
            "work_session_id": work_session_binding.get("work_session_id"),
            "work_session": work_session_binding,
        }
        _store_facade()._idem_store(c, "claim_task", idem_key, actor, payload, response)
        return response

def claim_task(task_id: str, agent_id: str,
               principal_id: str = "", actor: str = "system",
               ttl_seconds: int = 1800, idem_key: str = "",
               override_identity_risk: bool = False,
               work_session_id: str = "", work_session: Any = None,
               session_policy_profile: str = "",
               require_work_session: bool = False,
               project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    s = _store_facade()
    task = s.get_task(task_id, project=project)
    validation = (classify_task(task or {}, project=project, existing=task or {})
                  if task else {"ok": False, "error": "task_not_found"})
    if not validation.get("ok"):
        return {"claimed": False,
                "reason": validation.get("error") or "ui_validation_policy_failed",
                "task_id": task_id, "validation_policy": validation}
    if "agent_state" in (task or {}):
        s.ensure_task_validation(task_id, project=project, actor=actor)
    return s._write_through(project, lambda: s._claim_task_impl(
        task_id, agent_id, principal_id=principal_id, actor=actor,
        ttl_seconds=ttl_seconds, idem_key=idem_key,
        override_identity_risk=override_identity_risk,
        work_session_id=work_session_id, work_session=work_session,
        session_policy_profile=session_policy_profile,
        require_work_session=require_work_session, project=project))

def claim_next(agent_id: str, lanes: Any = None,
               capabilities: Any = None,
               max_risk: str = "", max_budget_usd: Optional[float] = None,
               principal_id: str = "", actor: str = "system",
               ttl_seconds: int = 1800, idem_key: str = "",
               override_identity_risk: bool = False,
               work_session_id: str = "", work_session: Any = None,
               session_policy_profile: str = "",
               require_work_session: bool = False,
               project: str = DEFAULT_PROJECT,
               deliverable_id: str = "", board_id: str = "",
               mission_id: str = "", milestone_id: str = "") -> Dict[str, Any]:
    """Atomically claim the highest-priority unblocked task for an agent.

    This is the first TXP slice: deterministic, dependency-aware, and intentionally
    conservative. More sophisticated cost/reliability scoring can layer onto the same
    task_claims/activity records.

    When deliverable_id or board_id/mission_id is provided, only linked mission tasks
    are eligible — the scheduler never wanders outside that deliverable scope.
    """
    if (deliverable_id or board_id or mission_id):
        s = _store_facade()
        return s._claim_next_mission_scoped(
            agent_id, lanes=lanes, capabilities=capabilities,
            max_risk=max_risk, max_budget_usd=max_budget_usd,
            principal_id=principal_id, actor=actor,
            ttl_seconds=ttl_seconds, idem_key=idem_key,
            override_identity_risk=override_identity_risk,
            work_session_id=work_session_id,
            work_session=work_session,
            session_policy_profile=session_policy_profile,
            require_work_session=require_work_session,
            mission_project=project,
            deliverable_id=deliverable_id, board_id=board_id,
            mission_id=mission_id, milestone_id=milestone_id)
    now = time.time()
    lanes = _store_facade().coerce_csv_list(lanes)
    capabilities = _store_facade().coerce_csv_list(capabilities)
    lane_set = {x.strip().upper() for x in lanes}
    cap_set = {x.strip().lower() for x in capabilities}
    max_risk_value = _store_facade()._risk_value(max_risk)
    payload = {"agent_id": agent_id, "lanes": sorted(lane_set),
               "capabilities": sorted(capabilities or []), "max_risk": max_risk,
               "max_budget_usd": max_budget_usd, "ttl_seconds": ttl_seconds,
               "override_identity_risk": bool(override_identity_risk),
               "work_session_id": work_session_id,
               "work_session": work_session,
               "session_policy_profile": session_policy_profile,
               "require_work_session": bool(require_work_session)}
    with _conn(project) as c:
        hit = _store_facade()._idem_hit(c, "claim_next", idem_key, actor, payload)
        if hit is not None:
            return hit
        _heal_dependency_blocked_tasks_in(c, actor="switchboard/claim-next")
        active_claims = {
            r["task_id"] for r in c.execute(
                "SELECT task_id FROM task_claims WHERE status='active' AND expires_at>?",
                (now,),
            ).fetchall()
        }
        rows = c.execute("SELECT * FROM tasks ORDER BY sort_order, task_id").fetchall()
        tasks = [_task_row(r) for r in rows]
        by_id = {t["task_id"]: t for t in tasks}
        eligible = []
        skipped = {"active_claim": 0, "status": 0, "lane": 0, "dependencies": 0,
                   "human_approval": 0, "capability_mismatch": 0, "risk": 0, "budget": 0,
                   "identity_unknown": 0, "work_session": 0, "ui_validation": 0}
        identity_risks: Dict[str, Dict[str, Any]] = {}
        human_gates: Dict[str, Dict[str, Any]] = {}
        work_session_findings: Dict[str, Dict[str, Any]] = {}
        validation_findings: Dict[str, Dict[str, Any]] = {}
        for t in tasks:
            if t["task_id"] in active_claims:
                skipped["active_claim"] += 1
                continue
            if t.get("status") not in READY_TASK_STATUSES:
                skipped["status"] += 1
                continue
            validation = classify_task(t, project=project, existing=t)
            if not validation.get("ok"):
                skipped["ui_validation"] += 1
                validation_findings[t["task_id"]] = validation
                continue
            t["_effective_validation"] = validation
            if lane_set and (t.get("_wsId") or "").upper() not in lane_set:
                skipped["lane"] += 1
                continue
            if not _deps_done(t, by_id):
                skipped["dependencies"] += 1
                continue
            identity_risk = _store_facade()._identity_takeover_risk_in(c, t["task_id"], now)
            if identity_risk and not override_identity_risk:
                skipped["identity_unknown"] += 1
                identity_risks[t["task_id"]] = identity_risk
                continue
            session_verdict = _store_facade()._validate_work_session_claim_binding_in(
                c, t, agent_id, project=project,
                work_session_id=work_session_id,
                work_session=work_session,
                policy_profile=session_policy_profile,
                require_work_session=require_work_session,
                now=now)
            if not session_verdict.get("ok"):
                skipped["work_session"] += 1
                work_session_findings[t["task_id"]] = session_verdict
                continue
            required_caps = _store_facade()._task_required_capabilities(t)
            if required_caps and not set(required_caps).issubset(cap_set):
                skipped["capability_mismatch"] += 1
                continue
            if max_risk_value and _store_facade()._risk_value(t.get("risk_level") or "") > max_risk_value:
                skipped["risk"] += 1
                continue
            tally = _task_tally_snapshot(c, t["task_id"])
            score = _store_facade()._dispatch_score(t, lane_set, cap_set, tally, max_budget_usd)
            if score["budget"]["status"] == "over_budget":
                skipped["budget"] += 1
                continue
            if identity_risk and override_identity_risk:
                score["identity_override"] = identity_risk
            eligible.append((score["score"], -int(t.get("sort_order") or 0), t["task_id"], t, score))
        if not eligible:
            response = {"claimed": False, "reason": "no_unblocked_work",
                        "retry_after_seconds": 60,
                        "cursor": c.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0],
                        "dispatch_reason": {"policy": "score.v1", "skipped": skipped,
                                            "candidate_count": 0,
                                            "human_gates": human_gates,
                                            "identity_risks": identity_risks,
                                            "work_session_findings": work_session_findings,
                                            "validation_findings": validation_findings}}
            _store_facade()._idem_store(c, "claim_next", idem_key, actor, payload, response)
            return response
        _, _, _, task, selected_score = sorted(
            eligible, key=lambda x: (-x[0], -x[1], x[2]))[0]
        selected_validation = task.pop("_effective_validation", {})
        if selected_validation:
            state = dict(task.get("agent_state") or {})
            state["validation_policy"] = selected_validation
            task["agent_state"] = state
            c.execute("UPDATE tasks SET agent_state=?, updated_at=? WHERE task_id=?",
                      (json.dumps(state, sort_keys=True), now, task["task_id"]))
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (task["task_id"], actor, "validation.classified",
                       json.dumps(selected_validation, sort_keys=True), now))
        claim_id = "taskclaim-" + uuid.uuid4().hex[:16]
        lease_id = "lease-" + uuid.uuid4().hex[:16]
        expires_at = now + max(60, int(ttl_seconds or 1800))
        c.execute(
            "INSERT INTO task_claims(id, task_id, agent_id, principal_id, status, claimed_at, "
            "expires_at, idem_key) VALUES (?,?,?,?,?,?,?,?)",
            (claim_id, task["task_id"], agent_id, principal_id or None, "active",
             now, expires_at, idem_key or None),
        )
        c.execute(
            "INSERT INTO resource_leases(id, agent_id, principal_id, task_id, resource_type, "
            "names, claimed_at, ttl_seconds) VALUES (?,?,?,?,?,?,?,?)",
            (lease_id, agent_id, principal_id or None, task["task_id"], "task",
             json.dumps([task["task_id"]]), now, max(60, int(ttl_seconds or 1800))),
        )
        c.execute("UPDATE tasks SET status='In Progress', assignee=?, updated_at=? WHERE task_id=?",
                  (agent_id, now, task["task_id"]))
        dispatch_reason = {"policy": "score.v1",
                           "score": selected_score["score"],
                           "factors": selected_score["factors"],
                           "required_capabilities": selected_score["required_capabilities"],
                           "matched_capabilities": selected_score["matched_capabilities"],
                           "skipped": skipped,
                           "candidate_count": len(eligible)}
        if selected_score.get("identity_override"):
            dispatch_reason["identity_override"] = selected_score["identity_override"]
        session_verdict = _store_facade()._validate_work_session_claim_binding_in(
            c, task, agent_id, project=project,
            work_session_id=work_session_id,
            work_session=work_session,
            policy_profile=session_policy_profile,
            require_work_session=require_work_session,
            now=now)
        work_session_binding = _attach_work_session_claim_in(
            c, session_verdict, claim_id, task["task_id"], agent_id, actor,
            principal_id=principal_id, project=project, now=now)
        if work_session_binding.get("error"):
            response = {"claimed": False, "reason": "work_session_bind_failed",
                        "task_id": task["task_id"], "work_session": work_session_binding}
            _store_facade()._idem_store(c, "claim_next", idem_key, actor, payload, response)
            return response
        dispatch_reason["work_session"] = work_session_binding
        payload_event = {"claim_id": claim_id, "lease_id": lease_id,
                         "task_id": task["task_id"], "agent_id": agent_id,
                         "dispatch_reason": dispatch_reason}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task["task_id"], actor, "task.claimed",
                   json.dumps(payload_event, sort_keys=True), now))
        claimed_task = _task_row(c.execute("SELECT * FROM tasks WHERE task_id=?",
                                           (task["task_id"],)).fetchone())
        response = {
            "claimed": True,
            "claim_id": claim_id,
            "task": claimed_task,
            "lease": {"lease_id": lease_id, "resource_type": "task",
                      "names": [task["task_id"]], "expires_at": expires_at},
            "budget": selected_score["budget"],
            "dispatch_reason": dispatch_reason,
            "recommendation": _store_facade()._model_recommendation(task, selected_score),
            "work_session_id": work_session_binding.get("work_session_id"),
            "work_session": work_session_binding,
        }
        _store_facade()._idem_store(c, "claim_next", idem_key, actor, payload, response)
        return response

def _complete_claim_impl(claim_id: str, evidence: str = "", final_status: str = "",
                         actor: str = "system",
                         project: str = DEFAULT_PROJECT,
                         mission_project: str = "",
                         finalize: bool = True,
                         push_check: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    now = time.time()
    evidence_obj = _store_facade()._parse_evidence(evidence)
    requested_status = (final_status or evidence_obj.get("final_status") or evidence_obj.get("status") or "").strip()
    done_requested = requested_status.lower() == "done" or str(evidence_obj.get("done", "")).lower() in ("1", "true", "yes")
    if done_requested and not evidence_obj:
        return {"error": "evidence required for final_status=Done", "claim_id": claim_id}
    done_gate = None
    if done_requested:
        done_gate = {
            "code": "done_requires_merge_provenance",
            "message": "Agent completion records evidence and moves to In Review; Done requires GitHub/default-branch merge provenance.",
            "requested_status": requested_status or "Done",
        }
    next_status = "In Review"
    pushed_at = evidence_obj.get("pushed_at")
    if pushed_at is None and evidence_obj.get("head_sha"):
        pushed_at = now
    merged_at = evidence_obj.get("merged_at")
    if merged_at is None and evidence_obj.get("merged_sha"):
        merged_at = now
    with _conn(project) as c:
        row = c.execute("SELECT * FROM task_claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            return {"error": "claim not found", "claim_id": claim_id}
        if row["status"] != "active":
            return {"error": "claim is not active", "claim_id": claim_id,
                    "status": row["status"]}
        task_row = c.execute("SELECT * FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        task_for_gate = _task_row(task_row) if task_row else {"task_id": row["task_id"]}
        work_session_gate = _complete_claim_work_session_gate_in(
            c, row, task_for_gate, evidence_obj, project, now)
        if not work_session_gate.get("ok"):
            response = {"completed": False,
                        "reason": work_session_gate.get("reason") or "work_session_completion_gate_failed",
                        "failure_class": work_session_gate.get("failure_class"),
                        "severity": work_session_gate.get("severity"),
                        "message": work_session_gate.get("message"),
                        "claim_id": claim_id,
                        "task_id": row["task_id"],
                        "work_session_gate": work_session_gate,
                        "override_field": "completion_evidence",
                        "override_required": True}
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "task.complete_blocked_work_session",
                       json.dumps({"evidence": evidence_obj, **response}, sort_keys=True), now))
            return response
        semantic_gate = semantic_completion_gate(task_for_gate, evidence_obj)
        if not semantic_gate.get("ok"):
            current_git = _store_facade()._load_git_state(c, row["task_id"])
            git_updates = _store_facade()._preserve_provider_pr_evidence(
                current_git,
                {
                    "branch": evidence_obj.get("branch"),
                    "head_sha": evidence_obj.get("head_sha"),
                    "pushed_at": now if evidence_obj.get("head_sha") else None,
                    "pr_number": evidence_obj.get("pr_number"),
                    "pr_url": evidence_obj.get("pr_url"),
                    "evidence": evidence_obj,
                },
                evidence_obj,
            )
            git_state = _store_facade()._upsert_git_state(c, row["task_id"], git_updates)
            response = {
                "completed": False,
                "reason": semantic_gate.get("code") or "semantic_completion_failed",
                "failure_class": semantic_gate.get("failure_class") or "failed_gate",
                "message": semantic_gate.get("message"),
                "claim_id": claim_id,
                "task_id": row["task_id"],
                "semantic_gate": semantic_gate,
                "git_state": git_state,
            }
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "task.complete_blocked_semantic",
                       json.dumps({"evidence": evidence_obj, **response}, sort_keys=True), now))
            return response
        # Completion is a two-phase handoff.  Resolve the exact supervised
        # implementation generation before mutating claim/task state; task-only
        # inference is deliberately forbidden because it can select a reviewer
        # or a stale generation.  Connect may late-bind claim_id, so the Work
        # Session is the only admissible alternate identity.
        gated_work_session = work_session_gate.get("work_session") or {}
        work_session_id = str(gated_work_session.get("work_session_id") or "")
        candidates = c.execute(
            "SELECT * FROM runner_sessions WHERE status IN ('starting','ready','running') "
            "AND (claim_id=? OR (?!='' AND json_extract(metadata_json,'$.work_session_id')=?))",
            (claim_id, work_session_id, work_session_id),
        ).fetchall()
        exact = []
        for candidate in candidates:
            metadata = json.loads(candidate["metadata_json"] or "{}")
            role = str(metadata.get("role") or metadata.get("lifecycle_role")
                       or "implementation").strip().lower()
            if role == "implementation":
                exact.append((candidate, metadata))
        if len(exact) != 1:
            response = {
                "completed": False,
                "reason": "implementation_execution_binding_ambiguous",
                "failure_class": "unbound_identity",
                "message": ("complete_claim requires exactly one live implementation "
                            "execution bound by claim or Work Session"),
                "claim_id": claim_id, "task_id": row["task_id"],
                "matching_execution_ids": [item[0]["runner_session_id"] for item in exact],
            }
            c.execute(
                "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                (row["task_id"], actor, "task.complete_blocked_runner_binding",
                 json.dumps(response, sort_keys=True), now),
            )
            return response
        runner, runner_metadata = exact[0]
        execution_id = str(runner["runner_session_id"])
        runner_metadata["completion_handoff"] = {
            "schema": "switchboard.completion_handoff.v1",
            "claim_id": claim_id,
            "task_id": row["task_id"],
            "execution_id": execution_id,
            "work_session_id": work_session_id or None,
            "role": "implementation",
            "requested_at": now,
            "evidence": evidence_obj,
            "requested_status": requested_status or None,
        }
        runner_metadata["lease_surrender"] = {
            "schema": "switchboard.runner_lease_surrender.v1",
            "claim_id": claim_id,
            "work_session_id": work_session_id or None,
            "surrendered_at": now,
            "reason": "completion_requested",
        }
        ttl_s = max(10, int(runner["heartbeat_ttl_s"] or 60))
        c.execute("UPDATE runner_sessions SET heartbeat_at=?, metadata_json=?, updated_at=? "
                  "WHERE runner_session_id=?",
                  (now - ttl_s, json.dumps(runner_metadata, sort_keys=True), now,
                   execution_id))
        c.execute("UPDATE task_claims SET status='stopping' WHERE id=? AND status='active'",
                  (claim_id,))
        c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (row["task_id"], actor, "task.claim.stopping",
             json.dumps({"claim_id": claim_id, "execution_id": execution_id,
                         "work_session_id": work_session_id or None}, sort_keys=True), now),
        )
        return {"completed": False, "stopping": True, "pending_host_ack": True,
                "lifecycle_phase": "stopping", "claim_id": claim_id,
                "task_id": row["task_id"], "execution_id": execution_id,
                "status": task_for_gate.get("status") or "In Progress"}
        c.execute("UPDATE task_claims SET status='completed', completed_at=? WHERE id=?",
                  (now, claim_id))
        c.execute("UPDATE resource_leases SET released_at=? WHERE resource_type='task' "
                  "AND task_id=? AND agent_id=? AND released_at IS NULL",
                  (now, row["task_id"], row["agent_id"]))
        c.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=? "
                  "AND status NOT IN ('Done', 'Cancelled', 'Canceled')",
                  (next_status, now, row["task_id"]))
        current_git = _store_facade()._load_git_state(c, row["task_id"])
        git_updates = {
            "branch": evidence_obj.get("branch"),
            "head_sha": evidence_obj.get("head_sha"),
            "pushed_at": pushed_at,
            "pr_number": evidence_obj.get("pr_number"),
            "pr_url": evidence_obj.get("pr_url"),
            "merged_sha": evidence_obj.get("merged_sha"),
            "merged_at": merged_at,
            "in_main_content": True if evidence_obj.get("merged_sha") else None,
            "evidence": ({**evidence_obj, "push_verification": push_check}
                         if push_check else evidence_obj),
        }
        git_updates = _store_facade()._preserve_provider_pr_evidence(current_git, git_updates, evidence_obj)
        git_state = _store_facade()._upsert_git_state(c, row["task_id"], git_updates)
        task_snapshot_row = c.execute("SELECT * FROM tasks WHERE task_id=?",
                                      (row["task_id"],)).fetchone()
        task_snapshot = _task_row(task_snapshot_row) if task_snapshot_row else {"task_id": row["task_id"]}
        task_snapshot["git_state"] = git_state
        external_ci_gate = _store_facade()._external_ci_review_gate(
            task_snapshot, evidence=evidence_obj, c=c, project=project)
        publication_gate = _store_facade()._publication_review_gate(
            task_snapshot, evidence=evidence_obj, c=c, project=project)
        status_row = c.execute("SELECT status FROM tasks WHERE task_id=?",
                               (row["task_id"],)).fetchone()
        stored_status = status_row["status"] if status_row else next_status
        terminal_status_preserved = (
            stored_status == "Done" and _store_facade()._has_done_provenance(git_state)
        )
        if terminal_status_preserved:
            next_status = "Done"
        elif stored_status in ("Cancelled", "Canceled"):
            next_status = stored_status
        gated_work_session = work_session_gate.get("work_session") or {}
        if work_session_gate.get("required") and gated_work_session.get("work_session_id"):
            c.execute(
                "UPDATE work_sessions SET status='completed', completed_at=?, updated_by=?, updated_at=? "
                "WHERE work_session_id=?",
                (now, actor, now, gated_work_session["work_session_id"]),
            )
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "work_session.completed",
                       json.dumps({"work_session_id": gated_work_session["work_session_id"],
                                   "claim_id": claim_id,
                                   "source": "complete_claim",
                                   "policy_profile": work_session_gate.get("policy_profile")},
                                  sort_keys=True), now))
        _surrender_claim_runner_leases_in(
            c, claim_id, row["task_id"], actor, now,
            work_session_id=str(gated_work_session.get("work_session_id") or ""),
        )
        if done_gate:
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "task.done_blocked",
                       json.dumps({"claim_id": claim_id, "evidence": evidence_obj,
                                   "done_gate": done_gate,
                                   "source": "complete_claim"}, sort_keys=True), now))
        if external_ci_gate.get("required"):
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "task.review_gate",
                       json.dumps({"claim_id": claim_id,
                                   "gate": external_ci_gate.get("gate"),
                                   "external_ci": external_ci_gate,
                                   "source": "complete_claim"}, sort_keys=True), now))
        if publication_gate.get("required"):
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "task.review_gate",
                       json.dumps({"claim_id": claim_id,
                                   "gate": publication_gate.get("gate"),
                                   "publication": publication_gate,
                                   "source": "complete_claim"}, sort_keys=True), now))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "task.claim.completed",
                   json.dumps({"claim_id": claim_id, "evidence": evidence_obj,
                               "requested_status": requested_status or None,
                               "next_status": next_status,
                               "done_gate": done_gate,
                               "review_gate": (
                                   external_ci_gate.get("gate")
                                   if external_ci_gate.get("required") else
                                   publication_gate.get("gate")
                                   if publication_gate.get("required") else None),
                               "review_gates": [
                                   gate for gate in (
                                       external_ci_gate.get("gate")
                                       if external_ci_gate.get("required") else None,
                                       publication_gate.get("gate")
                                       if publication_gate.get("required") else None,
                                   ) if gate
                               ],
                               "work_session_gate": {
                                   key: value for key, value in work_session_gate.items()
                                   if key != "work_session"
                               },
                               "terminal_status_preserved": terminal_status_preserved},
                              sort_keys=True), now))
    response = {"completed": True, "claim_id": claim_id, "task_id": row["task_id"],
                "status": next_status, "git_state": git_state,
                "work_session_gate": {
                    key: value for key, value in work_session_gate.items()
                    if key != "work_session"
                }}
    if external_ci_gate.get("required"):
        response["review_gate"] = external_ci_gate.get("gate")
        response["external_ci"] = external_ci_gate
    if publication_gate.get("required"):
        response.setdefault("review_gate", publication_gate.get("gate"))
        response["publication"] = publication_gate
        response["review_gates"] = [
            gate for gate in (
                response.get("review_gate") if external_ci_gate.get("required") else None,
                publication_gate.get("gate"),
            ) if gate
        ]
    if done_gate:
        response["done_gate"] = done_gate
        response["warning"] = done_gate["message"]
    if push_check:
        response["push_verification"] = push_check
        if push_check.get("status") == push_verification.UNVERIFIED:
            response.setdefault("warnings", []).append(
                "push_unverified: could not confirm the branch/head_sha is on the "
                f"canonical remote ({push_check.get('reason') or 'unknown'}); "
                "completion allowed, flagged for reconcile.")
    if not finalize:
        return response
    return _finalize_complete_claim_response(
        response, evidence_obj, project, mission_project, actor)


def terminal_ack_claim_completion_in(c: sqlite3.Connection, runner_session_id: str,
                                     actor: str, now: float) -> Optional[Dict[str, Any]]:
    """Idempotently finish a stopping claim after the exact host terminal ack."""
    runner = c.execute("SELECT * FROM runner_sessions WHERE runner_session_id=?",
                       (runner_session_id,)).fetchone()
    if not runner or str(runner["status"] or "").lower() not in {
            "completed", "stopped", "failed", "expired", "killed", "exited"}:
        return None
    metadata = json.loads(runner["metadata_json"] or "{}")
    handoff = metadata.get("completion_handoff") or {}
    if str(handoff.get("execution_id") or "") != runner_session_id:
        return None
    claim_id = str(handoff.get("claim_id") or "")
    claim = c.execute("SELECT * FROM task_claims WHERE id=?", (claim_id,)).fetchone()
    if not claim:
        return None
    if claim["status"] == "completed":
        return {"completed": True, "idempotent": True, "claim_id": claim_id}
    if claim["status"] != "stopping" or claim["task_id"] != handoff.get("task_id"):
        return None
    evidence = handoff.get("evidence") if isinstance(handoff.get("evidence"), dict) else {}
    task_id = str(claim["task_id"])
    c.execute("UPDATE task_claims SET status='completed', completed_at=? WHERE id=?",
              (now, claim_id))
    c.execute("UPDATE resource_leases SET released_at=? WHERE resource_type='task' "
              "AND task_id=? AND agent_id=? AND released_at IS NULL",
              (now, task_id, claim["agent_id"]))
    c.execute("UPDATE tasks SET status='In Review', updated_at=? WHERE task_id=? "
              "AND status NOT IN ('Done','Cancelled','Canceled')", (now, task_id))
    ws_id = str(handoff.get("work_session_id") or "")
    if ws_id:
        c.execute("UPDATE work_sessions SET status='completed', completed_at=?, "
                  "updated_by=?, updated_at=? WHERE work_session_id=?",
                  (now, actor, now, ws_id))
    current_git = _store_facade()._load_git_state(c, task_id)
    updates = _store_facade()._preserve_provider_pr_evidence(current_git, {
        "branch": evidence.get("branch"), "head_sha": evidence.get("head_sha"),
        "pushed_at": now if evidence.get("head_sha") else None,
        "pr_number": evidence.get("pr_number"), "pr_url": evidence.get("pr_url"),
        "evidence": evidence,
    }, evidence)
    _store_facade()._upsert_git_state(c, task_id, updates)
    metadata["completion_handoff"] = {**handoff, "acknowledged_at": now,
                                      "acknowledged_by": actor}
    c.execute("UPDATE runner_sessions SET metadata_json=?, updated_at=? "
              "WHERE runner_session_id=?",
              (json.dumps(metadata, sort_keys=True), now, runner_session_id))
    c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (task_id, actor, "task.claim.completed_by_terminal_ack",
               json.dumps({"claim_id": claim_id, "execution_id": runner_session_id,
                           "work_session_id": ws_id or None}, sort_keys=True), now))
    return {"completed": True, "claim_id": claim_id, "task_id": task_id,
            "status": "In Review", "execution_id": runner_session_id}


def _surrender_claim_runner_leases_in(
        c: sqlite3.Connection, claim_id: str, task_id: str, actor: str,
        now: float, *, work_session_id: str = "") -> List[str]:
    """Fence heartbeat renewal for the exact implementation generation.

    The runner-session heartbeat remains the sole automatic stop clock.  Claim
    completion merely makes the bound rows immediately stale and records a
    durable fence which rejects late heartbeats until the existing lease-expiry
    reaper terminalizes them.
    """
    # Connect registers the supervised generation before a claim exists, then
    # late-binds the claim.  A completion racing that late heartbeat can still
    # see claim_id=NULL, so the Work Session binding is the exact durable join
    # for that production shape.  Never widen this to every runner for a task.
    rows = c.execute(
        "SELECT runner_session_id, claim_id, heartbeat_ttl_s, metadata_json "
        "FROM runner_sessions WHERE claim_id=? OR "
        "(COALESCE(claim_id,'')='' AND task_id=?)",
        (claim_id, task_id),
    ).fetchall()
    surrendered: List[str] = []
    for runner in rows:
        metadata = json.loads(runner["metadata_json"] or "{}")
        bound_work_session = str(metadata.get("work_session_id") or "")
        if work_session_id and bound_work_session != work_session_id:
            continue
        if not str(runner["claim_id"] or "") and (
                not work_session_id or bound_work_session != work_session_id):
            continue
        fence = metadata.get("lease_surrender") or {}
        if str(fence.get("claim_id") or "") == claim_id:
            surrendered.append(runner["runner_session_id"])
            continue
        metadata["lease_surrender"] = {
            "schema": "switchboard.runner_lease_surrender.v1",
            "claim_id": claim_id,
            "work_session_id": bound_work_session or None,
            "surrendered_at": now,
            "reason": "claim_completed",
        }
        ttl_s = max(10, int(runner["heartbeat_ttl_s"] or 60))
        c.execute(
            "UPDATE runner_sessions SET heartbeat_at=?, metadata_json=?, updated_at=? "
            "WHERE runner_session_id=?",
            (now - ttl_s, json.dumps(metadata, sort_keys=True), now,
             runner["runner_session_id"]),
        )
        surrendered.append(runner["runner_session_id"])
    if surrendered:
        c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, actor, "runner.lease_surrendered",
             json.dumps({"claim_id": claim_id,
                         "work_session_id": work_session_id or None,
                         "runner_session_ids": surrendered}, sort_keys=True), now),
        )
    return surrendered

def _finalize_complete_claim_response(
        response: Dict[str, Any], evidence_obj: Dict[str, Any], project: str,
        mission_project: str, actor: str) -> Dict[str, Any]:
    """Attach mission rollup after the claim transaction has committed.

    This intentionally runs outside the retry boundary: retrying the completed claim
    transaction after a later mission/read lock would turn success into
    ``claim is not active`` and could duplicate side effects.
    """
    deliverable_id = (evidence_obj.get("deliverable_id") or "").strip()
    milestone_id = (evidence_obj.get("milestone_id") or "").strip()
    mp = (evidence_obj.get("mission_project") or mission_project or "").strip()
    if not deliverable_id or not mp:
        matches = _store_facade()._find_deliverable_links_for_task(project, response["task_id"],
                                                   mission_project=mp,
                                                   deliverable_id=deliverable_id)
        if len(matches) == 1:
            deliverable_id = matches[0]["deliverable_id"]
            mp = matches[0]["mission_project"]
            if not milestone_id:
                milestone_id = (matches[0].get("milestone_id") or "").strip()
    if deliverable_id and mp:
        response["mission"] = _record_mission_claim_completion(
            mp, deliverable_id, project, response["task_id"], response["claim_id"],
            response["status"],
            milestone_id=milestone_id, actor=actor)
    return response

def abandon_claim(claim_id: str, reason: str,
                  actor: str = "system",
                  project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM task_claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            return {"error": "claim not found", "claim_id": claim_id}
        if row["status"] != "active":
            return {"error": "claim is not active", "claim_id": claim_id,
                    "status": row["status"]}
        c.execute("UPDATE task_claims SET status='abandoned', abandon_reason=? WHERE id=?",
                  (reason, claim_id))
        c.execute("UPDATE resource_leases SET released_at=? WHERE resource_type='task' "
                  "AND task_id=? AND agent_id=? AND released_at IS NULL",
                  (now, row["task_id"], row["agent_id"]))
        c.execute("UPDATE tasks SET status='Not Started', "
                  "assignee=CASE WHEN assignee=? THEN NULL ELSE assignee END, "
                  "updated_at=? WHERE task_id=? AND status='In Progress'",
                  (row["agent_id"], now, row["task_id"]))
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "task.claim.abandoned",
                   json.dumps({"claim_id": claim_id, "reason": reason}, sort_keys=True), now))
    return {"abandoned": True, "claim_id": claim_id, "task_id": row["task_id"]}

def _verify_completion_push(evidence_obj: Dict[str, Any],
                            project: str) -> Optional[Dict[str, Any]]:
    """Prove agent-reported branch/head_sha is actually on the canonical remote.

    Gated by ``PM_VERIFY_COMPLETION_PUSH`` (staged rollout on the live control
    plane): when disabled this returns ``None`` and completion behaves exactly as
    before (no network call), so dev/CI/test runs never reach GitHub.

    When enabled, runs OUTSIDE the completion DB transaction: this makes a GitHub
    API call, and network I/O must never hold the sqlite write lock on the shared
    box (HARDEN-32 class wedge). Fail-open to 'unverified' on any error -- the
    policy is fail-closed only on a *proven* absent ref, never on our own
    verification plumbing failing.
    """
    if os.environ.get("PM_VERIFY_COMPLETION_PUSH", "").strip().lower() not in (
            "1", "true", "yes", "on"):
        return None
    try:
        repo = _store_facade().get_project_github_repo(project)
    except Exception:
        repo = ""
    token = push_verification.github_token_from_env()
    try:
        return push_verification.verify_push_evidence(evidence_obj, repo, token)
    except Exception as e:
        return {"status": push_verification.UNVERIFIED,
                "schema": push_verification.SCHEMA,
                "reason": "verification_error", "detail": str(e)}

def _completion_push_absent_response(claim_id: str, evidence_obj: Dict[str, Any],
                                     push_check: Dict[str, Any], project: str,
                                     actor: str) -> Dict[str, Any]:
    """Fail-closed response when the completion branch/head_sha is provably NOT on
    the canonical remote -- committed-but-unpushed work, the silent-failed-push
    leak. Records an auditable ``task.complete_blocked_push`` activity row."""
    ref = (push_check.get("ref") or evidence_obj.get("head_sha")
           or evidence_obj.get("branch") or "")
    message = (
        f"Completion {push_check.get('ref_kind') or 'ref'} '{ref}' is not on the "
        f"canonical remote ({push_check.get('repo') or 'repo'}). Push the branch "
        "before completing the claim; committed-but-unpushed work is invisible to "
        "the fleet and never lands on the board.")
    response = {"completed": False,
                "reason": "push_not_on_remote",
                "failure_class": "stale_branch",
                "severity": "high",
                "message": message,
                "claim_id": claim_id,
                "push_verification": push_check,
                "override_field": "completion_evidence",
                "override_required": True}
    try:
        with _conn(project) as c:
            row = c.execute("SELECT * FROM task_claims WHERE id=?", (claim_id,)).fetchone()
            if not row:
                return {"error": "claim not found", "claim_id": claim_id}
            if row["status"] != "active":
                return {"error": "claim is not active", "claim_id": claim_id,
                        "status": row["status"]}
            response["task_id"] = row["task_id"]
            c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                      (row["task_id"], actor, "task.complete_blocked_push",
                       json.dumps({"claim_id": claim_id, "evidence": evidence_obj,
                                   **response}, sort_keys=True), time.time()))
    except Exception:
        pass
    return response

def complete_claim(claim_id: str, evidence: str = "", final_status: str = "",
                   actor: str = "system",
                   project: str = DEFAULT_PROJECT,
                   mission_project: str = "") -> Dict[str, Any]:
    s = _store_facade()
    evidence_obj = s._parse_evidence(evidence)
    # Verify the push BEFORE opening the claim transaction. A branch/head_sha that
    # is provably absent from the canonical remote fails closed here (stale_branch);
    # an unreachable remote is allowed through as 'unverified' and flagged.
    push_check = s._verify_completion_push(evidence_obj, project)
    if push_check and push_check.get("status") == push_verification.ABSENT:
        return s._completion_push_absent_response(
            claim_id, evidence_obj, push_check, project, actor)
    response = s._write_through(project, lambda: s._complete_claim_impl(
        claim_id, evidence=evidence, final_status=final_status, actor=actor,
        project=project, mission_project=mission_project, finalize=False,
        push_check=push_check))
    if not response.get("completed"):
        return response
    return s._finalize_complete_claim_response(
        response, evidence_obj, project, mission_project, actor)

def revoke_claim(claim_id: str, reason: str,
                 reassign_to: str = "", sort_order: Optional[int] = None,
                 partial_evidence: Any = None, notify: bool = True,
                 ack_deadline_minutes: float = 5,
                 expected_task_id: str = "",
                 actor: str = "switchboard/operator",
                 project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Operator override for an active task claim.

    Unlike abandon_claim(), revoke_claim() records that a human/operator took
    control, preserves partial evidence, optionally redirects the task, and
    sends the displaced holder an ack-required stop signal.
    """
    now = time.time()
    reason = (reason or "").strip() or "operator override"
    reassignee = (reassign_to or "").strip()
    evidence_obj = _store_facade()._parse_evidence(partial_evidence)
    notification = None
    with _conn(project) as c:
        row = c.execute("SELECT * FROM task_claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            return {"error": "claim not found", "claim_id": claim_id}
        if row["status"] != "active":
            return {"error": "claim is not active", "claim_id": claim_id,
                    "status": row["status"]}
        task_id = row["task_id"]
        if expected_task_id and task_id != expected_task_id:
            return {"error": "claim belongs to a different task", "claim_id": claim_id,
                    "task_id": task_id, "expected_task_id": expected_task_id}
        task = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not task:
            return {"error": "task not found", "task_id": task_id, "claim_id": claim_id}

        c.execute(
            "UPDATE task_claims SET status='revoked', completed_at=?, abandon_reason=? "
            "WHERE id=?",
            (now, f"revoked by {actor}: {reason}", claim_id),
        )
        c.execute(
            "UPDATE resource_leases SET released_at=? WHERE resource_type='task' "
            "AND task_id=? AND agent_id=? AND released_at IS NULL",
            (now, task_id, row["agent_id"]),
        )

        sets = ["status='Not Started'", "assignee=?", "updated_at=?"]
        vals: List[Any] = [reassignee or None, now]
        if sort_order is not None:
            sets.append("sort_order=?")
            vals.append(int(sort_order))
        vals.append(task_id)
        c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=? "
                  "AND status NOT IN ('Done', 'Cancelled', 'Canceled')", vals)

        git_state = None
        if evidence_obj:
            git_updates = {
                "branch": evidence_obj.get("branch"),
                "head_sha": evidence_obj.get("head_sha"),
                "pushed_at": now if evidence_obj.get("head_sha") else None,
                "pr_number": evidence_obj.get("pr_number"),
                "pr_url": evidence_obj.get("pr_url"),
                "evidence": {"operator_revoke": evidence_obj},
            }
            if any(v is not None for v in git_updates.values()):
                git_state = _store_facade()._upsert_git_state(c, task_id, git_updates)

        payload = {
            "claim_id": claim_id,
            "task_id": task_id,
            "revoked_agent": row["agent_id"],
            "reason": reason,
            "reassigned_to": reassignee or None,
            "sort_order": sort_order,
            "partial_evidence": evidence_obj,
        }
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "task.claim.revoked",
                   json.dumps(payload, sort_keys=True), now))
        updated_task = _task_row(c.execute("SELECT * FROM tasks WHERE task_id=?",
                                           (task_id,)).fetchone())
        updated_task["git_state"] = git_state or _store_facade()._load_git_state(c, task_id)
        updated_task["active_claims"] = _active_task_claims_in(c, task_id, now)

    if notify:
        msg = (f"Operator revoked claim {claim_id} on {updated_task['task_id']}. "
               f"Stop work, preserve any local evidence, and ack this message. "
               f"Reason: {reason}.")
        if reassignee:
            msg += f" Redirected to {reassignee}."
        notification = _store_facade().send_agent_message(
            actor,
            row["agent_id"],
            msg,
            task_id=updated_task["task_id"],
            requires_ack=True,
            ack_deadline_minutes=ack_deadline_minutes,
            signal="claim_revoked",
            priority=20,
            project=project,
        )
    return {
        "revoked": True,
        "claim_id": claim_id,
        "task_id": updated_task["task_id"],
        "revoked_agent": row["agent_id"],
        "reassigned_to": reassignee or None,
        "task": updated_task,
        "notification": notification,
    }


# --- ARCH-MS-50: leases + completion evidence ---
def _active_resource_leases_in(c: sqlite3.Connection, now: float,
                               resource_type: Optional[str] = None) -> List[Dict[str, Any]]:
    if resource_type:
        rows = c.execute("SELECT * FROM resource_leases WHERE released_at IS NULL "
                         "AND resource_type=?", (resource_type,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM resource_leases WHERE released_at IS NULL").fetchall()
    return [dict(r) for r in rows if now < r["claimed_at"] + r["ttl_seconds"]]


def claim_resources(agent_id: str, resource_type: str, names: List[str],
                    task_id: Optional[str] = None, ttl_seconds: int = 1800,
                    principal_id: str = "", actor: str = "system",
                    idem_key: str = "",
                    project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    clean_names = sorted({n.strip() for n in names if n and n.strip()})
    payload = {"agent_id": agent_id, "resource_type": resource_type, "names": clean_names,
               "task_id": task_id, "ttl_seconds": ttl_seconds}
    if not clean_names:
        return {"error": "no resource names given"}
    with _conn(project) as c:
        hit = _store_facade()._idem_hit(c, "claim", idem_key, actor, payload)
        if hit is not None:
            return hit
        wanted = set(clean_names)
        for lease in _active_resource_leases_in(c, now, resource_type):
            if lease["agent_id"] == agent_id:
                continue
            overlap = wanted & set(json.loads(lease["names"] or "[]"))
            if overlap:
                expires_at = lease["claimed_at"] + lease["ttl_seconds"]
                response = {"conflict": lease["agent_id"], "resource_type": resource_type,
                            "names": sorted(overlap), "task_id": lease.get("task_id"),
                            "retry_after_seconds": max(5, int((expires_at - now) / 2))}
                _store_facade()._idem_store(c, "claim", idem_key, actor, payload, response)
                return response
        lease_id = "lease-" + uuid.uuid4().hex[:16]
        c.execute(
            "INSERT INTO resource_leases(id, agent_id, principal_id, task_id, resource_type, "
            "names, claimed_at, ttl_seconds) VALUES (?,?,?,?,?,?,?,?)",
            (lease_id, agent_id, principal_id or None, task_id, resource_type,
             json.dumps(clean_names), now, max(1, int(ttl_seconds or 1800))),
        )
        response = {"lease_id": lease_id, "agent_id": agent_id, "resource_type": resource_type,
                    "names": clean_names, "task_id": task_id, "claimed_at": now,
                    "expires_at": now + max(1, int(ttl_seconds or 1800))}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (task_id, actor, "lease.claimed", json.dumps(response, sort_keys=True), now))
        _store_facade()._idem_store(c, "claim", idem_key, actor, payload, response)
        return response


def check_resources(resource_type: str, names: List[str],
                    project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    now = time.time()
    wanted = {n.strip() for n in names if n and n.strip()}
    out: List[Dict[str, Any]] = []
    with _conn(project) as c:
        for lease in _active_resource_leases_in(c, now, resource_type):
            for name in wanted & set(json.loads(lease["names"] or "[]")):
                out.append({"resource_type": resource_type, "name": name,
                            "held_by": lease["agent_id"], "lease_id": lease["id"],
                            "task_id": lease.get("task_id"),
                            "expires_at": lease["claimed_at"] + lease["ttl_seconds"]})
    return sorted(out, key=lambda x: x["name"])


def release_resource_lease(lease_id: str, actor: str = "system",
                           project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    now = time.time()
    with _conn(project) as c:
        row = c.execute("SELECT * FROM resource_leases WHERE id=?", (lease_id,)).fetchone()
        if not row:
            return {"error": "lease not found", "lease_id": lease_id}
        if row["released_at"] is not None:
            return {"released": False, "lease_id": lease_id, "note": "already released"}
        c.execute("UPDATE resource_leases SET released_at=? WHERE id=?", (now, lease_id))
        payload = {"lease_id": lease_id, "agent_id": row["agent_id"],
                   "resource_type": row["resource_type"], "names": json.loads(row["names"] or "[]")}
        c.execute("INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (row["task_id"], actor, "lease.released", json.dumps(payload, sort_keys=True), now))
    return {"released": True, "lease_id": lease_id}


def list_active_resource_leases(project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    now = time.time()
    with _conn(project) as c:
        leases = _active_resource_leases_in(c, now)
    return [{"lease_id": l["id"], "agent_id": l["agent_id"], "task_id": l.get("task_id"),
             "resource_type": l["resource_type"], "names": json.loads(l["names"] or "[]"),
             "expires_at": l["claimed_at"] + l["ttl_seconds"]} for l in leases]


RISK_ORDER = {"low": 1, "medium": 2, "med": 2, "high": 3, "critical": 4}
CAPABILITY_RE = re.compile(
    r"(?:requires?\s+capabilit(?:y|ies)|required\s+capabilit(?:y|ies)|capabilities)\s*[:=]\s*([^\n.;]+)",
    re.I,
)


def _risk_value(risk: str) -> int:
    return RISK_ORDER.get((risk or "").strip().lower(), 0)


def _task_required_capabilities(task: Dict[str, Any]) -> List[str]:
    dispatch_state = ((task.get("agent_state") or {}).get("dispatch") or {})
    raw = (dispatch_state.get("required_capabilities") or
           dispatch_state.get("capabilities") or [])
    caps = _store_facade().coerce_csv_list(raw)
    if not caps:
        text = "\n".join(str(task.get(k) or "") for k in (
            "description", "entry_criteria", "exit_criteria", "deliverable"))
        for m in CAPABILITY_RE.finditer(text):
            caps.extend(_store_facade().coerce_csv_list(m.group(1)))
    return sorted({c.strip().lower() for c in caps if c and c.strip()})


def _evidence_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "ok", "pass", "passed", "clean"}


def _evidence_sequence(value: Any) -> List[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _completion_evidence_has_tests(evidence: Dict[str, Any],
                                   session: Dict[str, Any]) -> bool:
    keys = ("tests", "test_commands", "verification_commands", "checks")
    if any(_evidence_sequence(evidence.get(key)) for key in keys):
        return True
    for key in ("verification", "verification_note", "test_results"):
        if str(evidence.get(key) or "").strip():
            return True
    hygiene = session.get("hygiene") or {}
    if any(_evidence_sequence(hygiene.get(key)) for key in keys):
        return True
    return bool(str(hygiene.get("verification") or "").strip())


def _executed_test_run_candidates(evidence: Dict[str, Any],
                                  session: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []

    def add(value: Any, source: str) -> None:
        if value in (None, ""):
            return
        if isinstance(value, dict):
            row = dict(value)
            row.setdefault("_source", source)
            candidates.append(row)
            return
        if isinstance(value, list):
            for item in value:
                add(item, source)

    for key in (
        "executed_test_run",
        "executed_test_runs",
        "test_run",
        "test_runs",
        "test_results",
        "verification_run",
        "verification_runs",
    ):
        add(evidence.get(key), f"evidence.{key}")
    hygiene = (session or {}).get("hygiene") or {}
    for key in (
        "executed_test_run",
        "executed_test_runs",
        "test_run",
        "test_runs",
        "test_results",
        "verification_run",
        "verification_runs",
    ):
        add(hygiene.get(key), f"hygiene.{key}")
    return candidates


def _executed_test_run_commands(run: Dict[str, Any]) -> List[Any]:
    commands: List[Any] = []
    for key in ("commands", "test_commands", "verification_commands", "checks"):
        commands.extend(_evidence_sequence(run.get(key)))
    if run.get("command") not in (None, ""):
        commands.append(run.get("command"))
    return [cmd for cmd in commands if str(cmd or "").strip()]


def _executed_test_run_has_output_hash(run: Dict[str, Any]) -> bool:
    for key in (
        "output_hash",
        "output_sha256",
        "stdout_sha256",
        "stderr_sha256",
        "log_hash",
        "logs_hash",
        "artifact_hash",
        "result_hash",
    ):
        if str(run.get(key) or "").strip():
            return True
    return False


def _executed_test_run_succeeded(run: Dict[str, Any]) -> bool:
    if run.get("executed") is False:
        return False
    if run.get("ok") is True or run.get("passed") is True:
        return True
    exit_code = run.get("exit_code", run.get("returncode"))
    if exit_code not in (None, ""):
        try:
            return int(exit_code) == 0
        except (TypeError, ValueError):
            return False
    status = str(run.get("status") or run.get("conclusion") or run.get("result") or "").strip().lower()
    return status in {"pass", "passed", "success", "succeeded", "ok", "green", "completed"}


def _executed_test_run_gate(evidence: Dict[str, Any],
                            session: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    candidates = _executed_test_run_candidates(evidence, session)
    problems: List[Dict[str, Any]] = []
    session_id = str((session or {}).get("work_session_id") or "").strip()
    session_branch = str((session or {}).get("branch") or "").strip()
    session_head = str((session or {}).get("head_sha") or "").strip()
    for run in candidates:
        source = run.get("_source")
        run_id = str(run.get("run_id") or run.get("id") or "").strip() or None
        run_schema = str(run.get("schema") or "").strip()
        commands = _executed_test_run_commands(run)
        run_problems: List[Dict[str, Any]] = []
        if run_schema and run_schema != EXECUTED_TEST_RUN_SCHEMA:
            run_problems.append({"reason": "unknown_test_run_schema",
                                 "message": "Executed test run schema is not recognized.",
                                 "schema": run_schema})
        if not commands:
            run_problems.append({"reason": "missing_test_commands",
                                 "message": "Executed test run must include the command(s) that ran."})
        if not _executed_test_run_succeeded(run):
            run_problems.append({"reason": "test_run_failed",
                                 "message": "Executed test run did not record a passing result.",
                                 "status": run.get("status") or run.get("conclusion"),
                                 "exit_code": run.get("exit_code", run.get("returncode"))})
        if not _executed_test_run_has_output_hash(run):
            run_problems.append({"reason": "missing_test_output_hash",
                                 "message": "Executed test run must include an output/log/artifact hash."})
        if not any(str(run.get(key) or "").strip() for key in ("completed_at", "executed_at", "finished_at")):
            run_problems.append({"reason": "missing_test_completion_time",
                                 "message": "Executed test run must include completed_at/executed_at/finished_at."})
        run_session_id = str(run.get("work_session_id") or "").strip()
        if session_id and run_session_id and run_session_id != session_id:
            run_problems.append({"reason": "wrong_test_work_session",
                                 "message": "Executed test run belongs to a different Work Session.",
                                 "test_work_session_id": run_session_id,
                                 "work_session_id": session_id})
        run_branch = str(run.get("branch") or "").strip()
        if session_branch and run_branch and run_branch != session_branch:
            run_problems.append({"reason": "stale_test_branch",
                                 "message": "Executed test run branch does not match the Work Session.",
                                 "test_branch": run_branch,
                                 "work_session_branch": session_branch})
        run_head = str(run.get("head_sha") or "").strip()
        if session_head and run_head and run_head != session_head:
            run_problems.append({"reason": "stale_test_head_sha",
                                 "message": "Executed test run head_sha does not match the Work Session.",
                                 "test_head_sha": run_head,
                                 "work_session_head_sha": session_head})
        if not run_problems:
            clean = {k: v for k, v in run.items() if k != "_source"}
            return {"ok": True, "schema": EXECUTED_TEST_RUN_SCHEMA,
                    "source": source, "run_id": run_id, "run": clean}
        problems.append({"source": source, "run_id": run_id, "problems": run_problems})
    return {"ok": False, "schema": EXECUTED_TEST_RUN_SCHEMA,
            "reason": "missing_executed_test_run" if not candidates else "invalid_executed_test_run",
            "message": (
                "Completion evidence must include a passing executed test run with commands, "
                "completion time, and output/log hash."
            ),
            "problems": problems}


def _completion_evidence_has_diff_check(evidence: Dict[str, Any],
                                        session: Dict[str, Any]) -> bool:
    for key in ("git_diff_check", "diff_check", "diff_check_clean"):
        if key in evidence and _evidence_truthy(evidence.get(key)):
            return True
    for item in _evidence_sequence(evidence.get("checks")):
        text = json.dumps(item, sort_keys=True) if isinstance(item, dict) else str(item)
        if "git diff --check" in text and not any(word in text.lower() for word in ("fail", "failed")):
            return True
    for item in _evidence_sequence(evidence.get("verification_commands")):
        if "git diff --check" in str(item):
            return True
    hygiene = session.get("hygiene") or {}
    for key in ("git_diff_check", "diff_check", "diff_check_clean"):
        if key in hygiene and _evidence_truthy(hygiene.get(key)):
            return True
    return False


def _completion_has_push_or_review_evidence(evidence: Dict[str, Any]) -> bool:
    if evidence.get("pr_url") or evidence.get("pr_number"):
        return True
    if evidence.get("pushed_at") or evidence.get("remote_ref"):
        return True
    offline = evidence.get("offline_evidence")
    return bool(offline if isinstance(offline, dict) else str(offline or "").strip())


def _active_leases_in(c, now: float) -> List[Dict[str, Any]]:
    """Active leases using an existing connection — not released and not TTL-expired."""
    rows = c.execute("SELECT * FROM file_leases WHERE released_at IS NULL").fetchall()
    return [dict(r) for r in rows if now < r["claimed_at"] + r["ttl_minutes"] * 60]


def claim_files(agent_id: str, files: List[str], task_id: Optional[str] = None,
                ttl_minutes: int = 30, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Claim a set of file paths for an agent. Returns {lease_id, files, expires_at} on
    success, or {conflict, task_id, files, retry_after_seconds} if any file is held by
    another active lease. Same agent claiming its own files is idempotent (no conflict)."""
    now = time.time()
    file_set = set(files)
    with _conn(project) as c:
        for lease in _active_leases_in(c, now):
            if lease["agent_id"] == agent_id:
                continue
            held = set(json.loads(lease["files"] or "[]"))
            overlap = file_set & held
            if overlap:
                expires_at = lease["claimed_at"] + lease["ttl_minutes"] * 60
                remaining = max(0.0, expires_at - now)
                return {"conflict": lease["agent_id"], "task_id": lease.get("task_id"),
                        "files": sorted(overlap),
                        "retry_after_seconds": max(30, int(remaining / 2))}
        lease_id = f"lease-{agent_id}-{int(now)}"
        c.execute(
            "INSERT OR REPLACE INTO file_leases(id, agent_id, task_id, files, claimed_at, ttl_minutes) "
            "VALUES (?,?,?,?,?,?)",
            (lease_id, agent_id, task_id, json.dumps(sorted(files)), now, ttl_minutes),
        )
    expires_at = now + ttl_minutes * 60
    return {"lease_id": lease_id, "agent_id": agent_id, "task_id": task_id,
            "files": sorted(files), "expires_at": expires_at, "ttl_minutes": ttl_minutes}


def release_files(lease_id: str, project: str = DEFAULT_PROJECT) -> Dict[str, Any]:
    """Release a lease by id. Returns {released: true} or {error: ...}."""
    now = time.time()
    with _conn(project) as c:
        cur = c.execute(
            "UPDATE file_leases SET released_at=? WHERE id=? AND released_at IS NULL",
            (now, lease_id),
        )
        if cur.rowcount == 0:
            r = c.execute("SELECT id FROM file_leases WHERE id=?", (lease_id,)).fetchone()
            if r:
                return {"error": "lease already released", "lease_id": lease_id}
            return {"error": "lease not found", "lease_id": lease_id}
    return {"released": True, "lease_id": lease_id}


def check_files(files: List[str], project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """For each file path, return its holder if held by an active lease. Files not held
    are omitted. [{file, held_by, task_id, expires_at}]."""
    now = time.time()
    file_set = set(files)
    results = []
    with _conn(project) as c:
        for lease in _active_leases_in(c, now):
            held = set(json.loads(lease["files"] or "[]"))
            for f in file_set & held:
                results.append({"file": f, "held_by": lease["agent_id"],
                                 "task_id": lease.get("task_id"),
                                 "expires_at": lease["claimed_at"] + lease["ttl_minutes"] * 60})
    return sorted(results, key=lambda x: x["file"])


def list_active_leases(project: str = DEFAULT_PROJECT) -> List[Dict[str, Any]]:
    """All active leases board-wide (not released, not TTL-expired)."""
    now = time.time()
    with _conn(project) as c:
        leases = _active_leases_in(c, now)
    out = []
    for lease in leases:
        out.append({"lease_id": lease["id"], "agent_id": lease["agent_id"],
                    "task_id": lease.get("task_id"),
                    "files": json.loads(lease["files"] or "[]"),
                    "expires_at": lease["claimed_at"] + lease["ttl_minutes"] * 60})
    return sorted(out, key=lambda x: x["lease_id"])


class StoreClaimsRepository:
    """SQL-backed claim lifecycle repository (ARCH-MS-32)."""

    def claim_task(self, task_id: str, agent_id: str, **kwargs) -> dict[str, Any]:
        return claim_task(task_id, agent_id, **kwargs)

    def claim_next(self, agent_id: str, **kwargs) -> dict[str, Any]:
        return claim_next(agent_id, **kwargs)

    def complete_claim(self, claim_id: str, **kwargs) -> dict[str, Any]:
        return complete_claim(claim_id, **kwargs)

    def abandon_claim(self, claim_id: str, reason: str, **kwargs) -> dict[str, Any]:
        return abandon_claim(claim_id, reason, **kwargs)

    def revoke_claim(self, claim_id: str, reason: str, **kwargs) -> dict[str, Any]:
        return revoke_claim(claim_id, reason, **kwargs)


def default_claims_repository() -> StoreClaimsRepository:
    return StoreClaimsRepository()


__all__ = [
    "StoreClaimsRepository",
    "default_claims_repository",
    "_active_task_claims_in",
    "claim_binding_target",
    "_attach_work_session_claim_in",
    "_complete_claim_work_session_gate_in",
    "_claim_task_impl",
    "claim_task",
    "claim_next",
    "_complete_claim_impl",
    "_finalize_complete_claim_response",
    "abandon_claim",
    "_verify_completion_push",
    "_completion_push_absent_response",
    "complete_claim",
    "revoke_claim",
    "_record_mission_claim_completion",
    "_claim_next_mission_scoped",
    "_active_resource_leases_in",
    "claim_resources",
    "check_resources",
    "release_resource_lease",
    "list_active_resource_leases",
    "RISK_ORDER",
    "CAPABILITY_RE",
    "_risk_value",
    "_task_required_capabilities",
    "_evidence_truthy",
    "_evidence_sequence",
    "_completion_evidence_has_tests",
    "_executed_test_run_candidates",
    "_executed_test_run_commands",
    "_executed_test_run_has_output_hash",
    "_executed_test_run_succeeded",
    "_executed_test_run_gate",
    "_completion_evidence_has_diff_check",
    "_completion_has_push_or_review_evidence",
    "_active_leases_in",
    "claim_files",
    "release_files",
    "check_files",
    "list_active_leases",
]
