"""Mission coordinator tick — deliverable-scoped dispatch loop (DELIVERABLES-7).

COORD-3: every tick persists an explainable coordinator decision
(``switchboard.coordinator_decision.v1``) so operators can see why an action was
chosen or skipped without reading chat transcripts.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

ACTION_PRIORITY: Dict[str, int] = {
    "request_human_approval": 0,
    "approve_breakdown": 1,
    "repair_task_link": 2,
    "verify_merge_provenance": 3,
    "claim_task": 4,
    "resume_or_claim": 5,
    "propose_breakdown": 6,
}

HUMAN_ESCALATION = frozenset({
    "request_human_approval",
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
    "claimed": "coord.tick.dispatch_claim",
    "dispatch_blocked": "coord.tick.dispatch_blocked",
    "wake_requested": "coord.tick.dispatch_wake",
}


def _normalize_policy(policy: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = {
        "auto_refresh_brief": True,
        "auto_claim": True,
        "auto_wake": False,
        "monitor_in_review": True,
        "worker_agent_id": "",
        "worker_wake_selector": {},
    }
    if isinstance(policy, dict):
        base.update({k: v for k, v in policy.items() if k in base})
    base["worker_agent_id"] = (base.get("worker_agent_id") or "").strip()
    selector = base.get("worker_wake_selector")
    base["worker_wake_selector"] = selector if isinstance(selector, dict) else {}
    return base


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
    progress = mission_status.get("progress") or {}
    linked = int(progress.get("linked_task_count") or 0)
    done_ratio = float(progress.get("done_with_proof_ratio") or 0.0)
    if linked > 0 and done_ratio >= 1.0:
        return {
            "status": "mission_complete",
            "deliverable_id": mission_status.get("deliverable_id"),
            "reason": "All linked tasks have terminal Done provenance",
        }
    selected = pick_coordinator_action(mission_status.get("next_actions") or [])
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
        plan["auto_claim"] = bool(pol["auto_claim"])
        plan["auto_wake"] = bool(pol["auto_wake"])
        plan["worker_agent_id"] = pol["worker_agent_id"]
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
            "auto_claim": bool(policy.get("auto_claim")),
            "auto_wake": bool(policy.get("auto_wake")),
            "auto_refresh_brief": bool(policy.get("auto_refresh_brief")),
            "monitor_in_review": bool(policy.get("monitor_in_review")),
            "worker_agent_id": policy.get("worker_agent_id") or "",
            "worker_wake_selector": policy.get("worker_wake_selector") or {},
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
        "claimed": "dispatch",
        "wake_requested": "nudge",
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
    idem_key: str = "",
) -> Dict[str, Any]:
    """Execute one deliverable-scoped coordinator tick with auditing."""
    import mission_narrative
    if store_mod is None:
        import store as store_mod

    pol = _normalize_policy(policy)
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
        worker = pol["worker_agent_id"] or (coordinator_agent_id or actor or "").strip()
        if pol["auto_claim"] and worker:
            claim = store_mod.claim_next(
                agent_id=worker,
                project=mission_project,
                deliverable_id=deliverable_id,
                actor=actor,
                idem_key=f"coord-{deliverable_id}-{dispatch.get('task_id')}-{int(now // 60)}",
            )
            executed.append({
                "kind": "claim_next",
                "deliverable_id": deliverable_id,
                "worker_agent_id": worker,
                "claimed": bool(claim.get("claimed")),
                "claim_id": claim.get("claim_id"),
                "task_id": (claim.get("task") or {}).get("task_id"),
                "task_project": claim.get("task_project"),
                "reason": claim.get("reason"),
            })
            result["dispatch"] = claim
            result["status"] = "claimed" if claim.get("claimed") else "dispatch_blocked"
            if not claim.get("claimed"):
                result["retry_after_seconds"] = int(claim.get("retry_after_seconds") or 120)
        elif pol["auto_wake"] and pol["worker_wake_selector"]:
            selector = dict(pol["worker_wake_selector"])
            selector.setdefault("deliverable_id", deliverable_id)
            selector.setdefault("task_id", dispatch.get("task_id"))
            selector.setdefault("project_id", dispatch.get("project_id"))
            wake = store_mod.request_wake(
                selector,
                reason=f"Mission coordinator dispatch for {deliverable_id}",
                source=actor,
                task_id=dispatch.get("task_id") or "",
                actor=actor,
                project=mission_project,
                idem_key=f"coord-wake-{deliverable_id}-{dispatch.get('task_id')}",
            )
            executed.append({
                "kind": "request_wake",
                "deliverable_id": deliverable_id,
                "requested": bool(wake.get("requested", wake.get("wake_id"))),
                "wake_id": wake.get("wake_id"),
                "reason": wake.get("reason"),
            })
            result["dispatch"] = wake
            result["status"] = "wake_requested" if wake.get("wake_id") else "dispatch_blocked"
        else:
            result["status"] = "dispatch_ready"
            result["dispatch"] = dispatch
            result["retry_after_seconds"] = 60

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
