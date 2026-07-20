"""Mission coordinator tick — deliverable-scoped dispatch loop (DELIVERABLES-7).

COORD-3: every tick persists an explainable coordinator decision
(``switchboard.coordinator_decision.v1``) so operators can see why an action was
chosen or skipped without reading chat transcripts.

COORD-6: when the tick requires a human exception, deliver a structured
operator notification (task + failed condition + choices + minimum decision)
via ``coordinator_escalation`` — normal session/monitor progress stays autonomous.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

ACTION_PRIORITY: Dict[str, int] = {
    "approve_breakdown": 1,
    "repair_task_link": 2,
    "verify_merge_provenance": 3,
    "claim_task": 4,
    "resume_or_claim": 5,
    "propose_breakdown": 6,
}

HUMAN_ESCALATION = frozenset({
    "approve_breakdown",
    "repair_task_link",
    "propose_breakdown",
})

AUTO_CLAIM = frozenset({"claim_task", "resume_or_claim"})

POLICY_RULES = {
    "mission_complete": "coord.tick.mission_complete",
    "idle": "coord.tick.idle_no_actions",
    "human_required": "coord.tick.human_escalation",
    "monitor": "coord.tick.monitor_in_review",
    "dispatch_ready": "coord.tick.dispatch_priority",
    "unknown_action": "coord.tick.unknown_action",
    "dispatch_blocked": "coord.tick.dispatch_blocked",
    "session_ensured": "coord.tick.ensure_task_session",
    "session_transitioning": "coord.tick.transition_task_session",
}


def _lifecycle_role(store_mod: Any, project: str, task_id: str) -> str:
    """Return the one role the lifecycle owner must ensure for this task."""
    list_remediations = getattr(store_mod, "list_review_remediations", None)
    if callable(list_remediations):
        try:
            queued = list_remediations(
                task_id=task_id, status="queued", project=project)
            if queued:
                return "remediation"
        except Exception:
            pass
    return "implementation"


def _normalize_policy(policy: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = {
        "auto_refresh_brief": True,
        "auto_start": True,
        "monitor_in_review": True,
        "allowed_lanes": [],
        "denied_lanes": [],
        "target_task_id": "",
        "target_project_id": "",
        "target_milestone_id": "",
    }
    if isinstance(policy, dict):
        base.update({k: v for k, v in policy.items() if k in base})
    lanes = base.get("allowed_lanes")
    if isinstance(lanes, str):
        lanes = lanes.split(",")
    base["allowed_lanes"] = sorted({
        str(lane).strip().upper() for lane in (lanes or []) if str(lane).strip()
    })
    denied = base.get("denied_lanes")
    if isinstance(denied, str):
        denied = denied.split(",")
    base["denied_lanes"] = sorted({
        str(lane).strip().upper() for lane in (denied or []) if str(lane).strip()
    })
    base["target_task_id"] = str(base.get("target_task_id") or "").strip().upper()
    base["target_project_id"] = str(base.get("target_project_id") or "").strip()
    base["target_milestone_id"] = str(base.get("target_milestone_id") or "").strip()
    return base


_NON_FLOW_LINK_ROLES = frozenset({
    "foundation", "historical", "moved", "parked", "skipped",
})


def _explicit_target_actions(mission_status: Dict[str, Any],
                             policy: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Opt one named nonblocking flow task/milestone into a tick.

    Generic deliverable drain never reaches these links. Context roles and an
    explicit ``dispatch_eligible=false`` remain fail-closed even when targeted.
    """
    target_task = policy.get("target_task_id") or ""
    target_project = policy.get("target_project_id") or ""
    target_milestone = policy.get("target_milestone_id") or ""
    if not target_task and not target_milestone:
        return list(mission_status.get("next_actions") or [])
    milestone_statuses = {
        str(row.get("id") or ""): str(row.get("status") or "").strip().lower()
        for row in (mission_status.get("milestones") or [])
    }
    actions: List[Dict[str, Any]] = []
    for link in mission_status.get("linked_tasks") or []:
        task_id = str(link.get("task_id") or "").upper()
        milestone_id = str(link.get("milestone_id") or "")
        if target_task and task_id != target_task:
            continue
        if target_project and str(link.get("project_id") or "") != target_project:
            continue
        if target_milestone and milestone_id != target_milestone:
            continue
        role = str(link.get("role") or "").strip().lower()
        metadata = link.get("metadata") or {}
        if (milestone_statuses.get(milestone_id) == "skipped"
                or role in _NON_FLOW_LINK_ROLES
                or metadata.get("dispatch_eligible") is False):
            continue
        detail = link.get("task_detail") or {}
        if detail.get("error"):
            continue
        status = detail.get("status")
        claims = detail.get("active_claims") or []
        dependency = detail.get("dependency_state") or {}
        lane = detail.get("workstream") or detail.get("_wsId")
        common = {
            "project_id": link.get("project_id"),
            "task_id": detail.get("task_id") or task_id,
            "title": detail.get("title"),
            "milestone_id": link.get("milestone_id"),
            "lane": lane,
            "explicit_target": True,
            "automatic": True,
            "attention": False,
            "delivery_impact": "none",
        }
        if status == "Not Started" and dependency.get("ready") and not claims:
            actions.append({
                **common, "action": "claim_task", "owner_type": "agent",
                "label": "Agent will claim the explicitly targeted task",
                "reason": "Explicit task/milestone policy opt-in",
            })
        elif status == "In Progress" and not claims:
            actions.append({
                **common, "action": "resume_or_claim", "owner_type": "agent",
                "label": "Agent will resume the explicitly targeted task",
                "reason": "Explicit task/milestone policy opt-in",
            })
        elif status == "In Review":
            actions.append({
                **common, "action": "verify_merge_provenance",
                "owner_type": "coordinator",
                "label": "Coordinator will verify targeted merge provenance",
                "reason": "Explicit task/milestone policy opt-in",
            })
    return actions


