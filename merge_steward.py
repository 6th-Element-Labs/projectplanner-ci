"""T3 policy-gated merge steward (COORD-7).

Optional autopilot for **eligible In Review** PRs. Default posture is
dry-run: plan + COORD-3 decisions + activity artifact. Acting requires both
``PM_COORDINATOR_MERGE_ACT=1`` and an enabled merge policy.

Hard floor (never bypassed):

* never set Done (webhook/reconcile only)
* never arm when ``merge_gate`` is blocked
* red/unknown checks, conflicts, stale branches, missing provenance,
  missing authority and mechanical merge failures fail closed to
  COORD-6 escalation
* successful arm may trigger reconcile so Done provenance can land later
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import coordinator_audit as audit
import coordinator_escalation as escalation
import merge_coordinator as mc

PLAN_SCHEMA = "switchboard.coordinator_merge_plan.v1"
RUN_SCHEMA = "switchboard.coordinator_merge_run.v1"
ACTIVITY_SCHEMA = "switchboard.coordinator_merge_activity.v1"
ACTIVITY_KIND = "coordinator.merge_steward.tick"
TIER = "T3"
DEFAULT_ACTOR = "switchboard/coordinator-t3"
DEFAULT_OPERATOR = "switchboard/operator"
DEFAULT_MAX_IN_FLIGHT = 3

ACTION_ARM = "arm_auto_merge"
ACTION_HOLD_PENDING = "hold_pending_ci"
ACTION_HOLD_DEPS = "hold_for_dependencies"
ACTION_HOLD_BACKPRESSURE = "hold_backpressure"
ACTION_HOLD_POLICY = "hold_policy_disabled"
ACTION_VERIFY_POST_MERGE = "verify_post_merge_provenance"
ACTION_ESCALATE = "escalate_human"
ACTION_NOOP = "noop"

POLICY = {
    ACTION_ARM: "coord.merge.arm_auto_merge",
    ACTION_HOLD_PENDING: "coord.merge.hold_pending_ci",
    ACTION_HOLD_DEPS: "coord.merge.hold_for_dependencies",
    ACTION_HOLD_BACKPRESSURE: "coord.merge.hold_backpressure",
    ACTION_HOLD_POLICY: "coord.merge.hold_policy_disabled",
    ACTION_VERIFY_POST_MERGE: "coord.merge.verify_post_merge_provenance",
    ACTION_ESCALATE: "coord.merge.escalate_blocked_gate",
    ACTION_NOOP: "coord.merge.noop",
}

def enabled_from_env(name: str, default: bool = True) -> bool:
    return audit.enabled_from_env(name, default)


def _status(value: Any) -> str:
    return audit._status(value)


def _ci_state(ci: Mapping[str, Any] | None) -> str:
    return audit._ci_state(ci)


def default_merge_policy() -> dict[str, Any]:
    return {
        "schema": "switchboard.coordinator_policy.v1",
        "enabled": False,
        "tier": TIER,
        "dry_run_default": True,
        "max_in_flight": DEFAULT_MAX_IN_FLIGHT,
        "require_merge_gate_pass": True,
        "deny_blocking_tasks": False,
        "post_merge_reconcile": True,
        "arm_mode": "github_auto_merge_squash",
        "authority_granted": False,
    }


def load_merge_policy(*, env: Mapping[str, str] | None = None,
                      meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Compose merge-steward policy from defaults, project meta, and env overrides."""
    policy = default_merge_policy()
    nested = {}
    if isinstance(meta, Mapping):
        nested = meta.get("merge_steward") if isinstance(meta.get("merge_steward"), Mapping) else meta
    if isinstance(nested, Mapping):
        for key in policy:
            if key in nested and nested[key] is not None:
                policy[key] = nested[key]

    environ = env if env is not None else os.environ
    if "PM_COORDINATOR_MERGE_ENABLED" in environ:
        policy["enabled"] = enabled_from_env("PM_COORDINATOR_MERGE_ENABLED", False)
    if "PM_COORDINATOR_MERGE_AUTHORITY" in environ:
        policy["authority_granted"] = enabled_from_env("PM_COORDINATOR_MERGE_AUTHORITY", False)
    try:
        if environ.get("PM_COORDINATOR_MERGE_MAX_IN_FLIGHT"):
            policy["max_in_flight"] = max(0, int(environ["PM_COORDINATOR_MERGE_MAX_IN_FLIGHT"]))
    except (TypeError, ValueError):
        pass
    policy["enabled"] = bool(policy.get("enabled"))
    policy["authority_granted"] = bool(policy.get("authority_granted"))
    policy["require_merge_gate_pass"] = bool(policy.get("require_merge_gate_pass", True))
    policy["post_merge_reconcile"] = bool(policy.get("post_merge_reconcile", True))
    return policy


