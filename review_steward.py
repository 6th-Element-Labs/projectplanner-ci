"""T2 review steward (COORD-5).

Keeps In-Review work moving toward a trustworthy green without merging:

* inspect board-recorded PR / scratchpad CI / dependency / session state
* ensure one exact-head ``review_merge`` Connect session for every open PR
* let that session record red/missing mechanical signals without repairing code
* reserve human/operator escalation for irreducible product decisions

The lifecycle leader selects dry-run versus acting for the whole tick. This
module has no independent scheduler or activation flag.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import coordinator_audit as audit

PLAN_SCHEMA = "switchboard.coordinator_review_plan.v1"
RUN_SCHEMA = "switchboard.coordinator_review_run.v1"
ACTIVITY_SCHEMA = "switchboard.coordinator_review_activity.v1"
ACTIVITY_KIND = "coordinator.review_steward.tick"
TIER = "T2"
DEFAULT_ACTOR = "switchboard/coordinator-t2"
DEFAULT_OPERATOR = "switchboard/operator"
DEFAULT_MAX_CI_RERUNS = 2
DEFAULT_REVIEW_ACK_TIMEOUT_S = 180.0
DEFAULT_REVIEW_RESTART_TIMEOUT_S = 10.0
REVIEW_CONTROL_POLL_S = 0.25

ACTION_RERUN_CI = "rerun_scratchpad_ci"
ACTION_REMEDIATE_CI = "remediate_failed_ci"
ACTION_HOLD_PENDING = "hold_pending_ci"
ACTION_DISPATCH_REVIEW = "dispatch_review_merge"
ACTION_HOLD_GATE = "hold_mechanical_gate"
ACTION_HOLD_DEPS = "hold_for_dependencies"
ACTION_REPAIR_SESSION = "repair_session_before_review"
ACTION_INSPECT_EVIDENCE = "inspect_missing_pr_or_offline_evidence"
ACTION_NOOP = "noop"

POLICY = {
    ACTION_RERUN_CI: "coord.review.rerun_scratchpad",
    ACTION_REMEDIATE_CI: "coord.review.remediate_failed_ci",
    ACTION_HOLD_PENDING: "coord.review.hold_pending_ci",
    ACTION_DISPATCH_REVIEW: "coord.review.dispatch_review_merge",
    ACTION_HOLD_GATE: "coord.review.hold_mechanical_gate",
    ACTION_HOLD_DEPS: "coord.review.hold_for_dependencies",
    ACTION_REPAIR_SESSION: "coord.review.repair_session",
    ACTION_INSPECT_EVIDENCE: "coord.review.inspect_evidence",
    ACTION_NOOP: "coord.review.noop",
}


def _status(value: Any) -> str:
    return audit._status(value)


def _depends_on(value: Any) -> list[str]:
    return audit._depends_on(value)


def _ci_state(ci: Mapping[str, Any] | None) -> str:
    return audit._ci_state(ci)


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
            "mutates": action in {
                ACTION_RERUN_CI, ACTION_REMEDIATE_CI,
                ACTION_DISPATCH_REVIEW,
            },
            "merges": False,
        })

    if not read_status.get("available"):
        add(
            "", ACTION_HOLD_GATE,
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
        }

        if not pr_number or not head_sha:
            add(task_id, ACTION_INSPECT_EVIDENCE,
                "In Review has no complete PR/head provenance; inspect evidence before dispatch.",
                base_inputs, escalation_class="missing_data", score=88,
                skipped=[{"action": ACTION_DISPATCH_REVIEW,
                          "reason": "missing_pr_or_head"},
                         {"action": ACTION_RERUN_CI,
                          "reason": "missing_pr_or_head"}])
            continue

        if task_id in unsafe_tasks:
            add(task_id, ACTION_REPAIR_SESSION,
                "CI is green, but an unsafe Work Session blocks review/merge handoff.",
                base_inputs, escalation_class="failed_gate", score=84,
                skipped=[{"action": ACTION_DISPATCH_REVIEW, "reason": "unsafe_session"}])
            continue

        add(task_id, ACTION_DISPATCH_REVIEW,
            "An open PR has an exact head; dispatch review_merge so the agent can "
            "record the verdict and merge only if every mechanical gate is green.",
            base_inputs, score=75,
            skipped=[{"action": ACTION_REMEDIATE_CI,
                      "reason": "red remediation belongs to WATCH-17"},
                     {"action": "merge_now", "reason": "review session owns safe merge"}])

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


def _remediation_prompt(task_id: str, inputs: Mapping[str, Any]) -> str:
    return (
        f"CI is red for {task_id} at exact head {inputs.get('head_sha') or 'unknown'}.\n"
        f"PR: {inputs.get('pr_url') or inputs.get('pr_number')}\n"
        f"CI run: {inputs.get('ci_run_url') or inputs.get('ci_run_id') or 'unknown'}\n"
        "Inspect the failing checks and logs, repair the product code or tests, run the "
        "relevant checks locally, push a new head, and continue the Switchboard lifecycle. "
        "When green, review and merge through Switchboard. Do not ask the operator to "
        "manually relay this failure."
    )


def _execute_action(action: Mapping[str, Any], *, project: str, actor: str,
                    operator_agent: str, dry_run: bool,
                    scratchpad_dispatcher: Callable[..., Any] | None,
                    task_starter: Callable[..., Any] | None,
                    task_messenger: Callable[..., Any] | None,
                    control_request_waiter: Callable[..., Any] | None,
                    control_request_superseder: Callable[..., Any] | None,
                    task_retrier: Callable[..., Any] | None,
                    runner_terminal_waiter: Callable[..., Any] | None,
                    review_ack_timeout_s: float,
                    review_restart_timeout_s: float) -> dict[str, Any]:
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
        ACTION_HOLD_GATE, ACTION_INSPECT_EVIDENCE, ACTION_NOOP,
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
            if not head_sha and not pr_number:
                raise RuntimeError("sha_or_pr_required_for_ci_rerun")
            try:
                dispatch = scratchpad_dispatcher(
                    int(pr_number or 0),
                    head_sha=head_sha,
                    project=project,
                    task_id=task_id,
                )
            except TypeError:
                # Injected test doubles may still use the pre-SIMPLIFY-8 signature.
                dispatch = scratchpad_dispatcher(
                    int(pr_number or 0),
                    head_sha=head_sha,
                    project=project,
                )
            result["effects"].append({"kind": "verify_ci", "payload": dispatch})
            result["status"] = "ci_rerun_requested" if dispatch.get("dispatched") else "ci_rerun_failed"
            if dispatch.get("error") or dispatch.get("skip_reason"):
                result["error"] = dispatch.get("error") or dispatch.get("skip_reason")

        elif action["action"] == ACTION_REMEDIATE_CI:
            if task_starter is None:
                raise RuntimeError("task_starter_required")
            prompt = _remediation_prompt(task_id, inputs)
            ensured = task_starter(
                task_id, project=project, actor=actor, role="remediation",
                instruction=prompt)
            result["effects"].append({"kind": "start_task", "payload": ensured})
            if ensured.get("attached") and task_messenger is not None:
                message = task_messenger(
                    task_id, prompt, project=project, actor=actor)
                result["effects"].append({"kind": "send_message", "payload": message})
            refused = ensured.get("action") == "refused" or bool(ensured.get("error"))
            transitioning = ensured.get("action") == "transitioning"
            result["status"] = (
                "remediation_session_failed" if refused else
                "remediation_session_transitioning" if transitioning else
                "remediation_session_ensured"
            )
            if refused:
                result["error"] = ensured.get("error") or ensured.get("reason")

        elif action["action"] == ACTION_DISPATCH_REVIEW:
            if task_starter is None:
                raise RuntimeError("task_starter_required")
            prompt = (
                f"Review {task_id} via Switchboard and merge if green.\n"
                f"PR: {inputs.get('pr_url') or pr_number}\n"
                f"head_sha: {head_sha or 'unknown'}\n"
                f"CI: {inputs.get('ci_state')} run={inputs.get('ci_run_id')}\n"
                "Inspect mergeability and review comments. If the exact-head review, "
                "required CI, dependencies, and merge gate are green, merge through "
                "the configured queue and reconcile Switchboard. Otherwise record the "
                "mechanical failure and stop. Do not repair red code in this session; "
                "the remediation lifecycle owns that work; no human approval is required."
            )
            ensured = task_starter(
                task_id, project=project, actor=actor, role="review_merge",
                source_sha=head_sha, instruction=prompt)
            result["effects"].append({"kind": "start_task", "payload": ensured})
            refused = ensured.get("action") == "refused" or bool(ensured.get("error"))
            if refused:
                result["status"] = "review_handoff_failed"
                result["error"] = ensured.get("error") or ensured.get("reason")
            elif ensured.get("started"):
                result["status"] = "new_generation_started"
                result["head_sha"] = head_sha
                result["awaiting_exact_head_verdict"] = True
            elif ensured.get("attached"):
                if task_messenger is None or control_request_waiter is None:
                    raise RuntimeError("review_attach_control_lifecycle_required")
                message = task_messenger(
                    task_id, prompt, project=project, actor=actor)
                result["effects"].append({"kind": "send_message", "payload": message})
                request_id = str(message.get("control_request_id") or "").strip()
                if not request_id:
                    raise RuntimeError("review_instruction_control_request_missing")
                acknowledgement = control_request_waiter(
                    request_id, project=project, timeout_s=review_ack_timeout_s)
                result["effects"].append({
                    "kind": "await_control_request", "payload": acknowledgement})
                ack_status = str(acknowledgement.get("status") or "").lower()
                if ack_status == "completed":
                    result["status"] = "instruction_acknowledged"
                    result["control_request_id"] = request_id
                    result["head_sha"] = head_sha
                    result["awaiting_exact_head_verdict"] = True
                    result["review_completed"] = False
                elif ack_status in {"failed", "cancelled", "refused"}:
                    result["status"] = "review_handoff_failed"
                    result["error"] = (
                        acknowledgement.get("error")
                        or acknowledgement.get("reason")
                        or f"review instruction {ack_status}"
                    )
                else:
                    if control_request_superseder is None:
                        raise RuntimeError("review_timeout_recovery_required")
                    superseded = control_request_superseder(
                        request_id, project=project,
                        reason="review instruction acknowledgement timed out")
                    result["effects"].append({
                        "kind": "supersede_control_request", "payload": superseded})
                    superseded_status = str(superseded.get("status") or "").lower()
                    if superseded_status == "completed":
                        # The host won the timeout/cancel race. Delivery is real;
                        # do not kill a runner that has already accepted the review.
                        result["status"] = "instruction_acknowledged"
                        result["control_request_id"] = request_id
                        result["head_sha"] = head_sha
                        result["awaiting_exact_head_verdict"] = True
                        result["review_completed"] = False
                    else:
                        # Ack deadlines are delivery observations, never process
                        # ownership. Retry the instruction once on the same live
                        # runner, then fail loudly; lease expiry is the sole kill
                        # authority.
                        retry_message = task_messenger(
                            task_id, prompt, project=project, actor=actor)
                        result["effects"].append({
                            "kind": "retry_review_instruction", "payload": retry_message})
                        retry_id = str(retry_message.get("control_request_id") or "").strip()
                        retry_ack = (control_request_waiter(
                            retry_id, project=project, timeout_s=review_ack_timeout_s)
                            if retry_id else {"status": "failed",
                                              "reason": "retry control request missing"})
                        result["effects"].append({
                            "kind": "await_retry_control_request", "payload": retry_ack})
                        if str(retry_ack.get("status") or "").lower() == "completed":
                            result["status"] = "instruction_acknowledged"
                            result["control_request_id"] = retry_id
                            result["head_sha"] = head_sha
                            result["awaiting_exact_head_verdict"] = True
                            result["review_completed"] = False
                        else:
                            result["status"] = "review_handoff_failed"
                            result["head_sha"] = head_sha
                            result["error"] = (
                                "review instruction acknowledgement timed out after one retry; "
                                "runner left alive")
                            result["failure_reason"] = "ack_timeout_after_retry"
            else:
                result["status"] = "review_handoff_failed"
                result["error"] = (
                    ensured.get("reason") or ensured.get("message")
                    or "review generation was neither started nor attached")

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
                    now: float | None = None,
                    db_path_resolver: Callable[[str], str] | None = None,
                    activity_writer: Callable[..., Any] | None = None,
                    decision_writer: Callable[..., Any] | None = None,
                    scratchpad_dispatcher: Callable[..., Any] | None = None,
                    task_starter: Callable[..., Any] | None = None,
                    task_messenger: Callable[..., Any] | None = None,
                    control_request_waiter: Callable[..., Any] | None = None,
                    control_request_superseder: Callable[..., Any] | None = None,
                    task_retrier: Callable[..., Any] | None = None,
                    runner_terminal_waiter: Callable[..., Any] | None = None,
                    review_ack_timeout_s: float = DEFAULT_REVIEW_ACK_TIMEOUT_S,
                    review_restart_timeout_s: float = DEFAULT_REVIEW_RESTART_TIMEOUT_S,
                    **_legacy_hooks: Any) -> dict[str, Any]:
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
            from switchboard.application.commands import verify_ci as verify_ci_command

            def _dispatch(pr_number: int, head_sha: str = "", project: str = project,
                          task_id: str = ""):
                """SIMPLIFY-8: stewards re-verify through the SHA adapter only."""
                result = verify_ci_command.verify(
                    head_sha or "",
                    ensure=True,
                    project=project,
                    pr_number=int(pr_number or 0),
                    task_id=task_id,
                    actor="review-steward",
                )
                ensure = result.get("ensure_result") or {}
                return {
                    "dispatched": bool(ensure.get("dispatched") or (
                        result.get("ensured") and not result.get("error"))),
                    "skip_reason": ensure.get("skip_reason") or result.get("error"),
                    "head_sha": result.get("sha") or head_sha,
                    "run_id": result.get("run_id") or ensure.get("run_id"),
                    "verify": result,
                    "error": result.get("error") or ensure.get("error"),
                    "pr": int(pr_number or 0) or None,
                }

            scratchpad_dispatcher = _dispatch
        if task_starter is None and not dry_run:
            from switchboard.application.commands import task_execution
            task_starter = task_execution.start_task
        if task_messenger is None and not dry_run:
            from switchboard.application.commands import task_execution
            task_messenger = task_execution.send_message
        if not dry_run and (control_request_waiter is None
                            or control_request_superseder is None):
            import store

            if control_request_waiter is None:
                def _wait_control(request_id: str, *, project: str,
                                  timeout_s: float) -> dict[str, Any]:
                    deadline = time.monotonic() + max(0.0, float(timeout_s))
                    while True:
                        rows = store.list_runner_control_requests(project=project)
                        row = next((item for item in rows
                                    if item.get("request_id") == request_id), None)
                        status = str((row or {}).get("status") or "missing").lower()
                        if status in {"completed", "failed", "cancelled", "refused"}:
                            return dict(row or {})
                        if time.monotonic() >= deadline:
                            return {"request_id": request_id, "status": "timeout"}
                        time.sleep(REVIEW_CONTROL_POLL_S)

                control_request_waiter = _wait_control
            if control_request_superseder is None:
                def _supersede_control(request_id: str, *, project: str,
                                       reason: str) -> dict[str, Any]:
                    return store.complete_runner_control_request(
                        request_id, status="cancelled", actor=actor,
                        project=project, result={
                            "reason": reason,
                            "superseded": True,
                            "superseded_by": "review_ack_timeout",
                        })

                control_request_superseder = _supersede_control
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
            dry_run=dry_run,
            scratchpad_dispatcher=scratchpad_dispatcher,
            task_starter=task_starter, task_messenger=task_messenger,
            control_request_waiter=control_request_waiter,
            control_request_superseder=control_request_superseder,
            task_retrier=task_retrier,
            runner_terminal_waiter=runner_terminal_waiter,
            review_ack_timeout_s=review_ack_timeout_s,
            review_restart_timeout_s=review_restart_timeout_s,
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
                        "ci_rerun_requested", "remediation_session_ensured",
                        "remediation_session_transitioning",
                        "instruction_acknowledged", "new_generation_started",
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
            "merged": False,
            "activity_ids": [row.get("activity_id") for row in receipts
                             if row.get("activity_id") is not None],
        },
    }
