"""Production completion tick: hydrate, classify, execute one effect, stop."""
from __future__ import annotations

import os
import subprocess
from typing import Any, Callable, Mapping, Optional

from switchboard.domain.completion.effects import plan_effect
from switchboard.domain.completion.executor import (
    CompletionEffectAdapters,
    ensure_completion_run,
    execute_effect,
)
from switchboard.domain.completion.normalize import normalize_snapshot
from switchboard.domain.completion.state_machine import (
    COMPLETION_CLASSIFIER_VERSION,
    build_completion_snapshot,
    classify_completion,
)


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


_QUEUE_EFFECTS = frozenset({"enqueue"})

#: GitHub's refusal when the PR is *already* in the merge queue. That is the
#: desired end state of ``enqueue``, not a failure.
_ALREADY_QUEUED_MARKERS = (
    "already in the queue",
    "already queued",
    "pull request is already in a merge queue",
)


def _normalize_already_queued(result: Mapping[str, Any]) -> dict[str, Any]:
    """Report "the state you asked for already holds" as success.

    A non-zero ``gh`` exit whose message is "Pull request is already in the
    queue" was recorded as a FAILED completion effect. Each such tick counted as
    non-progress, and enough of them tripped the no-spin convergence ladder into
    ``human(completion_not_converging)`` — pulling an operator in to resolve a
    healthy, correctly-queued PR that then merged unattended (BUG-201, live on
    COORD-68 2026-07-27; AUTOPILOT-BREAKDOWNS 59).

    An effect is idempotent precisely when a replay that changes nothing still
    leaves the world in the requested state, so this must converge, not escalate.
    """
    out = dict(result or {})
    if int(out.get("returncode") or 0) == 0:
        return out
    haystack = f"{out.get('stderr') or ''}\n{out.get('stdout') or ''}".lower()
    if not any(marker in haystack for marker in _ALREADY_QUEUED_MARKERS):
        return out
    out["returncode"] = 0
    out["idempotent_already_queued"] = True
    return out


