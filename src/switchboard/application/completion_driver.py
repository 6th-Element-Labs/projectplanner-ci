"""Production completion tick — Mission Bot only.

Hydrate authoritative facts, reduce to one of eight Mission Bot outputs,
execute exactly one port call. Old classify/normalize reinterpretation is gone.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from typing import Any, Callable, Mapping, Optional

from switchboard.application.mission_bot.driver import (
    arm_squash_auto_merge,
    production_mission_ports,
    run_mission_tick,
)
from switchboard.application.mission_bot.shadow import shadow_mission
from switchboard.domain.completion.executor import (
    CompletionEffectAdapters,
    ensure_completion_run,
)
from switchboard.domain.completion.normalize import normalize_snapshot
from switchboard.domain.completion.normalization_law import (
    NORMALIZATION_TABLE_VERSION,
)
from switchboard.domain.completion.state_machine import build_completion_snapshot
from switchboard.domain.mission_bot import canonical_task_id, reduce_mission
from switchboard.domain.mission_bot.outputs import MISSION_BOT_VERSION


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _fresh_decision_context(current: Mapping[str, Any] | None) -> dict[str, Any]:
    """Retired with the classifier. Mission Bot never reads stored outcomes."""
    del current
    return {}


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
    live_auto_merge_active: bool | None = None,
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
    if live_auto_merge_active is not None:
        enriched["live_auto_merge_active"] = bool(live_auto_merge_active)
    try:
        enriched["prior_enqueue_age_s"] = max(
            0.0, time.time() - float(newest.get("updated_at") or 0))
    except (TypeError, ValueError):
        pass
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
    from switchboard.storage.repositories import (
        autopilot_scopes,
        completion_runs,
        provenance,
    )

    hydration_started_at = time.time()
    source_observed_at: dict[str, float] = {}
    task_id = canonical_task_id(task_id)
    task = get_task(task_id, project=project) or {}
    task_observed_at = time.time()
    source_observed_at.update({
        "task": task_observed_at,
        "canonical_github_provenance": task_observed_at,
    })
    scopes = autopilot_scopes.list_autopilot_scopes(
        project=project,
        task_project=project,
        task_id=task_id,
        limit=500,
    )
    matching_scopes = [
        _map(scope) for scope in scopes
        # Board ids keep their hyphens: `_text` is an enum normalizer that maps
        # "QA-24" -> "qa_24", so filtering task_id through it matched nothing
        # and snapshot["autopilot_scope"] was permanently {} — the W2 stopped-
        # scope fence (reducer step 2) could never fire for a real board id.
        if _text(_map(scope).get("scope_type")) == "task"
        and canonical_task_id(_map(scope).get("task_id")) == task_id
    ]
    task_scope = max(
        matching_scopes,
        key=lambda scope: (
            float(scope.get("updated_at") or 0),
            str(scope.get("scope_id") or ""),
        ),
        default={},
    )
    source_observed_at["autopilot_scope"] = time.time()
    git_state = _map(task.get("git_state"))
    pr_number = int(git_state.get("pr_number") or 0)
    pr_url = str(git_state.get("pr_url") or "")
    repo = str(get_repo(project) or "")
    token = provenance._github_token()
    # Fetch once and hand the same record to merge_gate. Dual independent fetches
    # previously let the snapshot see an empty PR while the gate still observed
    # DIRTY, which the classifier then misreported as exact_head_pr_missing
    # (BUG-182).
    # Hydration must not crash on a GitHub outage: fall back to an empty PR and
    # let merge_gate's own fetch record the failure cause in its finding.
    github_pr_fetch_error: dict[str, Any] = {}
    try:
        github_pr = (
            provenance._github_pr(repo, pr_number, token)
            if repo and pr_number else {}
        ) or {}
    except provenance.GitHubPRFetchError as exc:
        # BUG-235's typed error means "the fetch failed", never "the PR is
        # gone" (a true 404 returns None without raising). Preserve that
        # distinction so facts can WAIT out a rate-limit/timeout instead of
        # reading it as the definite verdict github_pr_state_unavailable.
        github_pr = {}
        github_pr_fetch_error = {"transient": True, "detail": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001
        github_pr = {}
        github_pr_fetch_error = {"transient": True, "detail": str(exc)[:200]}
    github_pr_observed_at = time.time()
    source_observed_at["github_pr"] = github_pr_observed_at
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
    gate_observed_at = time.time()
    source_observed_at.update({
        "github_check_runs": gate_observed_at,
        "required_status_contexts": gate_observed_at,
        "review_verdict": gate_observed_at,
        "merge_gate": gate_observed_at,
    })
    # Prefer the PR merge_gate resolved so a successful gate fetch still reaches
    # the classifier when the hydrator's direct call returned empty.
    resolved_pr = _map(gate.get("github_pr")) or github_pr
    verdict = _map(_map(task.get("review_verdict")).get("current_verdict"))
    session_health = _map(task.get("session_health"))
    sessions = list(session_health.get("latest_sessions") or [])
    work_session = _map(sessions[0]) if sessions else {}
    # BUG-241: the health projection carries no hygiene, and the newest session
    # is exactly the fresh non-blocked one when a task keeps being dispatched —
    # so a server-stamped agent_requires_human receipt (which lives in the
    # BLOCKED session's hygiene.blocker) never reached the snapshot and
    # facts.agent_requires_human could not fire in production. Prefer a blocked
    # session and hydrate its full row so hygiene travels with it.
    blocked_session = next(
        (row for row in (_map(item) for item in sessions)
         if str(row.get("status") or "").strip().lower() == "blocked"),
        None,
    )
    session_pick = blocked_session or work_session
    session_pick_id = str(session_pick.get("work_session_id") or "").strip()
    if session_pick_id and not session_pick.get("hygiene"):
        try:
            from switchboard.storage.repositories.work_sessions import get_work_session
            full_session = get_work_session(session_pick_id, project=project)
            work_session = _map(full_session) if full_session else session_pick
        except Exception:  # noqa: BLE001
            work_session = session_pick
    else:
        work_session = session_pick
    runner_view = task_session.execute_for(task_id, project=project, task=task) or {}
    runner_observed_at = time.time()
    source_observed_at["runner_sessions"] = runner_observed_at
    active_runner = _map(runner_view.get("active_runner"))
    active_attempt = _map(runner_view.get("active_attempt"))
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
    merge_queue = _merge_queue_snapshot(
        resolved_pr.get("mergeQueueEntry"),
        task_id=task_id,
        head_sha=head_sha,
        project=project,
        # REST always carries auto_merge; GitHub clears it when the queue
        # ejects a PR, so this is the only live disarm signal production has
        # (mergeQueueEntry is GraphQL-only and never hydrated here).
        live_auto_merge_active=(
            None if not resolved_pr else bool(resolved_pr.get("auto_merge"))
        ),
    )
    external_effects_observed_at = time.time()
    source_observed_at.update({
        "merge_queue": external_effects_observed_at,
        "external_effects": external_effects_observed_at,
    })
    snapshot = build_completion_snapshot(
        task=task,
        github_pr=resolved_pr,
        required_status_contexts=list(gate.get("required_status_contexts") or []),
        status_contexts=gate.get("status_contexts"),
        review=verdict or _map(gate.get("review_gate")),
        merge_gate=gate,
        merge_queue=merge_queue,
        work_session=work_session,
        runner=runner,
        merge_provenance=_map(task.get("provenance")),
    )
    normalized = normalize_snapshot(snapshot)
    observed_at = time.time()
    normalized["snapshot_id"] = f"snapshot-{uuid.uuid4().hex}"
    normalized["observed_at"] = observed_at
    normalized["hydration_started_at"] = hydration_started_at
    normalized["autopilot_scope"] = task_scope
    if github_pr_fetch_error:
        normalized["github_pr_fetch_error"] = github_pr_fetch_error
    current_run = completion_runs.get_active_completion_run(
        task_id, project=project,
    ) or {}
    stale_assignment = _map(
        _map(current_run.get("evidence_refs")).get("stale_assignment")
    )
    stale_execution_id = str(stale_assignment.get("execution_id") or "").strip()
    current_execution_id = str(
        _map(active_attempt.get("execution")).get("execution_id")
        or active_attempt.get("execution_id")
        or _map(active_attempt.get("metadata")).get("execution_id")
        or runner.get("execution_id")
        or ""
    ).strip()
    if (
        stale_assignment.get("outcome") == "stale_assignment"
        and stale_execution_id
        and current_execution_id == stale_execution_id
    ):
        normalized["stale_assignment"] = {
            **stale_assignment,
            "pending": True,
        }
    source_observed_at["live_authority_evidence"] = observed_at
    normalized["source_observed_at"] = source_observed_at
    normalized["controller_build_sha"] = str(
        os.environ.get("SWITCHBOARD_BUILD_SHA")
        or os.environ.get("GIT_COMMIT")
        or "unavailable"
    ).strip()
    normalized["table_version"] = NORMALIZATION_TABLE_VERSION
    return normalized


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

    def retry_ci(plan: Mapping[str, Any]) -> dict[str, Any]:
        """Rerun one identified Actions run only when it still names this head."""
        url = str(plan.get("failing_check_url") or "")
        match = re.search(r"/actions/runs/(\d+)(?:/|$)", url)
        if not match:
            return {
                "returncode": 2,
                "stderr": "CI retry requires an identified GitHub Actions run",
            }
        run_id = match.group(1)
        readback = _github_command(
            ["api", f"repos/{repo}/actions/runs/{run_id}"], token=token,
        )
        if int(readback.get("returncode") or 0) != 0:
            return readback
        import json
        try:
            observed = json.loads(str(readback.get("stdout") or "{}"))
        except (TypeError, ValueError):
            return {"returncode": 2, "stderr": "CI run readback was not JSON"}
        expected_head = str(plan.get("head_sha") or "").strip().lower()
        observed_head = str(observed.get("head_sha") or "").strip().lower()
        if not expected_head or observed_head != expected_head:
            return {
                "returncode": 2,
                "stderr": (
                    "CI run head does not match the normalized exact head"
                ),
            }
        try:
            run_attempt = int(observed.get("run_attempt") or 0)
        except (TypeError, ValueError):
            run_attempt = 0
        if run_attempt < 1:
            return {
                "returncode": 2,
                "stderr": "CI run history does not expose an immutable attempt",
            }
        if run_attempt > 1:
            return {
                "returncode": 2,
                "stderr": "identical-head CI retry bound is exhausted",
            }
        result = _github_command(
            ["run", "rerun", run_id, "--repo", repo], token=token,
        )
        result["run_id"] = run_id
        result["head_sha"] = observed_head
        return result

    def enqueue(plan: Mapping[str, Any]) -> dict[str, Any]:
        """Arm squash auto-merge; GitHub's native queue owns serialization."""
        return arm_squash_auto_merge(
            repo=repo,
            pr_number=int(plan.get("pr_number") or 0),
            head_sha=str(plan.get("head_sha") or ""),
            token=token,
            command=_github_command,
        )

    return CompletionEffectAdapters(
        ensure_review_generation=start,
        start_remediation=start,
        mark_ready=mark_ready,
        retry_ci=retry_ci,
        enqueue=enqueue,
    )

