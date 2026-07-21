"""T1 coordinator dispatch mode (COORD-4).

Consumes COORD-2 audit recommendations and takes bounded T1 actions:
wake eligible hosts, send directed claim-request messages, and nudge stale
but still-live agent sessions. Every candidate produces a COORD-3 decision
record. Default is dry-run (observe/log only). The dispatcher never claims
work for itself and never writes task content or Done.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional

import coordinator_audit as audit_mod

TICK_SCHEMA = "switchboard.coordinator_dispatch_tick.v1"
PLAN_SCHEMA = "switchboard.coordinator_dispatch_plan.v1"
TIER = "T1"
ACTIVITY_KIND = "coordinator.dispatch.tick"

DEFAULT_POLICY: Dict[str, Any] = {
    "dry_run": True,
    "max_dispatches_per_tick": 3,
    "max_nudges_per_tick": 3,
    "allowed_lanes": [],  # empty = all lanes
    "allowed_runtimes": ["claude-code", "codex"],
    "default_runtime": "claude-code",
    "nudge_stale_after_seconds": 600,
    "nudge_requires_ack": False,
    "coordinator_agent_id": "switchboard/coordinator-t1",
    "worker_agent_id": "",  # unused at T1 — never self-claim
    "allow_self_claim": False,
    "send_claim_request_message": True,
    "post_task_comment": True,
    # BUG-91: only place Watch-requiring work on hosts that can actually show
    # the operator the run. Ships OFF and is enabled with
    # PM_COORD_REQUIRE_RUNNER_WATCH=1 -- hosts must first roll out the build that
    # advertises `runner_watch`, or enabling this would starve dispatch fleet-wide
    # (every host registered before that build advertises no such capability).
    # Rollout order: deploy agent hosts -> confirm the capability is advertised
    # -> flip this on.
    "require_runner_watch": False,
}


def _normalize_policy(policy: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    out = dict(DEFAULT_POLICY)
    if isinstance(policy, dict):
        for key, value in policy.items():
            if key in out:
                out[key] = value
    out["dry_run"] = bool(out["dry_run"])
    out["max_dispatches_per_tick"] = max(0, int(out["max_dispatches_per_tick"] or 0))
    out["max_nudges_per_tick"] = max(0, int(out["max_nudges_per_tick"] or 0))
    lanes = out.get("allowed_lanes") or []
    out["allowed_lanes"] = [str(x).strip().upper() for x in lanes if str(x).strip()]
    runtimes = out.get("allowed_runtimes") or []
    out["allowed_runtimes"] = [str(x).strip().lower() for x in runtimes if str(x).strip()]
    out["default_runtime"] = str(out.get("default_runtime") or "claude-code").strip().lower()
    out["coordinator_agent_id"] = str(out.get("coordinator_agent_id") or "").strip()
    out["worker_agent_id"] = str(out.get("worker_agent_id") or "").strip()
    out["allow_self_claim"] = bool(out.get("allow_self_claim"))
    out["nudge_stale_after_seconds"] = max(60, int(out["nudge_stale_after_seconds"] or 600))
    out["require_runner_watch"] = enabled_from_env(
        "PM_COORD_REQUIRE_RUNNER_WATCH", bool(out.get("require_runner_watch", False)))
    return out


def enabled_from_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _lane(task: Mapping[str, Any]) -> str:
    return str(task.get("_wsId") or task.get("workstream") or task.get("workstream_id") or "").strip().upper()


def _task_by_id(snapshot: Mapping[str, Any], task_id: str) -> Dict[str, Any]:
    wanted = str(task_id or "").strip().upper()
    for row in snapshot.get("tasks") or []:
        if str(row.get("task_id") or "").strip().upper() == wanted:
            return dict(row)
    return {}


RUNNER_WATCH_CAPABILITY = "runner_watch"


def host_serves_runner_watch(host: Mapping[str, Any]) -> bool:
    """True when this host advertises that it can serve browser Watch/Chat.

    BUG-91: CO-fleet workers accepted Watch-requiring work and then published
    runner rows with no PTY, no stream binding and runner_open/runner_inject
    false -- every one of the 84 measured AWS rows. The operator clicked the task
    and got a refusal for a session that could never have been watchable. A host
    that cannot show the run must not be handed work that promises it.
    """
    for entry in host.get("runtimes") or []:
        if not isinstance(entry, dict):
            continue
        capabilities = {str(item).strip().lower()
                        for item in (entry.get("capabilities") or [])}
        if RUNNER_WATCH_CAPABILITY in capabilities:
            return True
    return False


def _active_hosts(snapshot: Mapping[str, Any], *, lane: str = "",
                  runtime: str = "", require_runner_watch: bool = False
                  ) -> List[Dict[str, Any]]:
    now = float(snapshot.get("observed_at") or time.time())
    hosts = []
    for host in snapshot.get("hosts") or []:
        if host.get("stale"):
            continue
        heartbeat = float(host.get("heartbeat_at") or 0)
        ttl = float(host.get("heartbeat_ttl_s") or host.get("ttl_s") or 120)
        if heartbeat and heartbeat + ttl < now:
            continue
        if lane:
            host_lanes = set()
            for rt in host.get("runtimes") or []:
                if isinstance(rt, dict):
                    for item in (rt.get("lanes") or rt.get("allowed_lanes") or []):
                        host_lanes.add(str(item).strip().upper())
                    policy = rt.get("policy") or {}
                    for item in policy.get("allowed_lanes") or []:
                        host_lanes.add(str(item).strip().upper())
            # empty lanes advertisement = accept all lanes
            if host_lanes and lane not in host_lanes:
                continue
        if runtime:
            runtimes = []
            for rt in host.get("runtimes") or []:
                if isinstance(rt, dict) and rt.get("runtime"):
                    runtimes.append(str(rt.get("runtime")).strip().lower())
            if runtimes and runtime not in runtimes:
                continue
        if require_runner_watch and not host_serves_runner_watch(host):
            continue
        hosts.append(dict(host))
    return hosts


def _live_agents(snapshot: Mapping[str, Any], *, lane: str = "",
                 now: float | None = None) -> List[Dict[str, Any]]:
    observed = float(now if now is not None else snapshot.get("observed_at") or time.time())
    agents = []
    for agent in snapshot.get("agents") or []:
        agent_id = str(agent.get("agent_id") or "").strip()
        if not agent_id:
            continue
        heartbeat = float(agent.get("heartbeat_at") or 0)
        ttl = float(agent.get("ttl_s") or 120)
        if not heartbeat or heartbeat + ttl < observed:
            continue
        agent_lane = str(agent.get("lane") or "").strip().upper()
        if lane and agent_lane and agent_lane != lane:
            continue
        agents.append(dict(agent))
    return agents


def _stale_live_agents(snapshot: Mapping[str, Any], *, stale_after: int,
                       now: float | None = None) -> List[Dict[str, Any]]:
    observed = float(now if now is not None else snapshot.get("observed_at") or time.time())
    out = []
    for agent in _live_agents(snapshot, now=observed):
        heartbeat = float(agent.get("heartbeat_at") or 0)
        age = observed - heartbeat
        if age >= stale_after:
            row = dict(agent)
            row["stale_age_seconds"] = age
            out.append(row)
    return out


def build_dispatch_plan(snapshot: Mapping[str, Any], *,
                        policy: Optional[Mapping[str, Any]] = None,
                        audit_plan: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Pure plan: which T1 wake/nudge/message actions the dispatcher would take."""
    pol = _normalize_policy(policy)
    project = str(snapshot.get("project") or "")
    if audit_plan is None:
        audit_plan = audit_mod.build_plan(snapshot)
    recommendations = list(audit_plan.get("recommendations") or [])
    assignment_recs = [
        row for row in recommendations
        if row.get("category") == "assignment" and row.get("action") == "consider_assignment"
    ]
    candidates: List[Dict[str, Any]] = []
    for row in assignment_recs:
        task_id = str(row.get("target_id") or "").upper()
        task = _task_by_id(snapshot, task_id)
        lane = _lane(task) or str((row.get("inputs") or {}).get("lane") or "").upper()
        if pol["allowed_lanes"] and lane and lane not in pol["allowed_lanes"]:
            candidates.append({
                "kind": "skip",
                "action": "skip_lane_not_allowed",
                "task_id": task_id,
                "lane": lane,
                "reason": "lane_not_in_allowlist",
                "policy_rule": "coord.dispatch.lane_allowlist",
                "audit_recommendation_id": row.get("recommendation_id"),
            })
            continue
        runtime = pol["default_runtime"]
        if pol["allowed_runtimes"] and runtime not in pol["allowed_runtimes"]:
            runtime = pol["allowed_runtimes"][0]
        hosts = _active_hosts(snapshot, lane=lane, runtime=runtime,
                              require_runner_watch=pol["require_runner_watch"])
        # BUG-91: say so out loud when watch capability is what excluded a host,
        # rather than reporting an indistinguishable "no eligible host".
        watch_excluded = []
        if pol["require_runner_watch"] and not hosts:
            watch_excluded = [
                str(host.get("host_id") or "")
                for host in _active_hosts(snapshot, lane=lane, runtime=runtime)
                if not host_serves_runner_watch(host)
            ]
        agents = _live_agents(snapshot, lane=lane)
        if not hosts and not agents:
            candidates.append({
                "kind": "escalation",
                "action": "escalate_no_host",
                "task_id": task_id,
                "lane": lane,
                "runtime": runtime,
                **({"excluded_not_watch_capable": watch_excluded}
                   if watch_excluded else {}),
                "reason": ("no_watch_capable_host" if watch_excluded
                           else "no_eligible_host_or_agent"),
                "policy_rule": "coord.dispatch.no_host",
                "escalation_class": "no_host",
                "audit_recommendation_id": row.get("recommendation_id"),
            })
            continue
        target_agent = ""
        if agents:
            # Prefer an idle lane agent (no task_id) for a claim-request nudge.
            idle = [a for a in agents if not str(a.get("task_id") or "").strip()]
            pick = idle[0] if idle else agents[0]
            target_agent = str(pick.get("agent_id") or "")
        candidates.append({
            "kind": "dispatch",
            "action": "wake_and_request_claim",
            "task_id": task_id,
            "lane": lane,
            "runtime": runtime,
            "eligible_host_count": len(hosts),
            "live_agent_count": len(agents),
            "target_agent_id": target_agent,
            "reason": "ready_unclaimed_task",
            "policy_rule": "coord.dispatch.wake_ready_task",
            "audit_recommendation_id": row.get("recommendation_id"),
            "title": task.get("title") or task_id,
        })

    nudge_candidates: List[Dict[str, Any]] = []
    for agent in _stale_live_agents(snapshot, stale_after=pol["nudge_stale_after_seconds"]):
        agent_id = str(agent.get("agent_id") or "")
        if agent_id == pol["coordinator_agent_id"]:
            continue
        nudge_candidates.append({
            "kind": "nudge",
            "action": "nudge_stale_session",
            "task_id": str(agent.get("task_id") or ""),
            "lane": str(agent.get("lane") or "").upper(),
            "target_agent_id": agent_id,
            "stale_age_seconds": agent.get("stale_age_seconds"),
            "reason": "live_session_heartbeat_stale",
            "policy_rule": "coord.dispatch.nudge_stale_session",
        })

    dispatch_limit = pol["max_dispatches_per_tick"]
    nudge_limit = pol["max_nudges_per_tick"]
    selected_dispatch = [c for c in candidates if c["kind"] == "dispatch"][:dispatch_limit]
    selected_nudge = nudge_candidates[:nudge_limit]
    skipped = (
        [c for c in candidates if c["kind"] != "dispatch"]
        + [c for c in candidates if c["kind"] == "dispatch"][dispatch_limit:]
        + nudge_candidates[nudge_limit:]
    )

    return {
        "schema": PLAN_SCHEMA,
        "project": project,
        "tier": TIER,
        "dry_run": pol["dry_run"],
        "policy": {
            "max_dispatches_per_tick": dispatch_limit,
            "max_nudges_per_tick": nudge_limit,
            "allowed_lanes": pol["allowed_lanes"],
            "allowed_runtimes": pol["allowed_runtimes"],
            "default_runtime": pol["default_runtime"],
            "nudge_stale_after_seconds": pol["nudge_stale_after_seconds"],
            "allow_self_claim": pol["allow_self_claim"],
        },
        "audit_plan_id": audit_plan.get("plan_id"),
        "selected": selected_dispatch + selected_nudge,
        "skipped": skipped,
        "summary": {
            "dispatch_selected": len(selected_dispatch),
            "nudge_selected": len(selected_nudge),
            "skipped": len(skipped),
            "escalations": sum(1 for c in skipped if c.get("kind") == "escalation"),
        },
    }