def _merge_queue_snapshot(
    merge_queue_entry: Mapping[str, Any] | None,
    *,
    task_id: str,
    head_sha: str,
    project: str,
) -> dict[str, Any]:
    """Project live GitHub queue state plus Autopilot enqueue provenance.

    When GitHub has no ``mergeQueueEntry`` but completion already verified an
    enqueue for this exact head, surface ``prior_enqueue_verified`` so a later
    queue eject becomes an explicit operator decision instead of a custom loop.
    """
    queue = _map(merge_queue_entry)
    head = str(head_sha or "").strip().lower()
    task = str(task_id or "").strip().upper()
    if not task or not head:
        return queue
    try:
        from switchboard.storage.repositories import external_effects
        rows = list(
            external_effects.list_external_effects(
                effect_type="completion_effect",
                status="verified",
                task_id=task,
                project=project,
            )
            or []
        )
    except Exception:
        return queue
    matched: list[dict[str, Any]] = []
    for row in rows:
        item = _map(row)
        if _text(item.get("resource")) not in _QUEUE_EFFECTS:
            continue
        payload = _map(item.get("payload"))
        effect_head = str(payload.get("head_sha") or "").strip().lower()
        # Exact-head only: a missing ledger head_sha is not provenance for the
        # current tip (stale or under-hydrated rows must not invent eject state).
        if not effect_head or effect_head != head:
            continue
        matched.append(item)
    if not matched:
        return queue
    newest = max(
        matched,
        key=lambda item: float(_map(item).get("updated_at") or 0),
    )
    enriched = dict(queue)
    enriched["prior_enqueue_verified"] = True
    enriched["prior_enqueue_effect_key"] = str(newest.get("effect_key") or "")
    enriched["verified_queue_effect_count"] = len(matched)
    removal = (
        enriched.get("last_removal_reason")
        or enriched.get("removal_reason")
        or _map(newest.get("readback")).get("removal_reason")
    )
    if removal and not enriched.get("last_removal_reason"):
        enriched["last_removal_reason"] = removal
    return enriched


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
    # Fetch once and hand the same record to merge_gate. Dual independent fetches
    # previously let the snapshot see an empty PR while the gate still observed
    # DIRTY, which the classifier then misreported as exact_head_pr_missing
    # (BUG-182).
    github_pr = (
        provenance._github_pr(repo, pr_number, token)
        if repo and pr_number else {}
    ) or {}
    gate_payload: dict[str, Any] = {
        "task_id": task_id,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "repo": repo,
    }
    if github_pr:
        gate_payload["evidence"] = {"github_pr": github_pr}
    gate = merge_gate_command.merge_gate(
        gate_payload,
        actor=actor,
        project=project,
        record=False,
    )
    # Prefer the PR merge_gate resolved so a successful gate fetch still reaches
    # the classifier when the hydrator's direct call returned empty.
    resolved_pr = _map(gate.get("github_pr")) or github_pr
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
        "execution_id": identity.get("execution_id"),
        "execution_connection_id": _map(
            active_runner.get("metadata")).get("execution_connection_id"),
        "generation": (
            identity.get("generation")
            or active_runner.get("execution_generation")
        ),
        "fence_epoch": identity.get("fence_epoch"),
        "role": identity.get("role") or active_runner.get("execution_role"),
        "head_sha": identity.get("head_sha") or active_runner.get("head_sha"),
    }
    head_sha = str(
        _map(resolved_pr.get("head")).get("sha")
        or git_state.get("head_sha")
        or ""
    ).strip()
    snapshot = build_completion_snapshot(
        task=task,
        github_pr=resolved_pr,
        required_status_contexts=list(gate.get("required_status_contexts") or []),
        status_contexts=gate.get("status_contexts"),
        review=verdict or _map(gate.get("review_gate")),
        merge_gate=gate,
        merge_queue=_merge_queue_snapshot(
            resolved_pr.get("mergeQueueEntry"),
            task_id=task_id,
            head_sha=head_sha,
            project=project,
        ),
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
            decision_attempt=int(plan.get("decision_attempt") or 0),
            state_version=int(plan.get("state_version") or 0),
        )

    def mark_ready(plan: Mapping[str, Any]) -> dict[str, Any]:
        number = int(plan.get("pr_number") or 0)
        return _github_command(
            ["pr", "ready", str(number), "--repo", repo], token=token,
        )

    def update_branch(plan: Mapping[str, Any]) -> dict[str, Any]:
        """Advance one behind PR, then stop.

        The next Autopilot tick rehydrates the new head. CI and exact-head
        review must pass again before the classifier can emit ``enqueue``.
        """
        number = int(plan.get("pr_number") or 0)
        return _github_command(
            ["api", "-X", "PUT", f"repos/{repo}/pulls/{number}/update-branch"],
            token=token,
        )

    def enqueue(plan: Mapping[str, Any]) -> dict[str, Any]:
        """Arm squash auto-merge; GitHub's native queue owns serialization."""
        number = int(plan.get("pr_number") or 0)
        pr = provenance._github_pr(repo, number, token) or {}
        node_id = str(pr.get("node_id") or "")
        if not node_id:
            return {"returncode": 1, "stderr": "pull request node_id unavailable"}
        return _normalize_already_queued(_github_command(
            [
                "api", "graphql",
                "-f",
                (
                    "query=mutation($pullRequestId:ID!){"
                    "enablePullRequestAutoMerge(input:{"
                    "pullRequestId:$pullRequestId,mergeMethod:SQUASH})"
                    "{pullRequest{id autoMergeRequest{mergeMethod enabledAt}}}}"
                ),
                "-F", f"pullRequestId={node_id}",
            ],
            token=token,
        ))

    def reconcile(_: Mapping[str, Any]) -> dict[str, Any]:
        return provenance.reconcile(project=project, incremental=True)

    def fence_only(plan: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "action": "stopping",
            "runner_session_id": _map(
                plan.get("fence_identity")).get("runner_session_id"),
        }

    return CompletionEffectAdapters(
        ensure_review_generation=start,
        start_remediation=start,
        mark_ready=mark_ready,
        update_branch=update_branch,
        enqueue=enqueue,
        repair_dispatch=start,
        fence_runner=fence_only,
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
    from switchboard.storage.repositories import completion_runs, decision_records

    snapshot = hydrator(
        task_id, project=project, actor=actor, store_mod=store_mod,
    )
    current = completion_runs.get_active_completion_run(
        task_id, project=project,
    ) or {}
    decision = classify_completion(current, snapshot)
    # COORD-50: retain the classifier's input and output on EVERY tick, not only
    # the ones that pull a human in. completion_runs overwrites reason_code in
    # place, so this append-only episode is the only durable timeline. advice_version
    # is NULL until Phase 4 makes advice live — the control arm needs the column
    # populated from the first row, not retrofitted (spec §7).
    episode = decision_records.record_decision_episode(
        project=project,
        snapshot=snapshot,
        decision=decision,
        classifier_version=COMPLETION_CLASSIFIER_VERSION,
        advice_version=None,
    )
    # Human projection and its Needs-you authority must commit atomically in
    # execute_effect; pre-persisting Blocked(route=human) could strand a task
    # without an operator-visible request.
    persisted = (
        current
        if str(decision.get("route") or "") == "human"
        else ensure_completion_run(
            decision=decision,
            snapshot=snapshot,
            current=current,
            actor=actor,
            project=project,
        )
    )
    plan = plan_effect(decision, snapshot, persisted)

    def fence(identity: Any) -> Any:
        return task_execution.fence_task_generation(
            task_id,
            _map(identity),
            project=project,
            actor=actor,
            reason=(
                f"completion route changed to {plan.get('route')} at "
                f"{plan.get('head_sha')}"
            ),
        )

    result = execute_effect(
        plan,
        decision=decision,
        snapshot=snapshot,
        run=persisted,
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
        "decision_record": {
            "record_id": episode.get("record_id"),
            "snapshot_hash": episode.get("snapshot_hash"),
            "tick_count": episode.get("tick_count"),
            "reason_code_registered": episode.get("reason_code_registered"),
        },
    }


__all__ = [
    "hydrate_completion_snapshot",
    "production_effect_adapters",
    "run_completion_tick",
]