def _mission_ports_from_adapters(
    adapters: CompletionEffectAdapters,
    *,
    project: str,
    actor: str,
    agent_id: str,
    store_mod: Any = None,
    scope_authority: Optional[Mapping[str, Any]] = None,
    scope_project: str = "",
) -> Any:
    """Bridge legacy CompletionEffectAdapters into MissionPorts (tests/shadow)."""
    from switchboard.domain.mission_bot import MissionPorts

    fallback = production_mission_ports(
        project=project, actor=actor, agent_id=agent_id, store_mod=store_mod,
        scope_authority=scope_authority,
        scope_project=scope_project,
    )

    def start_task(plan: Mapping[str, Any]) -> dict[str, Any]:
        role = str(plan.get("role") or "")
        if role == "remediation" and adapters.start_remediation:
            return adapters.start_remediation(plan)
        if role in {"review_merge", "implementation"} and adapters.ensure_review_generation:
            if role == "implementation" and adapters.start_remediation:
                return adapters.start_remediation(plan)
            return adapters.ensure_review_generation(plan)
        if adapters.start_remediation:
            return adapters.start_remediation(plan)
        return fallback.start_task(plan)

    def mark_ready(plan: Mapping[str, Any]) -> dict[str, Any]:
        if adapters.mark_ready:
            return adapters.mark_ready(plan)
        return fallback.mark_ready(plan)

    def arm_merge(plan: Mapping[str, Any]) -> dict[str, Any]:
        if adapters.enqueue:
            return adapters.enqueue(plan)
        return fallback.arm_merge(plan)

    def observe_merged(**kwargs: Any) -> dict[str, Any]:
        command = kwargs.get("command") or {}
        if adapters.reconcile_provenance:
            return adapters.reconcile_provenance(command)
        return fallback.observe_merged(**kwargs)

    return MissionPorts(
        start_task=start_task,
        mark_ready=mark_ready,
        arm_merge=arm_merge,
        observe_merged=observe_merged,
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
    shadow_observer: Optional[Callable[[Mapping[str, Any]], Any]] = None,
    scope_authority: Optional[Mapping[str, Any]] = None,
    scope_project: str = "",
) -> dict[str, Any]:
    """Mission Bot production tick. One dossier → one boot/wait/land command."""
    snapshot = hydrator(
        task_id, project=project, actor=actor, store_mod=store_mod,
    )
    command = reduce_mission(snapshot)
    observation = {
        "schema": "switchboard.mission_bot.tick_observed.v1",
        "mission_bot_version": MISSION_BOT_VERSION,
        "task_id": str(task_id or "").strip().upper(),
        "output": command.get("output"),
        "reason_code": command.get("reason_code"),
        "role": command.get("role"),
        "head_sha": command.get("head_sha"),
        "shadow": shadow_mission(snapshot),
    }
    if shadow_observer is not None:
        try:
            observation["ledger_receipt"] = shadow_observer(observation)
        except Exception as exc:  # noqa: BLE001
            observation["ledger_error"] = f"{type(exc).__name__}: {exc}"

    ports = (
        _mission_ports_from_adapters(
            adapters, project=project, actor=actor, agent_id=agent_id,
            store_mod=store_mod, scope_authority=scope_authority,
            scope_project=scope_project,
        )
        if adapters is not None
        else production_mission_ports(
            project=project, actor=actor, agent_id=agent_id, store_mod=store_mod,
            scope_authority=scope_authority,
            scope_project=scope_project,
        )
    )
    result = run_mission_tick(
        task_id,
        project=project,
        actor=actor,
        agent_id=agent_id,
        store_mod=store_mod,
        hydrator=lambda *_a, **_k: snapshot,
        ports=ports,
        scope_authority=scope_authority,
        scope_project=scope_project,
    )
    dossier = _map(command.get("dossier"))
    decision = {
        "schema": "switchboard.completion_decision.v1",
        "state": command.get("state"),
        "route": command.get("route"),
        "reason_code": command.get("reason_code"),
        "desired_role": command.get("desired_role"),
        "board_projection": command.get("board_projection"),
        "effect": command.get("effect"),
        "mission_output": command.get("output"),
        "acceptance_findings": list(dossier.get("acceptance_findings") or []),
    }
    missing_artifact = _map(dossier.get("missing_artifact"))
    if missing_artifact:
        decision["missing_artifact"] = missing_artifact
    return {
        "schema": "switchboard.completion_tick.v1",
        "controller": "mission_bot",
        "mission_bot_version": MISSION_BOT_VERSION,
        "task_id": str(task_id or "").strip().upper(),
        "snapshot": snapshot,
        "decision": decision,
        "plan": command,
        "command": command,
        "observation": observation,
        "execution": result,
        "decision_record": _map(result.get("decision_record")),
    }


__all__ = [
    "_fresh_decision_context",
    "ensure_completion_run",
    "hydrate_completion_snapshot",
    "production_effect_adapters",
    "run_completion_tick",
]