def _record_decision(store_mod: Any, *, project: str, coordinator_agent_id: str,
                     title: str, policy_rule: str, chosen_action: Dict[str, Any],
                     skipped_alternatives: List[Dict[str, Any]], result: Dict[str, Any],
                     inputs_snapshot: Dict[str, Any], decision_kind: str,
                     task_id: str = "", stable_key: str = "",
                     dry_run: bool = True) -> Dict[str, Any]:
    return store_mod.record_coordinator_decision(
        author=coordinator_agent_id,
        title=title,
        inputs_snapshot=inputs_snapshot,
        policy_rule=policy_rule,
        chosen_action=chosen_action,
        skipped_alternatives=skipped_alternatives,
        result=result,
        project=project,
        task_id=task_id,
        coordinator_agent_id=coordinator_agent_id,
        decision_kind=decision_kind,
        stable_key=stable_key,
        context=("dry-run " if dry_run else "") + f"T1 dispatch on {project}",
        rationale=f"Applied {policy_rule}",
    )


def _execute_dispatch(store_mod: Any, *, project: str, candidate: Mapping[str, Any],
                      policy: Mapping[str, Any], dry_run: bool,
                      actor: str) -> Dict[str, Any]:
    task_id = str(candidate.get("task_id") or "")
    runtime = str(candidate.get("runtime") or policy["default_runtime"])
    lane = str(candidate.get("lane") or "")
    coordinator = policy["coordinator_agent_id"]
    if dry_run:
        return {
            "status": "dry_run",
            "would": ["request_wake", "send_claim_request_message", "post_task_comment"],
            "task_id": task_id,
            "runtime": runtime,
            "lane": lane,
            "target_agent_id": candidate.get("target_agent_id"),
            "eligible_host_count": candidate.get("eligible_host_count"),
        }

    # Never claim — T1 routes intent only.
    if policy.get("allow_self_claim"):
        # Explicitly refuse even when misconfigured; COORD-4 keeps claim out of band.
        pass

    from switchboard.application.commands import task_execution
    wake = task_execution.execute_mapping_result(
        "start_task", task_id, actor=actor, project=project, runtime=runtime)
    dispatched = bool(wake.get("started") or wake.get("starting")
                      or wake.get("attached"))
    result: Dict[str, Any] = {
        "status": "wake_requested" if dispatched else "dispatch_blocked",
        "wake": wake,
        "task_id": task_id,
        "runtime": runtime,
        "lane": lane,
    }
    if not dispatched:
        result["failure_class"] = "no_host" if not wake.get("work_hosts_online") else "failed_gate"
        return result

    target = str(candidate.get("target_agent_id") or "").strip()
    if policy.get("send_claim_request_message") and target:
        msg = store_mod.send_agent_message(
            from_agent=coordinator,
            to_agent=target,
            message=(
                f"Coordinator T1 dispatch: please claim {task_id} "
                f"({candidate.get('title') or task_id}) on project={project}, lane={lane or '—'}."
            ),
            task_id=task_id or None,
            requires_ack=False,
            signal="coord_dispatch_claim_request",
            priority=50,
            idem_key=f"coord4-msg:{project}:{task_id}:{target}",
            project=project,
        )
        result["claim_request_message"] = {
            "id": msg.get("id"),
            "to_agent": target,
            "delivery_status": msg.get("delivery_status"),
            "error": msg.get("error"),
        }
    return result