classify_merge_gate_result = escalation.classify_merge_gate_result


def _open_dependencies(task: Mapping[str, Any],
                       by_task: Mapping[str, Mapping[str, Any]]) -> list[str]:
    open_deps: list[str] = []
    for dep in audit._depends_on(task.get("depends_on")):
        other = by_task.get(dep)
        if other is None:
            open_deps.append(dep)
            continue
        if _status(other.get("status")) not in audit.TERMINAL_STATUSES:
            open_deps.append(dep)
    return open_deps


def _unsafe_tasks(snapshot: Mapping[str, Any], observed_at: float) -> set[str]:
    unsafe: set[str] = set()
    for row in snapshot.get("work_sessions") or []:
        if _status(row.get("status")) not in audit.ACTIVE_SESSION_STATUSES:
            continue
        expires = float(row.get("expires_at") or 0)
        if expires and expires < observed_at:
            continue
        dirty = _status(row.get("dirty_status"))
        conflicts = int(row.get("conflict_marker_count") or 0)
        if dirty in {"dirty", "conflict"} or conflicts > 0:
            task_id = str(row.get("task_id") or "").upper()
            if task_id:
                unsafe.add(task_id)
    return unsafe


def _ci_runs_for_task(snapshot: Mapping[str, Any], task_id: str,
                      head_sha: str = "") -> list[dict[str, Any]]:
    rows = []
    for raw in snapshot.get("ci_runs") or []:
        row = dict(raw)
        if str(row.get("task_id") or "").upper() != task_id:
            continue
        if head_sha and str(row.get("source_sha") or "") not in ("", head_sha):
            continue
        rows.append(row)
    return rows


def _board_gate_from_snapshot(task: Mapping[str, Any], git_state: Mapping[str, Any],
                              latest_ci: Mapping[str, Any] | None,
                              *, mergeable: bool | None = True) -> dict[str, Any]:
    """Synthesize a merge_gate-shaped receipt from board-recorded evidence (planner)."""
    findings: list[dict[str, Any]] = []
    ci_state = _ci_state(latest_ci)
    if not git_state.get("pr_number"):
        findings.append({"code": "missing_pr", "failure_class": "missing_data",
                         "detail": "In Review task has no recorded PR number"})
    if ci_state == "red":
        findings.append({"code": "required_status_red", "failure_class": "failed_gate",
                         "detail": "Latest board-recorded CI is red"})
    elif ci_state in {"missing", "unknown"}:
        findings.append({"code": "required_status_unknown", "failure_class": "failed_gate",
                         "detail": f"Latest board-recorded CI is {ci_state}"})
    elif ci_state == "pending":
        findings.append({"code": "required_status_pending", "failure_class": "failed_gate",
                         "detail": "Latest board-recorded CI is still pending"})
    if mergeable is False:
        findings.append({"code": "pr_not_mergeable", "failure_class": "stale_branch",
                         "detail": "PR is not mergeable (conflicts or behind)"})
    if findings:
        return {"ok": False, "status": "blocked", "findings": findings,
                "task_id": task.get("task_id")}
    return {"ok": True, "status": "passed", "findings": [],
            "task_id": task.get("task_id"),
            "pr_number": git_state.get("pr_number"),
            "head_sha": git_state.get("head_sha")}


