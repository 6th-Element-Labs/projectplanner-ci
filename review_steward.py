"""T2 review steward (COORD-5).

Keeps In-Review work moving toward a trustworthy green without merging:

* inspect board-recorded PR / scratchpad CI / dependency / session state
* auto-request scratchpad CI rerun on red or missing CI (bounded retries)
* dispatch a ``review_merge`` agent when CI is green and mergeability looks clear
* escalate to a human/operator only when policy requires judgment (COORD-6 path)

Default posture is dry-run (observe + log decisions). Acting requires an explicit
env flag. Merges remain COORD-7 / T3 only.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import coordinator_audit as audit

PLAN_SCHEMA = "switchboard.coordinator_review_plan.v1"
RUN_SCHEMA = "switchboard.coordinator_review_run.v1"
ACTIVITY_SCHEMA = "switchboard.coordinator_review_activity.v1"
ACTIVITY_KIND = "coordinator.review_steward.tick"
REVIEW_MERGE_SIGNAL = "review_merge.dispatch"
TIER = "T2"
DEFAULT_ACTOR = "switchboard/coordinator-t2"
DEFAULT_OPERATOR = "switchboard/operator"
DEFAULT_MAX_CI_RERUNS = 2
DEFAULT_REVIEW_RUNTIME = "cursor"

ACTION_RERUN_CI = "rerun_scratchpad_ci"
ACTION_HOLD_PENDING = "hold_pending_ci"
ACTION_DISPATCH_REVIEW = "dispatch_review_merge"
ACTION_ESCALATE = "escalate_human"
ACTION_HOLD_DEPS = "hold_for_dependencies"
ACTION_REPAIR_SESSION = "repair_session_before_review"
ACTION_INSPECT_EVIDENCE = "inspect_missing_pr_or_offline_evidence"
ACTION_NOOP = "noop"

POLICY = {
    ACTION_RERUN_CI: "coord.review.rerun_scratchpad",
    ACTION_HOLD_PENDING: "coord.review.hold_pending_ci",
    ACTION_DISPATCH_REVIEW: "coord.review.dispatch_review_merge",
    ACTION_ESCALATE: "coord.review.escalate_human_judgment",
    ACTION_HOLD_DEPS: "coord.review.hold_for_dependencies",
    ACTION_REPAIR_SESSION: "coord.review.repair_session",
    ACTION_INSPECT_EVIDENCE: "coord.review.inspect_evidence",
    ACTION_NOOP: "coord.review.noop",
}


def enabled_from_env(name: str, default: bool = True) -> bool:
    return audit.enabled_from_env(name, default)


def _status(value: Any) -> str:
    return audit._status(value)


def _depends_on(value: Any) -> list[str]:
    return audit._depends_on(value)


def _ci_state(ci: Mapping[str, Any] | None) -> str:
    return audit._ci_state(ci)


def _review_merge_agent(task_id: str) -> str:
    return f"review_merge/{task_id}"


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


def _count_terminal_ci_attempts(runs: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    for row in runs:
        state = _ci_state(row)
        if state in {"red", "green"}:
            count += 1
        elif _status(row.get("status")) in audit.RED_CI | audit.GREEN_CI:
            count += 1
    return count


def _open_dependencies(task: Mapping[str, Any], by_task: Mapping[str, Mapping[str, Any]]) -> list[str]:
    open_deps: list[str] = []
    missing: list[str] = []
    for dep in _depends_on(task.get("depends_on")):
        other = by_task.get(dep)
        if other is None:
            missing.append(dep)
            continue
        if _status(other.get("status")) not in audit.TERMINAL_STATUSES:
            open_deps.append(dep)
    return open_deps + missing


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


def plan_review_actions(snapshot: Mapping[str, Any], *,
                        max_ci_reruns: int = DEFAULT_MAX_CI_RERUNS,
                        now: float | None = None) -> dict[str, Any]:
    """Derive T2 actions for every In Review task from board-recorded state."""
    observed_at = float(time.time() if now is None else now)
    project = str(snapshot.get("project") or "")
    read_status = dict(snapshot.get("read_status") or {})
    actions: list[dict[str, Any]] = []

    def add(task_id: str, action: str, reason: str, inputs: dict[str, Any],
            *, escalation_class: str | None = None, score: int = 50,
            skipped: list[dict[str, Any]] | None = None) -> None:
        actions.append({
            "task_id": task_id,
            "action": action,
            "policy_rule": POLICY[action],
            "reason": reason,
            "score": score,
            "escalation_class": escalation_class,
            "inputs": inputs,
            "skipped_alternatives": skipped or [],
            "mutates": action in {ACTION_RERUN_CI, ACTION_DISPATCH_REVIEW, ACTION_ESCALATE},
            "merges": False,
        })

    if not read_status.get("available"):
        add(
            "", ACTION_ESCALATE,
            "Review steward cannot read the project database and must fail closed.",
            {"error_code": read_status.get("error_code"),
             "error_type": read_status.get("error_type")},
            escalation_class="failed_gate", score=100,
            skipped=[{"action": ACTION_RERUN_CI, "reason": "read_path_unavailable"},
                     {"action": ACTION_DISPATCH_REVIEW, "reason": "read_path_unavailable"}],
        )
        return {
            "schema": PLAN_SCHEMA,
            "project": project,
            "tier": TIER,
            "generated_at": observed_at,
            "dry_run_default": True,
            "actions": actions,
            "summary": {"action_count": len(actions), "in_review_count": 0},
        }

    tasks = [dict(row) for row in snapshot.get("tasks") or []]
    by_task = {str(row.get("task_id") or "").upper(): row for row in tasks}
    git_by_task = {str(row.get("task_id") or "").upper(): dict(row)
                   for row in snapshot.get("git_states") or []}
    unsafe_tasks = _unsafe_tasks(snapshot, observed_at)
    in_review = [row for row in tasks if _status(row.get("status")) == "in review"]

    for task in in_review:
        task_id = str(task.get("task_id") or "").upper()
        git_state = git_by_task.get(task_id) or {}
        pr_number = git_state.get("pr_number")
        head_sha = str(git_state.get("head_sha") or "")
        runs = _ci_runs_for_task(snapshot, task_id, head_sha=head_sha)
        latest = runs[0] if runs else None
        ci_state = _ci_state(latest)
        terminal_attempts = _count_terminal_ci_attempts(runs)
        open_deps = _open_dependencies(task, by_task)
        base_inputs = {
            "task_id": task_id,
            "status": task.get("status"),
            "pr_number": pr_number,
            "pr_url": git_state.get("pr_url"),
            "head_sha": head_sha or None,
            "ci_state": ci_state,
            "ci_run_id": (latest or {}).get("run_id"),
            "ci_run_url": (latest or {}).get("run_url"),
            "terminal_ci_attempts": terminal_attempts,
            "max_ci_reruns": int(max_ci_reruns),
            "open_dependencies": open_deps,
            "unsafe_session": task_id in unsafe_tasks,
            "human_gate": audit._human_gate(task),
        }

        if audit._human_gate(task):
            add(task_id, ACTION_ESCALATE,
                "In Review task is human-gated; T2 cannot clear it alone.",
                base_inputs, escalation_class="human_gate_required", score=95,
                skipped=[{"action": ACTION_DISPATCH_REVIEW, "reason": "human_gate"},
                         {"action": ACTION_RERUN_CI, "reason": "human_gate"}])
            continue

        if not pr_number:
            add(task_id, ACTION_INSPECT_EVIDENCE,
                "In Review has no recorded PR; inspect offline evidence or provenance.",
                base_inputs, escalation_class="missing_data", score=88,
                skipped=[{"action": ACTION_DISPATCH_REVIEW, "reason": "missing_pr"},
                         {"action": ACTION_RERUN_CI, "reason": "missing_pr"}])
            continue

        if ci_state == "pending":
            add(task_id, ACTION_HOLD_PENDING,
                "Scratchpad CI is still pending; wait for terminal evidence.",
                base_inputs, score=55,
                skipped=[{"action": ACTION_RERUN_CI, "reason": "ci_already_pending"},
                         {"action": ACTION_DISPATCH_REVIEW, "reason": "ci_not_green"}])
            continue

        if ci_state in {"red", "missing", "unknown"}:
            if terminal_attempts >= int(max_ci_reruns) and ci_state == "red":
                add(task_id, ACTION_ESCALATE,
                    "Required CI stays red after bounded steward reruns.",
                    base_inputs, escalation_class="failed_gate", score=96,
                    skipped=[{"action": ACTION_RERUN_CI, "reason": "retry_budget_exhausted"},
                             {"action": ACTION_DISPATCH_REVIEW, "reason": "ci_red"}])
            else:
                add(task_id, ACTION_RERUN_CI,
                    "Latest board-recorded CI is red or missing; request a scratchpad rerun.",
                    base_inputs, score=90 if ci_state == "red" else 80,
                    skipped=[{"action": ACTION_DISPATCH_REVIEW, "reason": f"ci_{ci_state}"},
                             {"action": ACTION_ESCALATE,
                              "reason": "retries_remain" if ci_state == "red"
                              else "missing_ci_first_attempt"}])
            continue

        if task_id in unsafe_tasks:
            add(task_id, ACTION_REPAIR_SESSION,
                "CI is green, but an unsafe Work Session blocks review/merge handoff.",
                base_inputs, escalation_class="failed_gate", score=84,
                skipped=[{"action": ACTION_DISPATCH_REVIEW, "reason": "unsafe_session"}])
            continue

        if open_deps:
            add(task_id, ACTION_HOLD_DEPS,
                "CI is green, but task dependencies are not all complete.",
                base_inputs, score=70,
                skipped=[{"action": ACTION_DISPATCH_REVIEW, "reason": "open_dependencies"}])
            continue

        add(task_id, ACTION_DISPATCH_REVIEW,
            "Board-recorded CI is green and deps are clear; dispatch review_merge "
            "(T2 does not merge).",
            base_inputs, score=75,
            skipped=[{"action": ACTION_RERUN_CI, "reason": "ci_already_green"},
                     {"action": "merge_now", "reason": "coord7_t3_only"}])

    actions.sort(key=lambda row: (-int(row["score"]), row["task_id"], row["action"]))
    return {
        "schema": PLAN_SCHEMA,
        "project": project,
        "tier": TIER,
        "generated_at": observed_at,
        "dry_run_default": True,
        "actions": actions,
        "summary": {
            "action_count": len(actions),
            "in_review_count": len(in_review),
            "by_action": {
                name: sum(1 for row in actions if row["action"] == name)
                for name in sorted({row["action"] for row in actions})
            },
        },
        "caveats": [
            "PR and CI evidence is board-recorded state, not live provider readback.",
            "T2 never merges; green CI only unlocks review_merge dispatch.",
            "Acting effects require dry_run=False (PM_COORDINATOR_REVIEW_ACT=1).",
        ],
    }


def _decision_title(action: Mapping[str, Any]) -> str:
    task_id = action.get("task_id") or "project"
    return f"T2 review steward: {action.get('action')} for {task_id}"


def _execute_action(action: Mapping[str, Any], *, project: str, actor: str,
                    operator_agent: str, review_runtime: str, dry_run: bool,
                    scratchpad_dispatcher: Callable[..., Any] | None,
                    message_sender: Callable[..., Any] | None,
                    wake_requester: Callable[..., Any] | None) -> dict[str, Any]:
    chosen = {
        "action": action["action"],
        "task_id": action.get("task_id") or None,
        "policy_rule": action["policy_rule"],
        "merges": False,
    }
    inputs = dict(action.get("inputs") or {})
    result: dict[str, Any] = {
        "status": "planned" if dry_run else "executed",
        "dry_run": dry_run,
        "effects": [],
    }

    if dry_run or action["action"] in {
        ACTION_HOLD_PENDING, ACTION_HOLD_DEPS, ACTION_REPAIR_SESSION,
        ACTION_INSPECT_EVIDENCE, ACTION_NOOP,
    }:
        if dry_run and action.get("mutates"):
            result["status"] = "dry_run"
            result["effects"].append({"kind": "would_execute", "action": action["action"]})
        else:
            result["status"] = "observed"
        return {"chosen_action": chosen, "result": result, "error": None}

    task_id = str(action.get("task_id") or "").upper()
    pr_number = inputs.get("pr_number")
    head_sha = str(inputs.get("head_sha") or "")

    try:
        if action["action"] == ACTION_RERUN_CI:
            if scratchpad_dispatcher is None:
                raise RuntimeError("scratchpad_dispatcher_required")
            if not pr_number:
                raise RuntimeError("pr_number_required_for_ci_rerun")
            dispatch = scratchpad_dispatcher(
                int(pr_number),
                head_sha=head_sha,
                project=project,
            )
            result["effects"].append({"kind": "scratchpad_dispatch", "payload": dispatch})
            result["status"] = "ci_rerun_requested" if dispatch.get("dispatched") else "ci_rerun_failed"
            if dispatch.get("error") or dispatch.get("skip_reason"):
                result["error"] = dispatch.get("error") or dispatch.get("skip_reason")

        elif action["action"] == ACTION_DISPATCH_REVIEW:
            if message_sender is None or wake_requester is None:
                raise RuntimeError("dispatch_hooks_required")
            agent_id = _review_merge_agent(task_id)
            prompt = (
                f"Review steward handoff for {task_id}.\n"
                f"PR: {inputs.get('pr_url') or pr_number}\n"
                f"head_sha: {head_sha or 'unknown'}\n"
                f"CI: {inputs.get('ci_state')} run={inputs.get('ci_run_id')}\n"
                "Inspect mergeability and review comments. Do NOT merge unless "
                "COORD-7 / T3 policy explicitly authorizes it."
            )
            idem = f"coord5-review-merge:{project}:{task_id}:{head_sha or 'no-sha'}"
            message = message_sender(
                from_agent=actor, to_agent=agent_id, message=prompt,
                task_id=task_id, requires_ack=False, signal=REVIEW_MERGE_SIGNAL,
                priority=1, project=project, idem_key=f"{idem}:msg",
            )
            wake = wake_requester(
                selector={"runtime": review_runtime, "agent_id": agent_id},
                reason=f"Review/merge stewardship for {task_id}",
                source=f"coordinator:{actor}",
                policy={
                    "mode": "message_only",
                    "kind": "review_merge",
                    "task_id": task_id,
                    "project": project,
                    "pr_number": pr_number,
                    "head_sha": head_sha or None,
                    "message_id": message.get("id"),
                },
                actor=actor, project=project, idem_key=idem,
            )
            result["effects"].extend([
                {"kind": "agent_message", "payload": {"id": message.get("id"),
                                                      "to_agent": agent_id}},
                {"kind": "wake", "payload": {
                    "wake_id": wake.get("wake_id"),
                    "requested": wake.get("requested", bool(wake.get("wake_id"))),
                    "error": wake.get("error"),
                }},
            ])
            if wake.get("error") or not wake.get("wake_id"):
                result["status"] = "dispatch_failed"
                result["error"] = wake.get("error") or wake.get("reason") or "wake_not_created"
            else:
                result["status"] = "review_merge_dispatched"

        elif action["action"] == ACTION_ESCALATE:
            if message_sender is None:
                raise RuntimeError("message_sender_required")
            body = (
                f"COORD-5 escalation for {task_id or project}: {action.get('reason')}\n"
                f"class={action.get('escalation_class')}\n"
                f"inputs={json.dumps(inputs, sort_keys=True, default=str)}"
            )
            idem = (
                f"coord5-escalate:{project}:{task_id or 'project'}:"
                f"{action.get('escalation_class')}:{inputs.get('ci_run_id') or head_sha or 'na'}"
            )
            message = message_sender(
                from_agent=actor, to_agent=operator_agent, message=body,
                task_id=task_id or None, requires_ack=True,
                ack_deadline_minutes=60, signal="coord.review.escalation",
                priority=2, project=project, idem_key=idem,
            )
            result["effects"].append({
                "kind": "escalation_message",
                "payload": {"id": message.get("id"), "to_agent": operator_agent,
                            "requires_ack": True},
            })
            result["status"] = "escalated"
        else:
            result["status"] = "observed"
    except Exception as exc:  # fail loud; decision result preserves the signal
        result["status"] = "error"
        result["error"] = str(exc)
        result["failure_class"] = "failed_gate"

    return {"chosen_action": chosen, "result": result, "error": result.get("error")}


def steward_project(project: str, *, actor: str = DEFAULT_ACTOR,
                    dry_run: bool = True, persist: bool = True,
                    max_ci_reruns: int = DEFAULT_MAX_CI_RERUNS,
                    operator_agent: str = DEFAULT_OPERATOR,
                    review_runtime: str = DEFAULT_REVIEW_RUNTIME,
                    now: float | None = None,
                    db_path_resolver: Callable[[str], str] | None = None,
                    activity_writer: Callable[..., Any] | None = None,
                    decision_writer: Callable[..., Any] | None = None,
                    scratchpad_dispatcher: Callable[..., Any] | None = None,
                    message_sender: Callable[..., Any] | None = None,
                    wake_requester: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Plan and optionally act on one project's In Review queue."""
    observed_at = float(time.time() if now is None else now)
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
        if scratchpad_dispatcher is None and not dry_run:
            import ci_scratchpad_dispatch

            def _dispatch(pr_number: int, head_sha: str = "", project: str = project):
                return ci_scratchpad_dispatch.try_dispatch_scratchpad(
                    pr_number, head_sha=head_sha, project=project)

            scratchpad_dispatcher = _dispatch
        if message_sender is None and not dry_run:
            message_sender = store.send_agent_message
        if wake_requester is None and not dry_run:
            wake_requester = store.request_wake

    try:
        db_path = db_path_resolver(project)  # type: ignore[misc]
        snapshot = audit.collect_snapshot(db_path, project, now=observed_at)
    except Exception as exc:
        snapshot = audit.unavailable_snapshot(
            project, "project_resolution_failed", now=observed_at,
            error_type=type(exc).__name__)

    plan = plan_review_actions(snapshot, max_ci_reruns=max_ci_reruns, now=observed_at)
    executed: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    for action in plan["actions"]:
        outcome = _execute_action(
            action, project=project, actor=actor, operator_agent=operator_agent,
            review_runtime=review_runtime, dry_run=dry_run,
            scratchpad_dispatcher=scratchpad_dispatcher,
            message_sender=message_sender, wake_requester=wake_requester,
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
                f"coord5:{project}:{action.get('task_id') or 'project'}:"
                f"{action['action']}:{head_sha or 'no-sha'}:"
                f"{(action.get('inputs') or {}).get('ci_run_id') or 'no-run'}:"
                f"{'dry' if dry_run else 'act'}"
            )
            decision = decision_writer(
                author=actor,
                title=_decision_title(action),
                inputs_snapshot={
                    "tier": TIER,
                    "project": project,
                    "dry_run": dry_run,
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
                "error_code": "review_steward_log_write_failed",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }

    ok = bool(snapshot.get("read_status", {}).get("available")) and not persistence_error
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
            "merged": False,
            "work_state_mutated": bool(
                not dry_run and any(
                    row.get("result", {}).get("status") in {
                        "ci_rerun_requested", "review_merge_dispatched", "escalated",
                    }
                    for row in executed
                )
            ),
            "activity_id": activity_id,
        },
    }


def steward_projects(projects: Iterable[str], *, actor: str = DEFAULT_ACTOR,
                     dry_run: bool = True, persist: bool = True,
                     max_ci_reruns: int = DEFAULT_MAX_CI_RERUNS,
                     operator_agent: str = DEFAULT_OPERATOR,
                     review_runtime: str = DEFAULT_REVIEW_RUNTIME,
                     now: float | None = None,
                     **hooks: Any) -> dict[str, Any]:
    receipts = []
    for raw in projects:
        project = str(raw or "").strip()
        if not project:
            continue
        receipts.append(steward_project(
            project, actor=actor, dry_run=dry_run, persist=persist,
            max_ci_reruns=max_ci_reruns, operator_agent=operator_agent,
            review_runtime=review_runtime, now=now, **hooks,
        ))
    return {
        "schema": RUN_SCHEMA,
        "tier": TIER,
        "actor": actor,
        "dry_run": dry_run,
        "projects": receipts,
        "ok": bool(receipts) and all(row.get("ok") for row in receipts),
        "effects": {
            "merged": False,
            "activity_ids": [row.get("activity_id") for row in receipts
                             if row.get("activity_id") is not None],
        },
    }
