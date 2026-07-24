"""Production completion tick: hydrate, classify, execute one effect, stop."""
from __future__ import annotations

import os
import subprocess
from typing import Any, Callable, Mapping, Optional

from switchboard.domain.completion.effects import plan_effect
from switchboard.domain.completion.executor import (
    CompletionEffectAdapters,
    execute_effect,
)
from switchboard.domain.completion.normalize import normalize_snapshot
from switchboard.domain.completion.state_machine import (
    build_completion_snapshot,
    classify_completion,
)


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _github_command(args: list[str], *, token: str) -> dict[str, Any]:
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    try:
        proc = subprocess.run(
            ["gh", *args], text=True, capture_output=True, check=False, env=env,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": 124,
            "stdout": str(exc.stdout or "")[:1000],
            "stderr": "GitHub command timed out after 30 seconds",
        }
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip()[:1000],
        "stderr": (proc.stderr or "").strip()[:1000],
    }


def hydrate_completion_snapshot(
    task_id: str,
    *,
    project: str,
    actor: str,
    store_mod: Any = None,
) -> dict[str, Any]:
    """Read the public production authorities for one exact-head assessment."""
    if store_mod is None:
        from switchboard.storage.repositories import projects, tasks
        get_task = tasks.get_task
        get_repo = projects.get_project_github_repo
    else:
        get_task = store_mod.get_task
        get_repo = store_mod.get_project_github_repo
    from switchboard.application.commands import merge_gate as merge_gate_command
    from switchboard.application.queries import task_session
    from switchboard.storage.repositories import provenance

    task_id = str(task_id or "").strip().upper()
    task = get_task(task_id, project=project) or {}
    git_state = _map(task.get("git_state"))
    pr_number = int(git_state.get("pr_number") or 0)
    pr_url = str(git_state.get("pr_url") or "")
    repo = str(get_repo(project) or "")
    token = provenance._github_token()
    github_pr = (
        provenance._github_pr(repo, pr_number, token)
        if repo and pr_number else {}
    ) or {}
    gate = merge_gate_command.merge_gate(
        {
            "task_id": task_id,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "repo": repo,
        },
        actor=actor,
        project=project,
        record=False,
    )
    verdict = _map(_map(task.get("review_verdict")).get("current_verdict"))
    session_health = _map(task.get("session_health"))
    sessions = list(session_health.get("latest_sessions") or [])
    work_session = _map(sessions[0]) if sessions else {}
    runner_view = task_session.execute_for(task_id, project=project, task=task) or {}
    active_runner = _map(runner_view.get("active_runner"))
    identity = _map(active_runner.get("execution"))
    runner = {
        **active_runner,
        "live": bool(active_runner),
        "generation": (
            identity.get("generation")
            or active_runner.get("execution_generation")
        ),
        "role": identity.get("role") or active_runner.get("execution_role"),
        "head_sha": identity.get("head_sha") or active_runner.get("head_sha"),
    }
    snapshot = build_completion_snapshot(
        task=task,
        github_pr=github_pr,
        required_status_contexts=list(gate.get("required_status_contexts") or []),
        status_contexts=gate.get("status_contexts"),
        review=verdict or _map(gate.get("review_gate")),
        merge_gate=gate,
        merge_queue=_map(github_pr.get("mergeQueueEntry")),
        work_session=work_session,
        runner=runner,
        merge_provenance=_map(task.get("provenance")),
    )
    return normalize_snapshot(snapshot)