def _execute_nudge(store_mod: Any, *, project: str, candidate: Mapping[str, Any],
                   policy: Mapping[str, Any], dry_run: bool) -> Dict[str, Any]:
    target = str(candidate.get("target_agent_id") or "").strip()
    task_id = str(candidate.get("task_id") or "")
    coordinator = policy["coordinator_agent_id"]
    if dry_run:
        return {
            "status": "dry_run",
            "would": ["send_agent_message"],
            "target_agent_id": target,
            "task_id": task_id,
            "stale_age_seconds": candidate.get("stale_age_seconds"),
        }
    if not target:
        return {"status": "skipped", "reason": "missing_target_agent"}
    msg = store_mod.send_agent_message(
        from_agent=coordinator,
        to_agent=target,
        message=(
            f"Coordinator T1 nudge: your session looks stale "
            f"(~{int(candidate.get('stale_age_seconds') or 0)}s since heartbeat). "
            f"Please heartbeat or complete/abandon your claim"
            + (f" on {task_id}." if task_id else ".")
        ),
        task_id=task_id or None,
        requires_ack=bool(policy.get("nudge_requires_ack")),
        signal="coord_dispatch_nudge",
        priority=40,
        idem_key=f"coord4-nudge:{project}:{target}:{task_id or 'none'}",
        project=project,
    )
    return {
        "status": "nudged" if msg.get("id") and not msg.get("error") else "nudge_failed",
        "message_id": msg.get("id"),
        "delivery_status": msg.get("delivery_status"),
        "target_agent_id": target,
        "error": msg.get("error"),
    }