def plan_merge_actions(snapshot: Mapping[str, Any], *,
                       policy: Mapping[str, Any] | None = None,
                       saturated: bool = False,
                       in_flight: int = 0,
                       merge_gate_fn: Callable[..., Mapping[str, Any]] | None = None,
                       now: float | None = None) -> dict[str, Any]:
    """Pure T3 planner over a coordinator_audit snapshot."""
    observed_at = float(time.time() if now is None else now)
    project = str(snapshot.get("project") or "")
    pol = load_merge_policy(meta=policy)
    read_status = dict(snapshot.get("read_status") or {})
    actions: list[dict[str, Any]] = []
    arm_budget = max(0, int(pol.get("max_in_flight") or 0) - max(0, int(in_flight or 0)))

    def add(task_id: str, action: str, reason: str, inputs: dict[str, Any],
            *, escalation_class: str | None = None, score: int = 50,
            skipped: list[dict[str, Any]] | None = None,
            merges: bool = False) -> None:
        actions.append({
            "task_id": task_id,
            "action": action,
            "policy_rule": POLICY[action],
            "reason": reason,
            "score": score,
            "escalation_class": escalation_class,
            "inputs": inputs,
            "skipped_alternatives": skipped or [],
            "mutates": action in {ACTION_ARM, ACTION_ESCALATE, ACTION_VERIFY_POST_MERGE},
            "merges": bool(merges),
        })

    if not read_status.get("available"):
        add(
            "", ACTION_ESCALATE,
            "Merge steward cannot read the project database and must fail closed.",
            {"error_code": read_status.get("error_code"),
             "error_type": read_status.get("error_type")},
            escalation_class="failed_gate", score=100,
            skipped=[{"action": ACTION_ARM, "reason": "read_path_unavailable"}],
        )
        return {
            "schema": PLAN_SCHEMA,
            "project": project,
            "tier": TIER,
            "generated_at": observed_at,
            "dry_run_default": True,
            "policy": pol,
            "actions": actions,
            "summary": {"action_count": len(actions), "in_review_count": 0},
        }

    if not pol.get("authority_granted"):
        # Still plan fail-closed escalations for unsafe In Review items, but never arm.
        authority_missing = True
    else:
        authority_missing = False

    tasks = [dict(row) for row in snapshot.get("tasks") or []]
    by_task = {str(row.get("task_id") or "").upper(): row for row in tasks}
    git_by_task = {str(row.get("task_id") or "").upper(): dict(row)
                   for row in snapshot.get("git_states") or []}
    unsafe_tasks = _unsafe_tasks(snapshot, observed_at)
    in_review = [row for row in tasks if _status(row.get("status")) == "in review"]
    armed = 0

    for task in in_review:
        task_id = str(task.get("task_id") or "").upper()
        git_state = git_by_task.get(task_id) or {}
        pr_number = git_state.get("pr_number")
        head_sha = str(git_state.get("head_sha") or "")
        runs = _ci_runs_for_task(snapshot, task_id, head_sha=head_sha)
        latest = runs[0] if runs else None
        ci_state = _ci_state(latest)
        open_deps = _open_dependencies(task, by_task)
        risk = task.get("risk_level") or "Medium"
        base_inputs = {
            "task_id": task_id,
            "status": task.get("status"),
            "risk_level": risk,
            "pr_number": pr_number,
            "pr_url": git_state.get("pr_url"),
            "head_sha": head_sha or None,
            "ci_state": ci_state,
            "ci_run_id": (latest or {}).get("run_id"),
            "open_dependencies": open_deps,
            "unsafe_session": task_id in unsafe_tasks,
            "policy_enabled": bool(pol.get("enabled")),
            "authority_granted": bool(pol.get("authority_granted")),
            "is_blocking": bool(task.get("is_blocking")),
        }

        if pol.get("deny_blocking_tasks") and task.get("is_blocking"):
            add(task_id, ACTION_ESCALATE,
                "Blocking tasks are denied by merge steward policy.",
                base_inputs, escalation_class="policy_violation", score=94,
                skipped=[{"action": ACTION_ARM, "reason": "blocking_denied"}])
            continue

        if not pr_number:
            add(task_id, ACTION_ESCALATE,
                "In Review has no recorded PR; cannot merge without provenance.",
                base_inputs, escalation_class="missing_provenance", score=96,
                skipped=[{"action": ACTION_ARM, "reason": "missing_pr"}])
            continue

        if ci_state == "pending":
            add(task_id, ACTION_HOLD_PENDING,
                "CI is still pending; T3 will not arm auto-merge yet.",
                base_inputs, score=55,
                skipped=[{"action": ACTION_ARM, "reason": "ci_pending"}])
            continue

        if ci_state in {"red", "missing", "unknown"}:
            add(task_id, ACTION_ESCALATE,
                f"Checks are {ci_state}; merge steward fails closed.",
                base_inputs,
                escalation_class=("red_ci_product_judgment" if ci_state == "red"
                                  else "failed_gate"),
                score=96 if ci_state == "red" else 92,
                skipped=[{"action": ACTION_ARM, "reason": f"ci_{ci_state}"}])
            continue

        if task_id in unsafe_tasks:
            add(task_id, ACTION_ESCALATE,
                "Unsafe Work Session blocks policy-gated merge.",
                base_inputs, escalation_class="failed_gate", score=90,
                skipped=[{"action": ACTION_ARM, "reason": "unsafe_session"}])
            continue

        if open_deps:
            add(task_id, ACTION_HOLD_DEPS,
                "Dependencies are not terminal; hold merge until they land.",
                base_inputs, score=70,
                skipped=[{"action": ACTION_ARM, "reason": "open_dependencies"}])
            continue

        if authority_missing:
            add(task_id, ACTION_ESCALATE,
                "Merge steward lacks explicit T3 authority; fail closed.",
                base_inputs, escalation_class="absent_permission", score=99,
                skipped=[{"action": ACTION_ARM, "reason": "missing_authority"}])
            continue

        if not pol.get("enabled"):
            add(task_id, ACTION_HOLD_POLICY,
                "Merge steward policy is disabled; observe only (no arm).",
                base_inputs, score=40,
                skipped=[{"action": ACTION_ARM, "reason": "policy_disabled"}])
            continue

        if saturated or arm_budget <= 0:
            add(task_id, ACTION_HOLD_BACKPRESSURE,
                "Backpressure/saturation holds merges this pass.",
                {**base_inputs, "saturated": saturated, "arm_budget": arm_budget,
                 "in_flight": in_flight},
                score=60,
                skipped=[{"action": ACTION_ARM, "reason": "backpressure"}])
            continue

        if pol.get("require_merge_gate_pass"):
            if merge_gate_fn is not None:
                gate = dict(merge_gate_fn(task=task, git_state=git_state,
                                          snapshot=snapshot, project=project) or {})
            else:
                gate = _board_gate_from_snapshot(task, git_state, latest)
            base_inputs["merge_gate"] = {
                "ok": gate.get("ok"),
                "status": gate.get("status"),
                "finding_codes": [str((f or {}).get("code") or "")
                                 for f in (gate.get("findings") or [])][:8],
            }
            if not (gate.get("ok") is True or str(gate.get("status") or "").lower() == "passed"):
                plan = classify_merge_gate_result(gate, project=project, task_id=task_id)
                add(task_id, ACTION_ESCALATE,
                    "merge_gate blocked; escalate instead of arming.",
                    base_inputs,
                    escalation_class=(plan or {}).get("escalation_class") or "failed_gate",
                    score=95,
                    skipped=[{"action": ACTION_ARM, "reason": "merge_gate_blocked"}])
                continue

        add(task_id, ACTION_ARM,
            "Policy allows T3 arm of GitHub auto-merge for this green PR.",
            base_inputs, score=80, merges=True,
            skipped=[{"action": ACTION_ESCALATE, "reason": "gate_passed"},
                     {"action": ACTION_HOLD_POLICY, "reason": "policy_enabled"}])
        armed += 1
        arm_budget -= 1

    actions.sort(key=lambda row: (-int(row["score"]), row["task_id"], row["action"]))
    return {
        "schema": PLAN_SCHEMA,
        "project": project,
        "tier": TIER,
        "generated_at": observed_at,
        "dry_run_default": True,
        "policy": pol,
        "actions": actions,
        "summary": {
            "action_count": len(actions),
            "in_review_count": len(in_review),
            "arm_count": sum(1 for row in actions if row["action"] == ACTION_ARM),
            "escalate_count": sum(1 for row in actions if row["action"] == ACTION_ESCALATE),
            "by_action": {
                name: sum(1 for row in actions if row["action"] == name)
                for name in sorted({row["action"] for row in actions})
            },
        },
        "caveats": [
            "Board-recorded CI/PR evidence is used unless merge_gate_fn is injected.",
            "T3 never sets Done; post-arm reconcile only verifies provenance later.",
            "Acting requires PM_COORDINATOR_MERGE_ACT=1 and policy enabled+authority.",
        ],
    }