def production_effect_adapters(
    *,
    project: str,
    actor: str,
    agent_id: str,
    store_mod: Any = None,
) -> CompletionEffectAdapters:
    """Bind the effect ports to existing Task Execution, GitHub, and reconcile."""
    if store_mod is None:
        from switchboard.storage.repositories import projects
        get_repo = projects.get_project_github_repo
    else:
        get_repo = store_mod.get_project_github_repo
    from switchboard.application.commands import task_execution
    from switchboard.storage.repositories import provenance

    repo = str(get_repo(project) or "")
    token = provenance._github_token()

    def start(plan: Mapping[str, Any]) -> dict[str, Any]:
        return task_execution.start_task(
            str(plan.get("task_id") or ""),
            project=project,
            actor=actor,
            agent_id=agent_id,
            role=str(plan.get("role") or "review_merge"),
            source_sha=str(plan.get("head_sha") or ""),
            reason_code=str(plan.get("reason_code") or ""),
            route=str(plan.get("route") or ""),
            findings=list(plan.get("acceptance_findings") or []),
        )

    def mark_ready(plan: Mapping[str, Any]) -> dict[str, Any]:
        number = int(plan.get("pr_number") or 0)
        return _github_command(
            ["pr", "ready", str(number), "--repo", repo], token=token,
        )

    def enqueue(plan: Mapping[str, Any]) -> dict[str, Any]:
        number = int(plan.get("pr_number") or 0)
        pr = provenance._github_pr(repo, number, token) or {}
        node_id = str(pr.get("node_id") or "")
        if not node_id:
            return {"returncode": 1, "stderr": "pull request node_id unavailable"}
        return _github_command(
            [
                "api", "graphql",
                "-f",
                (
                    "query=mutation($pullRequestId:ID!){"
                    "enqueuePullRequest(input:{pullRequestId:$pullRequestId})"
                    "{mergeQueueEntry{id state}}}"
                ),
                "-F", f"pullRequestId={node_id}",
            ],
            token=token,
        )

    def reconcile(_: Mapping[str, Any]) -> dict[str, Any]:
        return provenance.reconcile(project=project, incremental=True)

    return CompletionEffectAdapters(
        ensure_review_generation=start,
        start_remediation=start,
        mark_ready=mark_ready,
        enqueue=enqueue,
        requeue_merge_group=enqueue,
        repair_dispatch=start,
        reconcile_provenance=reconcile,
    )


def run_completion_tick(
    task_id: str,
    *,
    project: str,
    actor: str,
    agent_id: str,
    store_mod: Any = None,
    hydrator: Callable[..., dict[str, Any]] = hydrate_completion_snapshot,
    adapters: Optional[CompletionEffectAdapters] = None,
) -> dict[str, Any]:
    """Execute exactly one persisted route effect for one task."""
    from switchboard.application.commands import task_execution
    from switchboard.storage.repositories import completion_runs

    snapshot = hydrator(
        task_id, project=project, actor=actor, store_mod=store_mod,
    )
    current = completion_runs.get_active_completion_run(
        task_id, project=project,
    ) or {}
    decision = classify_completion(current, snapshot)
    plan = plan_effect(decision, snapshot, current)
    # The finding subsets are part of the effect contract, not transient
    # classifier-only state.
    plan["acceptance_findings"] = list(
        decision.get("acceptance_findings") or [])
    plan["escalated_findings"] = list(
        decision.get("escalated_findings") or [])

    def fence(_: Any) -> Any:
        try:
            return task_execution.stop_task(
                task_id, project=project, actor=actor,
                reason=(
                    f"completion route changed to {plan.get('route')} at "
                    f"{plan.get('head_sha')}"
                ),
            )
        except task_execution.TaskExecutionError as exc:
            if exc.code == "not_running":
                return {"stopped": False, "reason": "already_terminal"}
            raise

    result = execute_effect(
        plan,
        decision=decision,
        snapshot=snapshot,
        run=current,
        project=project,
        actor=actor,
        fence_generation=fence,
        adapters=adapters or production_effect_adapters(
            project=project, actor=actor, agent_id=agent_id,
            store_mod=store_mod,
        ),
    )
    return {
        "schema": "switchboard.completion_tick.v1",
        "task_id": str(task_id or "").strip().upper(),
        "snapshot": snapshot,
        "decision": decision,
        "plan": plan,
        "execution": result,
    }


__all__ = [
    "hydrate_completion_snapshot",
    "production_effect_adapters",
    "run_completion_tick",
]