def _scope_mission_status(mission_status: Dict[str, Any],
                          policy: Dict[str, Any]) -> Dict[str, Any]:
    scoped = dict(mission_status)
    actions = _explicit_target_actions(mission_status, policy)
    allowed_lanes = set(policy.get("allowed_lanes") or [])
    denied_lanes = set(policy.get("denied_lanes") or [])
    if allowed_lanes:
        actions = [
            action for action in actions
            if str(action.get("lane") or "").strip().upper() in allowed_lanes
        ]
    if denied_lanes:
        actions = [
            action for action in actions
            if not action.get("lane")
            or str(action.get("lane")).strip().upper() not in denied_lanes
        ]
    scoped["next_actions"] = actions
    scoped["coordinator_scope"] = {
        "mode": ("explicit" if policy.get("target_task_id")
                 or policy.get("target_milestone_id") else "deliverable"),
        "allowed_lanes": sorted(allowed_lanes),
        "denied_lanes": sorted(denied_lanes),
        "target_task_id": policy.get("target_task_id") or None,
        "target_project_id": policy.get("target_project_id") or None,
        "target_milestone_id": policy.get("target_milestone_id") or None,
        "candidate_count": len(actions),
    }
    return scoped


def pick_coordinator_action(next_actions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Choose the highest-priority next action from mission_status."""
    if not next_actions:
        return None
    return sorted(
        next_actions,
        key=lambda a: (ACTION_PRIORITY.get(a.get("action") or "", 99), a.get("task_id") or ""),
    )[0]


def coordinator_tick_plan(mission_status: Dict[str, Any],
                          policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Pure plan: what the coordinator would do without side effects."""
    pol = _normalize_policy(policy)
    mission_status = _scope_mission_status(mission_status, pol)
    progress = mission_status.get("progress") or {}
    dispatch_scope = mission_status.get("dispatch_scope") or {}
    linked = int(dispatch_scope.get(
        "blocking_task_count", progress.get("linked_task_count") or 0))
    done_ratio = float(dispatch_scope.get(
        "blocking_done_with_proof_ratio", progress.get("done_with_proof_ratio") or 0.0))
    selected = pick_coordinator_action(mission_status.get("next_actions") or [])
    if linked > 0 and done_ratio >= 1.0 and not selected:
        return {
            "status": "mission_complete",
            "deliverable_id": mission_status.get("deliverable_id"),
            "reason": "All linked tasks have terminal Done provenance",
        }
    if not selected:
        return {
            "status": "idle",
            "deliverable_id": mission_status.get("deliverable_id"),
            "reason": "No coordinator actions",
            "retry_after_seconds": 120,
        }
    action = selected.get("action")
    plan: Dict[str, Any] = {
        "status": "planned",
        "deliverable_id": mission_status.get("deliverable_id"),
        "selected_action": selected,
        "auto_refresh_brief": bool(pol["auto_refresh_brief"]),
        "scope": mission_status.get("coordinator_scope") or {},
    }
    if action in HUMAN_ESCALATION:
        plan["status"] = "human_required"
        plan["escalations"] = [selected]
        plan["retry_after_seconds"] = 300
    elif action == "verify_merge_provenance":
        plan["status"] = "monitor"
        plan["monitors"] = [selected]
        plan["retry_after_seconds"] = 60
    elif action in AUTO_CLAIM:
        plan["status"] = "dispatch_ready"
        plan["dispatch"] = selected
        plan["auto_start"] = bool(pol["auto_start"])
    else:
        plan["status"] = "unknown_action"
        plan["retry_after_seconds"] = 120
    return plan


def _skipped_alternatives(mission_status: Dict[str, Any],
                          selected: Optional[Dict[str, Any]],
                          plan_status: str = "") -> List[Dict[str, Any]]:
    selected_action = (selected or {}).get("action")
    selected_task = (selected or {}).get("task_id")
    selected_priority = ACTION_PRIORITY.get(selected_action or "", 99)
    skipped: List[Dict[str, Any]] = []
    for action in mission_status.get("next_actions") or []:
        if not isinstance(action, dict):
            continue
        if (action.get("action") == selected_action
                and action.get("task_id") == selected_task):
            continue
        priority = ACTION_PRIORITY.get(action.get("action") or "", 99)
        if plan_status == "mission_complete":
            reason = "mission_already_complete"
        elif priority > selected_priority:
            reason = "lower_action_priority"
        elif priority == selected_priority:
            reason = "task_id_tiebreak"
        else:
            reason = "not_selected_by_planner"
        skipped.append({
            "action": action.get("action"),
            "task_id": action.get("task_id"),
            "reason": reason,
            "priority": priority,
            "candidate": dict(action),
            "candidate_reason": action.get("reason") or action.get("detail"),
        })
    return skipped


def _record_tick_decision(
    store_mod: Any,
    *,
    mission_project: str,
    mission_status: Dict[str, Any],
    plan: Dict[str, Any],
    result: Dict[str, Any],
    policy: Dict[str, Any],
    coordinator_agent_id: str,
    actor: str,
    idem_key: str = "",
) -> Optional[Dict[str, Any]]:
    """Persist one explainable decision for this tick; never raise into the tick path."""
    deliverable_id = (mission_status.get("deliverable_id")
                      or plan.get("deliverable_id")
                      or result.get("deliverable_id")
                      or "")
    selected = (plan.get("selected_action")
                or plan.get("dispatch")
                or ((plan.get("escalations") or [None])[0])
                or ((plan.get("monitors") or [None])[0]))
    status = result.get("status") or plan.get("status") or "unknown"
    policy_rule = POLICY_RULES.get(status, f"coord.tick.{status}")
    chosen_action = {
        "action": (selected or {}).get("action") or status,
        "status": status,
        "task_id": (selected or {}).get("task_id"),
        "dispatch": result.get("dispatch"),
        "retry_after_seconds": result.get("retry_after_seconds"),
    }
    inputs_snapshot = {
        "deliverable_id": deliverable_id,
        "progress": mission_status.get("progress") or {},
        "next_actions": mission_status.get("next_actions") or [],
        "blockers": mission_status.get("blockers") or [],
        "action_priority": dict(ACTION_PRIORITY),
        "selection_tiebreak": ["task_id"],
        "policy": {
            "auto_start": bool(policy.get("auto_start")),
            "auto_refresh_brief": bool(policy.get("auto_refresh_brief")),
            "monitor_in_review": bool(policy.get("monitor_in_review")),
            "allowed_lanes": policy.get("allowed_lanes") or [],
            "denied_lanes": policy.get("denied_lanes") or [],
            "target_task_id": policy.get("target_task_id") or "",
            "target_project_id": policy.get("target_project_id") or "",
            "target_milestone_id": policy.get("target_milestone_id") or "",
        },
        "narrative_source_fingerprint": (
            (mission_status.get("mission_brief") or {}).get("source_fingerprint")
        ),
        "plan_status": plan.get("status"),
        "plan_reason": plan.get("reason"),
    }
    decision_kind = {
        "human_required": "human_escalation",
        "monitor": "monitor",
        "session_ensured": "action",
        "session_transitioning": "action",
        "dispatch_ready": "recommendation",
        "dispatch_blocked": "skip",
        "idle": "skip",
        "mission_complete": "recommendation",
        "unknown_action": "skip",
    }.get(status, "recommendation")
    task_id = (selected or {}).get("task_id") or ""
    try:
        decision = store_mod.record_coordinator_decision(
            author=coordinator_agent_id or actor or "coordinator",
            title=f"Coordinator tick: {chosen_action['action']}",
            inputs_snapshot=inputs_snapshot,
            policy_rule=policy_rule,
            chosen_action=chosen_action,
            skipped_alternatives=_skipped_alternatives(
                mission_status,
                selected if isinstance(selected, dict) else None,
                plan_status=plan.get("status") or "",
            ),
            result={
                "status": status,
                "executed": result.get("executed") or [],
                "escalations": result.get("escalations") or [],
                "monitors": result.get("monitors") or [],
                "dispatch": result.get("dispatch"),
                "human_notifications": result.get("human_notifications") or [],
                "retry_after_seconds": result.get("retry_after_seconds"),
            },
            project=mission_project,
            task_id=task_id,
            deliverable_id=deliverable_id,
            coordinator_agent_id=coordinator_agent_id or actor,
            decision_kind=decision_kind,
            stable_key=idem_key or f"{deliverable_id}:{status}:{task_id}:{int(time.time() // 60)}",
            context=(plan.get("reason")
                     or f"Mission coordinator tick on {deliverable_id or mission_project}"),
            rationale=f"Policy {policy_rule} selected {chosen_action['action']} "
                      f"({len(inputs_snapshot['next_actions'])} candidate action(s)).",
        )
    except Exception as exc:  # noqa: BLE001 — decision log must not fail the tick
        return {"error": "decision_log_failed", "message": str(exc)}
    return decision


def run_coordinator_tick(
    mission_status: Dict[str, Any],
    *,
    mission_project: str,
    coordinator_agent_id: str = "",
    actor: str = "system",
    policy: Optional[Dict[str, Any]] = None,
    store_mod: Any = None,
    task_starter: Any = None,
    idem_key: str = "",
) -> Dict[str, Any]:
    """Execute one deliverable-scoped coordinator tick with auditing."""
    import mission_narrative
    if store_mod is None:
        import store as store_mod

    pol = _normalize_policy(policy)
    mission_status = _scope_mission_status(mission_status, pol)
    deliverable_id = mission_status.get("deliverable_id") or ""
    plan = coordinator_tick_plan(mission_status, policy=pol)
    executed: List[Dict[str, Any]] = []
    now = time.time()

    if pol["auto_refresh_brief"]:
        narrative_state = mission_status.get("narrative_state") or {}
        stored_fp = (mission_status.get("mission_brief") or {}).get("source_fingerprint")
        current_fp = mission_narrative.brief_source_fingerprint(mission_status)
        if narrative_state.get("stale") or not stored_fp or stored_fp != current_fp:
            brief_result = store_mod.generate_mission_brief(
                project=mission_project,
                deliverable_id=deliverable_id,
                actor=actor,
                persist=True,
            )
            executed.append({
                "kind": "generate_mission_brief",
                "deliverable_id": deliverable_id,
                "ok": not brief_result.get("error"),
                "source_fingerprint": (brief_result.get("mission_brief") or {}).get(
                    "source_fingerprint"),
            })
            if not brief_result.get("error"):
                mission_status = brief_result.get("mission_status") or store_mod.get_mission_status(
                    project=mission_project, deliverable_id=deliverable_id)
                mission_status = _scope_mission_status(mission_status, pol)
                plan = coordinator_tick_plan(mission_status, policy=pol)

    result: Dict[str, Any] = {
        "schema": "switchboard.mission_coordinator_tick.v1",
        "project_id": mission_project,
        "deliverable_id": deliverable_id,
        "coordinator_agent_id": (coordinator_agent_id or actor or "").strip() or None,
        "plan": plan,
        "executed": executed,
        "escalations": [],
        "monitors": [],
        "dispatch": None,
        "retry_after_seconds": plan.get("retry_after_seconds", 120),
        "decision": None,
        "human_notifications": [],
    }

    status = plan.get("status")
    if status == "mission_complete":
        result["status"] = "mission_complete"
        result["retry_after_seconds"] = 3600
    elif status == "human_required":
        result["status"] = "human_required"
        result["escalations"] = plan.get("escalations") or []
    elif status == "monitor":
        result["status"] = "monitor"
        result["monitors"] = plan.get("monitors") or []
    elif status == "idle":
        result["status"] = "idle"
    elif status != "dispatch_ready":
        result["status"] = status
    else:
        dispatch = plan.get("dispatch") or {}
        if pol["auto_start"]:
            task_id = str(dispatch.get("task_id") or "").upper()
            task_project = str(dispatch.get("project_id") or mission_project)
            role = _lifecycle_role(store_mod, task_project, task_id)
            if task_starter is None:
                from switchboard.application.commands import task_execution
                task_starter = task_execution.start_task
            access = getattr(store_mod, "project_access", lambda *_a, **_k: {})
            principal_id = str((access(task_project) or {}).get("owner_user_id") or "")
            try:
                ensured = task_starter(
                    task_id, project=task_project, actor=actor,
                    principal_id=principal_id, role=role)
            except Exception as exc:  # fail visibly; the next tick may retry
                ensured = {"action": "refused", "error": type(exc).__name__,
                           "reason": str(exc), "task_id": task_id,
                           "project": task_project, "role": role}
            ensured = {**ensured, "task_project": task_project,
                       "role": ensured.get("role") or role}
            executed.append({
                "kind": "start_task",
                "deliverable_id": deliverable_id,
                "task_id": task_id,
                "task_project": task_project,
                "role": role,
                "action": ensured.get("action"),
                "wake_id": ensured.get("wake_id"),
                "execution_id": ensured.get("execution_id"),
                "reason": ensured.get("reason") or ensured.get("error"),
            })
            result["dispatch"] = ensured
            refused = ensured.get("action") == "refused" or bool(ensured.get("error"))
            transitioning = ensured.get("action") == "transitioning"
            if role == "remediation" and not refused and not transitioning:
                marker = getattr(store_mod, "mark_review_remediation_ensured", None)
                if callable(marker):
                    marker(task_id, wake_id=str(ensured.get("wake_id") or ""),
                           actor=actor, project=task_project)
            result["status"] = (
                "dispatch_blocked" if refused else
                "session_transitioning" if transitioning else
                "session_ensured"
            )
            result["retry_after_seconds"] = 120 if refused else 15 if transitioning else 30
        else:
            result["status"] = "dispatch_ready"
            result["dispatch"] = dispatch
            result["retry_after_seconds"] = 60

    # COORD-6: deliver exception-only human notifications (never for agent-lane progress).
    try:
        import coordinator_escalation
        coord_actor = (coordinator_agent_id or actor or "switchboard/coordinator").strip()
        if result.get("status") == "human_required":
            result["human_notifications"] = coordinator_escalation.deliver_mission_escalations(
                result.get("escalations") or [],
                store_mod=store_mod,
                project=mission_project,
                deliverable_id=deliverable_id,
                actor=coord_actor,
                now=now,
            )
        elif result.get("status") == "dispatch_blocked":
            blocked_plan = coordinator_escalation.classify_dispatch_blocked(
                result.get("dispatch") or {},
                project=mission_project,
                deliverable_id=deliverable_id,
                task_id=str((plan.get("dispatch") or {}).get("task_id") or ""),
            )
            if blocked_plan:
                result["human_notifications"] = [
                    coordinator_escalation.deliver_human_escalation(
                        blocked_plan,
                        store_mod=store_mod,
                        actor=coord_actor,
                        now=now,
                    )
                ]
                result["escalations"] = [{
                    "action": "dispatch_blocked",
                    "task_id": blocked_plan.get("task_id"),
                    "reason": blocked_plan.get("failed_condition"),
                    "escalation_class": blocked_plan.get("escalation_class"),
                    "attention": True,
                }]
    except Exception as exc:  # noqa: BLE001 — escalation delivery must not fail the tick
        result["human_notifications"] = [{
            "ok": False,
            "delivered": False,
            "error": "escalation_delivery_failed",
            "message": str(exc),
        }]

    decision = _record_tick_decision(
        store_mod,
        mission_project=mission_project,
        mission_status=mission_status,
        plan=plan,
        result=result,
        policy=pol,
        coordinator_agent_id=(coordinator_agent_id or actor or "").strip(),
        actor=actor,
        idem_key=idem_key,
    )
    result["decision"] = decision
    if isinstance(decision, dict) and decision.get("decision_id"):
        result["decision_id"] = decision.get("decision_id")
    return result