def _decision_title(action: Mapping[str, Any]) -> str:
    task_id = action.get("task_id") or "project"
    return f"T3 merge steward: {action.get('action')} for {task_id}"


def _execute_action(action: Mapping[str, Any], *, project: str, actor: str,
                    operator_agent: str, dry_run: bool, policy: Mapping[str, Any],
                    arm_fn: Callable[..., Any] | None,
                    escalate_fn: Callable[..., Any] | None,
                    reconcile_fn: Callable[..., Any] | None) -> dict[str, Any]:
    chosen = {
        "action": action["action"],
        "task_id": action.get("task_id") or None,
        "policy_rule": action["policy_rule"],
        "merges": bool(action.get("merges")),
    }
    inputs = dict(action.get("inputs") or {})
    result: dict[str, Any] = {
        "status": "planned" if dry_run else "executed",
        "dry_run": dry_run,
        "effects": [],
    }

    observe_only = {
        ACTION_HOLD_PENDING, ACTION_HOLD_DEPS, ACTION_HOLD_BACKPRESSURE,
        ACTION_HOLD_POLICY, ACTION_NOOP,
    }
    if dry_run or action["action"] in observe_only:
        if dry_run and action.get("mutates"):
            result["status"] = "dry_run"
            result["effects"].append({"kind": "would_execute", "action": action["action"]})
        else:
            result["status"] = "observed"
        return {"chosen_action": chosen, "result": result, "error": None}

    task_id = str(action.get("task_id") or "").upper()
    try:
        if action["action"] == ACTION_ARM:
            if arm_fn is None:
                raise RuntimeError("arm_fn_required")
            armed = arm_fn(
                project=project,
                task_id=task_id,
                pr_number=inputs.get("pr_number"),
                head_sha=inputs.get("head_sha") or "",
                actor=actor,
                policy=policy,
            )
            result["effects"].append({"kind": "arm_auto_merge", "payload": armed})
            if armed.get("error") or armed.get("ok") is False:
                result["status"] = "arm_failed"
                result["error"] = armed.get("error") or armed.get("message") or "arm_failed"
            else:
                result["status"] = "auto_merge_armed"
                if policy.get("post_merge_reconcile") and reconcile_fn is not None:
                    recon = reconcile_fn(project=project, task_id=task_id, actor=actor)
                    result["effects"].append({"kind": "reconcile", "payload": recon})
                    result["status"] = "auto_merge_armed_reconcile_requested"

        elif action["action"] == ACTION_ESCALATE:
            if escalate_fn is None:
                raise RuntimeError("escalate_fn_required")
            plan = escalation.build_escalation_plan(
                escalation_class=str(action.get("escalation_class") or "failed_gate"),
                project=project,
                task_id=task_id,
                failed_condition=str(action.get("reason") or "merge steward escalation"),
                source={"kind": "merge_steward", "inputs": inputs},
                blocks=["merge"],
            )
            if plan is None:
                raise RuntimeError("escalation_plan_unavailable")
            delivered = escalate_fn(plan, actor=actor, alert_to=operator_agent)
            result["effects"].append({"kind": "escalation", "payload": delivered})
            result["status"] = "escalated" if delivered.get("delivered") or delivered.get("deduped") \
                else "escalation_failed"
            if delivered.get("error"):
                result["error"] = delivered.get("error")

        elif action["action"] == ACTION_VERIFY_POST_MERGE:
            if reconcile_fn is None:
                raise RuntimeError("reconcile_fn_required")
            recon = reconcile_fn(project=project, task_id=task_id, actor=actor)
            result["effects"].append({"kind": "reconcile", "payload": recon})
            result["status"] = "reconcile_requested"
        else:
            result["status"] = "observed"
    except Exception as exc:  # fail loud; preserve signal in decision result
        result["status"] = "error"
        result["error"] = str(exc)
        result["failure_class"] = "failed_gate"

    return {"chosen_action": chosen, "result": result, "error": result.get("error")}