def run_dispatch_tick(
    project: str,
    *,
    policy: Optional[Mapping[str, Any]] = None,
    actor: str = "",
    store_mod: Any = None,
    audit_module: Any = None,
    now: float | None = None,
    idem_key: str = "",
) -> Dict[str, Any]:
    """Run one project-scoped T1 dispatch tick (dry-run by default)."""
    if store_mod is None:
        import store as store_mod
    if audit_module is None:
        audit_module = audit_mod

    pol = _normalize_policy(policy)
    coordinator = pol["coordinator_agent_id"]
    actor = (actor or coordinator).strip()
    observed = float(now if now is not None else time.time())
    dry_run = bool(pol["dry_run"])

    db_path = str(store_mod._resolve(project)["db"])
    snapshot = audit_module.collect_snapshot(db_path, project, now=observed)
    audit_plan = audit_module.build_plan(snapshot)
    plan = build_dispatch_plan(snapshot, policy=pol, audit_plan=audit_plan)

    executed: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    escalations: List[Dict[str, Any]] = []

    # Record skips/escalations first so the trail shows refused work.
    for skipped in plan.get("skipped") or []:
        kind = skipped.get("kind")
        decision_kind = {
            "escalation": "skip",
            "skip": "skip",
            "nudge": "nudge",
            "dispatch": "skip",
        }.get(kind, "skip")
        if kind == "escalation":
            escalations.append(dict(skipped))
            decision_kind = "skip"
        chosen = {
            "action": skipped.get("action"),
            "status": "skipped" if kind != "escalation" else "escalated",
            "task_id": skipped.get("task_id"),
            "escalation_class": skipped.get("escalation_class"),
        }
        decision = _record_decision(
            store_mod,
            project=project,
            coordinator_agent_id=coordinator,
            title=f"Coordinator T1: {skipped.get('action')}",
            policy_rule=str(skipped.get("policy_rule") or "coord.dispatch.skip"),
            chosen_action=chosen,
            skipped_alternatives=[{"action": "wake_and_request_claim", "reason": skipped.get("reason")}],
            result={"status": chosen["status"], "dry_run": dry_run},
            inputs_snapshot={
                "project": project,
                "candidate": skipped,
                "audit_plan_id": plan.get("audit_plan_id"),
                "policy": plan.get("policy"),
            },
            decision_kind=decision_kind,
            task_id=str(skipped.get("task_id") or ""),
            stable_key=(idem_key or f"coord4:{project}:{skipped.get('action')}:"
                        f"{skipped.get('task_id') or skipped.get('target_agent_id')}:{int(observed // 60)}"),
            dry_run=dry_run,
        )
        decisions.append(decision)

    for candidate in plan.get("selected") or []:
        kind = candidate.get("kind")
        if kind == "dispatch":
            result = _execute_dispatch(
                store_mod, project=project, candidate=candidate, policy=pol,
                dry_run=dry_run, actor=actor)
            decision_kind = "recommendation" if dry_run else "dispatch"
            if result.get("status") == "dispatch_blocked":
                decision_kind = "skip"
                escalations.append({
                    "action": "escalate_no_host",
                    "task_id": candidate.get("task_id"),
                    "escalation_class": result.get("failure_class") or "no_host",
                    "result": result,
                })
        elif kind == "nudge":
            result = _execute_nudge(
                store_mod, project=project, candidate=candidate, policy=pol,
                dry_run=dry_run)
            decision_kind = "recommendation" if dry_run else "nudge"
        else:
            continue

        executed.append({"kind": kind, "candidate": candidate, "result": result})
        skipped_alts = [
            {"action": other.get("action"), "task_id": other.get("task_id"),
             "reason": "capacity_or_priority"}
            for other in (plan.get("skipped") or [])
            if other.get("kind") in {"dispatch", "nudge"}
        ][:10]
        decision = _record_decision(
            store_mod,
            project=project,
            coordinator_agent_id=coordinator,
            title=f"Coordinator T1: {candidate.get('action')}",
            policy_rule=str(candidate.get("policy_rule") or "coord.dispatch.action"),
            chosen_action={
                "action": candidate.get("action"),
                "status": result.get("status"),
                "task_id": candidate.get("task_id"),
                "target_agent_id": candidate.get("target_agent_id"),
                "runtime": candidate.get("runtime"),
                "lane": candidate.get("lane"),
            },
            skipped_alternatives=skipped_alts,
            result=result,
            inputs_snapshot={
                "project": project,
                "candidate": candidate,
                "audit_plan_id": plan.get("audit_plan_id"),
                "policy": plan.get("policy"),
                "eligible_host_count": candidate.get("eligible_host_count"),
            },
            decision_kind=decision_kind,
            task_id=str(candidate.get("task_id") or ""),
            stable_key=(idem_key or f"coord4:{project}:{candidate.get('action')}:"
                        f"{candidate.get('task_id') or candidate.get('target_agent_id')}:"
                        f"{int(observed // 60)}"),
            dry_run=dry_run,
        )
        decisions.append(decision)

    status = "dry_run" if dry_run else "executed"
    if escalations and not any(e.get("result", {}).get("status") == "wake_requested"
                               for e in executed):
        if any(e.get("escalation_class") == "no_host" for e in escalations):
            status = "escalated_no_host" if not dry_run else "dry_run"
    receipt = {
        "schema": TICK_SCHEMA,
        "project": project,
        "tier": TIER,
        "status": status,
        "dry_run": dry_run,
        "coordinator_agent_id": coordinator,
        "actor": actor,
        "observed_at": observed,
        "plan": plan,
        "executed": executed,
        "escalations": escalations,
        "decisions": [
            {"decision_id": d.get("decision_id"), "id": d.get("id"),
             "decision_kind": d.get("decision_kind"), "created": d.get("created"),
             "idempotent": d.get("idempotent"), "error": d.get("error")}
            for d in decisions if isinstance(d, dict)
        ],
        "retry_after_seconds": 120 if dry_run else 60,
        "effects": {
            "claims": [],  # T1 never claims
            "wakes": [e["result"].get("wake") for e in executed
                      if e.get("kind") == "dispatch" and isinstance(e.get("result"), dict)],
            "messages": [e["result"] for e in executed
                         if e.get("kind") == "nudge"],
        },
    }

    try:
        store_mod.append_activity(
            ACTIVITY_KIND, actor,
            {"schema": TICK_SCHEMA, "project": project, "tier": TIER,
             "dry_run": dry_run, "status": status,
             "summary": plan.get("summary"),
             "decision_ids": [d.get("decision_id") for d in receipt["decisions"]
                             if d.get("decision_id")],
             "executed_count": len(executed),
             "escalation_count": len(escalations)},
            project=project,
        )
    except Exception as exc:  # noqa: BLE001 — receipt should still return
        receipt["activity_error"] = {"error_type": type(exc).__name__, "message": str(exc)}
    return receipt


