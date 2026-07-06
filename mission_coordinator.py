"""Mission coordinator tick — deliverable-scoped dispatch loop (DELIVERABLES-7)."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

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


def run_coordinator_tick(
    mission_status: Dict[str, Any],
    *,
    mission_project: str,
    coordinator_agent_id: str = "",
    actor: str = "system",
    policy: Optional[Dict[str, Any]] = None,
    store_mod: Any = None,
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
    }

    status = plan.get("status")
    if status == "mission_complete":
        result["status"] = "mission_complete"
        result["retry_after_seconds"] = 3600
        return result
    if status == "human_required":
        result["status"] = "human_required"
        result["escalations"] = plan.get("escalations") or []
        return result
    if status == "monitor":
        result["status"] = "monitor"
        result["monitors"] = plan.get("monitors") or []
        return result
    if status == "idle":
        result["status"] = "idle"
        return result
    if status != "dispatch_ready":
        result["status"] = status
        return result

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
        return result

    if pol["auto_wake"] and pol["worker_wake_selector"]:
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
        return result

    result["status"] = "dispatch_ready"
    result["dispatch"] = dispatch
    result["retry_after_seconds"] = 60
    return result