def steward_project(project: str, *, actor: str = DEFAULT_ACTOR,
                    dry_run: bool = True, persist: bool = True,
                    policy: Mapping[str, Any] | None = None,
                    saturated: bool = False, in_flight: int = 0,
                    operator_agent: str = DEFAULT_OPERATOR,
                    now: float | None = None,
                    db_path_resolver: Callable[[str], str] | None = None,
                    activity_writer: Callable[..., Any] | None = None,
                    decision_writer: Callable[..., Any] | None = None,
                    merge_gate_fn: Callable[..., Mapping[str, Any]] | None = None,
                    arm_fn: Callable[..., Any] | None = None,
                    escalate_fn: Callable[..., Any] | None = None,
                    reconcile_fn: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Plan and optionally act on one project's In Review merge queue."""
    observed_at = float(time.time() if now is None else now)
    pol = load_merge_policy(meta=policy)

    if db_path_resolver is None or (persist and (
            activity_writer is None or decision_writer is None)):
        import store
        if db_path_resolver is None:
            def resolve_store_db_path(resolved_project: str) -> str:
                return str(store._resolve(resolved_project)["db"])

            db_path_resolver = resolve_store_db_path
        if activity_writer is None:
            activity_writer = store.append_activity
        if decision_writer is None:
            decision_writer = store.record_coordinator_decision
        if merge_gate_fn is None and not dry_run:
            def _gate(**kwargs: Any) -> Mapping[str, Any]:
                task = kwargs.get("task") or {}
                git_state = kwargs.get("git_state") or {}
                return store.merge_gate({
                    "task_id": task.get("task_id"),
                    "pr_number": git_state.get("pr_number"),
                    "pr_url": git_state.get("pr_url"),
                    "head_sha": git_state.get("head_sha"),
                    "branch": git_state.get("branch"),
                    "target_branch": "master",
                }, project=kwargs.get("project") or project, actor=actor)

            merge_gate_fn = _gate
        if arm_fn is None and not dry_run:
            def _arm(**kwargs: Any) -> dict[str, Any]:
                # Prefer GitHub auto-merge via merge_coordinator helper when available.
                number = kwargs.get("pr_number")
                if not number:
                    return {"ok": False, "error": "pr_number_required"}
                try:
                    from merge_coordinator import _enable_auto_merge
                    # Token/repo resolution stays inside merge_coordinator.main path;
                    # when called without GitHub context, fail closed loudly.
                    return {"ok": False, "error": "arm_requires_github_context",
                            "pr_number": number,
                            "hint": "Wire arm_fn or run merge_coordinator --arm"}
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "error": str(exc)}

            arm_fn = _arm
        if escalate_fn is None and not dry_run:
            def _escalate(plan: Mapping[str, Any], *, actor: str,
                          alert_to: str) -> dict[str, Any]:
                return escalation.deliver_human_escalation(
                    dict(plan), store_mod=store, actor=actor, alert_to=alert_to,
                    notify_outbound=True,
                )

            escalate_fn = _escalate
        if reconcile_fn is None and not dry_run:
            def _reconcile(**kwargs: Any) -> dict[str, Any]:
                return store.reconcile(project=kwargs.get("project") or project)

            reconcile_fn = _reconcile

    try:
        db_path = db_path_resolver(project)  # type: ignore[misc]
        snapshot = audit.collect_snapshot(db_path, project, now=observed_at)
    except Exception as exc:
        snapshot = audit.unavailable_snapshot(
            project, "project_resolution_failed", now=observed_at,
            error_type=type(exc).__name__)

    plan = plan_merge_actions(
        snapshot, policy=pol, saturated=saturated, in_flight=in_flight,
        merge_gate_fn=merge_gate_fn, now=observed_at,
    )
    executed: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    for action in plan["actions"]:
        outcome = _execute_action(
            action, project=project, actor=actor, operator_agent=operator_agent,
            dry_run=dry_run, policy=pol, arm_fn=arm_fn, escalate_fn=escalate_fn,
            reconcile_fn=reconcile_fn,
        )
        executed.append({
            "task_id": action.get("task_id"),
            "action": action["action"],
            "policy_rule": action["policy_rule"],
            "result": outcome["result"],
            "error": outcome["error"],
        })
        if persist and decision_writer is not None:
            head_sha = str((action.get("inputs") or {}).get("head_sha") or "")
            stable_key = (
                f"coord7:{project}:{action.get('task_id') or 'project'}:"
                f"{action['action']}:{head_sha or 'no-sha'}:"
                f"{'dry' if dry_run else 'act'}"
            )
            decision = decision_writer(
                author=actor,
                title=_decision_title(action),
                inputs_snapshot={
                    "tier": TIER,
                    "project": project,
                    "dry_run": dry_run,
                    "policy": {
                        "enabled": pol.get("enabled"),
                        "authority_granted": pol.get("authority_granted"),
                        "max_in_flight": pol.get("max_in_flight"),
                    },
                    "action_inputs": action.get("inputs") or {},
                    "reason": action.get("reason"),
                },
                policy_rule=action["policy_rule"],
                chosen_action=outcome["chosen_action"],
                skipped_alternatives=action.get("skipped_alternatives") or [],
                result=outcome["result"],
                project=project,
                task_id=action.get("task_id") or "",
                coordinator_agent_id=actor,
                decision_kind=("action" if (not dry_run and action.get("mutates"))
                               else "recommendation"),
                stable_key=stable_key,
                rationale=str(action.get("reason") or ""),
            )
            decisions.append({
                "decision_id": decision.get("decision_id"),
                "created": decision.get("created"),
                "error": decision.get("error"),
            })

    activity_id = None
    persistence_error = None
    if persist and activity_writer is not None:
        payload = {
            "schema": ACTIVITY_SCHEMA,
            "project": project,
            "tier": TIER,
            "actor": actor,
            "dry_run": dry_run,
            "plan": plan,
            "executed": executed,
            "decision_ids": [row.get("decision_id") for row in decisions
                             if row.get("decision_id")],
        }
        try:
            activity_id = activity_writer(ACTIVITY_KIND, actor, payload, project=project)
        except Exception as exc:
            persistence_error = {
                "error_code": "merge_steward_log_write_failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }

    ok = bool(snapshot.get("read_status", {}).get("available")) and not persistence_error
    merged_effect = bool(
        not dry_run and any(
            row.get("result", {}).get("status") in {
                "auto_merge_armed", "auto_merge_armed_reconcile_requested",
            }
            for row in executed
        )
    )
    return {
        "schema": RUN_SCHEMA,
        "project": project,
        "tier": TIER,
        "actor": actor,
        "dry_run": dry_run,
        "generated_at": observed_at,
        "plan": plan,
        "executed": executed,
        "decisions": decisions,
        "activity_id": activity_id,
        "persistence_error": persistence_error,
        "ok": ok,
        "effects": {
            "merged": merged_effect,
            "done_set": False,
            "work_state_mutated": merged_effect or bool(
                not dry_run and any(
                    row.get("result", {}).get("status") in {
                        "escalated", "reconcile_requested",
                    }
                    for row in executed
                )
            ),
            "activity_id": activity_id,
        },
    }


def steward_projects(projects: Iterable[str], *, actor: str = DEFAULT_ACTOR,
                     dry_run: bool = True, persist: bool = True,
                     policy: Mapping[str, Any] | None = None,
                     saturated: bool = False, in_flight: int = 0,
                     operator_agent: str = DEFAULT_OPERATOR,
                     now: float | None = None,
                     **hooks: Any) -> dict[str, Any]:
    receipts = []
    for raw in projects:
        project = str(raw or "").strip()
        if not project:
            continue
        receipts.append(steward_project(
            project, actor=actor, dry_run=dry_run, persist=persist, policy=policy,
            saturated=saturated, in_flight=in_flight, operator_agent=operator_agent,
            now=now, **hooks,
        ))
    return {
        "schema": RUN_SCHEMA,
        "tier": TIER,
        "actor": actor,
        "dry_run": dry_run,
        "projects": receipts,
        "ok": bool(receipts) and all(row.get("ok") for row in receipts),
        "effects": {
            "merged": any((row.get("effects") or {}).get("merged") for row in receipts),
            "done_set": False,
        },
    }


def order_eligible_prs(actions: Iterable[Mapping[str, Any]], *,
                       task_deps: Mapping[str, Iterable[str]] | None = None,
                       max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
                       saturated: bool = False) -> dict[str, Any]:
    """Optional composition helper: feed armable actions into merge_coordinator.plan_merges."""
    candidates = []
    open_ids = set()
    for action in actions:
        if action.get("action") != ACTION_ARM:
            continue
        inputs = action.get("inputs") or {}
        task_id = str(action.get("task_id") or "")
        if not task_id:
            continue
        open_ids.add(task_id)
        candidates.append(mc.PRCandidate(
            number=int(inputs.get("pr_number") or 0),
            head_sha=str(inputs.get("head_sha") or ""),
            task_ids=(task_id,),
            gate_state="success",
            claim_backed=True,
            mergeable=True,
            draft=False,
        ))
    return mc.plan_merges(
        candidates,
        task_deps={k: list(v) for k, v in (task_deps or {}).items()},
        open_task_ids=open_ids,
        max_in_flight=max_in_flight,
        saturated=saturated,
    )