def dispatch_projects(projects: Iterable[str], *, policy: Optional[Mapping[str, Any]] = None,
                      actor: str = "", store_mod: Any = None) -> Dict[str, Any]:
    """Run a T1 tick across selected projects."""
    receipts = []
    for raw in projects:
        project = str(raw or "").strip()
        if not project:
            continue
        receipts.append(run_dispatch_tick(
            project, policy=policy, actor=actor, store_mod=store_mod))
    return {
        "schema": "switchboard.coordinator_dispatch_run.v1",
        "tier": TIER,
        "projects": receipts,
        "ok": bool(receipts) and all(
            not r.get("activity_error") for r in receipts),
    }


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import store

    parser = argparse.ArgumentParser(description="COORD-4 T1 coordinator dispatch tick")
    parser.add_argument("--project", action="append", default=[],
                        help="Project id (repeatable). Default: PM_COORDINATOR_DISPATCH_PROJECTS or switchboard")
    parser.add_argument("--act", action="store_true",
                        help="Disable dry-run and perform wakes/messages")
    parser.add_argument("--max-dispatches", type=int, default=None)
    parser.add_argument("--max-nudges", type=int, default=None)
    args = parser.parse_args(argv)

    projects = args.project or [
        p.strip() for p in os.environ.get("PM_COORDINATOR_DISPATCH_PROJECTS", "switchboard").split(",")
        if p.strip()
    ]
    dry_run = not (args.act or enabled_from_env("PM_COORDINATOR_DISPATCH_ACT", False))
    policy = {
        "dry_run": dry_run,
        "coordinator_agent_id": (
            os.environ.get("PM_COORDINATOR_DISPATCH_ACTOR") or "switchboard/coordinator-t1"
        ).strip(),
    }
    if args.max_dispatches is not None:
        policy["max_dispatches_per_tick"] = args.max_dispatches
    if args.max_nudges is not None:
        policy["max_nudges_per_tick"] = args.max_nudges

    result = dispatch_projects(projects, policy=policy, store_mod=store)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
