"""Production Mission Bot tick — hydrate facts, reduce, execute one port call.

This replaces the old classify → normalize → reinterpret completion path.
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid
from typing import Any, Callable, Mapping, Optional

from switchboard.domain.mission_bot import (
    MissionPorts,
    execute_mission_command,
    reduce_mission,
)
from switchboard.domain.mission_bot.outputs import MISSION_BOT_VERSION


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _github_command(args: list[str], *, token: str = "") -> dict[str, Any]:
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    completed = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    return {
        "returncode": int(completed.returncode),
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
    }


def _normalize_already_queued(result: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(result)
    stderr = str(row.get("stderr") or "")
    if int(row.get("returncode") or 0) != 0 and "already" in stderr.lower():
        row["returncode"] = 0
        row["already_queued"] = True
    return row


def hydrate_mission_snapshot(
    task_id: str,
    *,
    project: str,
    actor: str,
    store_mod: Any = None,
) -> dict[str, Any]:
    """Reuse the existing completion hydrator as the fact-read port."""
    from switchboard.application.completion_driver import hydrate_completion_snapshot

    return hydrate_completion_snapshot(
        task_id, project=project, actor=actor, store_mod=store_mod,
    )


def production_mission_ports(
    *,
    project: str,
    actor: str,
    agent_id: str,
    store_mod: Any = None,
    scope_authority: Optional[Mapping[str, Any]] = None,
    scope_project: str = "",
) -> MissionPorts:
    """Bind Mission Bot outputs to start_task / GitHub / persist ports."""
    from switchboard.application.commands import task_execution
    from switchboard.storage.repositories import provenance

    # Lazy: hermetic tests patch completion_driver.ensure_completion_run.
    def _ensure_completion_run(**kwargs: Any) -> dict[str, Any]:
        from switchboard.application.completion_driver import ensure_completion_run
        return ensure_completion_run(**kwargs)

    # Hermetic conformance passes store_mod=object() and never needs GitHub
    # identity here — adapters override mark_ready / arm_merge. Only resolve
    # the repo/token when talking to the live store.
    if store_mod is None:
        from switchboard.storage.repositories import projects
        repo = str(projects.get_project_github_repo(project) or "")
        token = provenance._github_token()
    else:
        get_repo = getattr(store_mod, "get_project_github_repo", None)
        repo = str(get_repo(project) or "") if callable(get_repo) else ""
        token = ""

    def require_scope(task_id: str) -> None:
        if scope_authority is None:
            raise RuntimeError("scope_authority_required")
        validator = getattr(store_mod, "validate_autopilot_scope_authority", None)
        if not callable(validator):
            from switchboard.storage.repositories.autopilot_scopes import (
                validate_autopilot_scope_authority,
            )
            validator = validate_autopilot_scope_authority
        authority = dict(scope_authority)
        verdict = validator(
            authority,
            project=scope_project or project,
            deliverable_id=str(authority.get("deliverable_id") or ""),
            task_project=project,
            task_id=task_id,
        )
        if verdict.get("allowed") is not True:
            reason = ",".join(verdict.get("reason_codes") or [])
            raise RuntimeError(
                str(verdict.get("error") or "scope_authority_denied")
                + (f":{reason}" if reason else "")
            )

    def start_task(plan: Mapping[str, Any]) -> dict[str, Any]:
        from switchboard.domain.mission_bot.dossier import prompt_safe_dossier

        dossier = prompt_safe_dossier(_map(plan.get("dossier")))
        task_id = str(plan.get("task_id") or "")
        require_scope(task_id)
        return task_execution.start_task(
            task_id,
            project=project,
            actor=actor,
            agent_id=agent_id,
            role=str(plan.get("role") or "remediation"),
            source_sha=str(plan.get("head_sha") or ""),
            reason_code=str(plan.get("reason_code") or ""),
            route=str(plan.get("route") or ""),
            findings=list(dossier.get("acceptance_findings") or []),
            decision_attempt=int(plan.get("decision_attempt") or 0),
            state_version=int(plan.get("state_version") or 0),
            mission_key=str(
                plan.get("idem_key") or plan.get("idempotency_key") or ""),
            mission_dossier=dossier,
            instruction=_mission_instruction(plan),
        )

    def mark_ready(plan: Mapping[str, Any]) -> dict[str, Any]:
        require_scope(str(plan.get("task_id") or ""))
        number = int(plan.get("pr_number") or 0)
        return _github_command(
            ["pr", "ready", str(number), "--repo", repo], token=token,
        )

    def arm_merge(plan: Mapping[str, Any]) -> dict[str, Any]:
        require_scope(str(plan.get("task_id") or ""))
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

    def observe_merged(
        *, command: Mapping[str, Any], snapshot: Mapping[str, Any],
        run: Mapping[str, Any], project: str, actor: str,
    ) -> dict[str, Any]:
        del run
        snap = _map(snapshot)
        require_scope(str(command.get("task_id") or snap.get("task_id") or ""))
        # Always stamp merge provenance in production — do not invent Done.
        # Hermetic conformance injects store_mod=object() with no task surface;
        # skip network there. Production coordinators pass the live store, which
        # previously skipped reconcile entirely when store_mod was truthy.
        reconcile_result: dict[str, Any] = {}
        live_store = store_mod is None or callable(
            getattr(store_mod, "get_task", None)
        )
        if live_store:
            from switchboard.application.commands.reconcile_task_merge import (
                execute as reconcile_task_merge,
            )
            from switchboard.storage.repositories.provenance import (
                task_merge_reconcile_subject,
            )
            from switchboard.storage.repositories import projects
            reconcile_result = reconcile_task_merge(
                str(command.get("task_id") or snap.get("task_id") or ""),
                project=project,
                actor=actor or "mission_bot/observe_merged",
                load_subject=task_merge_reconcile_subject,
                canonical_repo_for=projects.get_project_github_repo,
                fetch_pull_request=lambda repo, number: provenance._github_pr(
                    repo, number, token=provenance._github_token(repo),
                ),
                mark_merged=provenance.mark_task_merged,
            )
        reconcile_error = str(_map(reconcile_result).get("error") or "").strip()
        if reconcile_error:
            # Failed GitHub reconciliation must not persist Done.
            decision = {
                "state": "reconciling",
                "route": "reconcile",
                "reason_code": "reconcile_failed",
                "desired_role": None,
                "board_projection": str(
                    snap.get("board_status") or "In Review"
                ),
                "effect": "reconcile_provenance",
            }
            _ensure_completion_run(
                decision=decision,
                snapshot=snapshot,
                current={},
                actor=actor,
                project=project,
            )
            return {
                "action": "reconcile_failed",
                "error": reconcile_error,
                "head_sha": command.get("head_sha"),
                "decision": decision,
                "reconcile": reconcile_result,
                "snapshot_task_id": snap.get("task_id"),
                "project": project,
                "actor": actor,
            }
        decision = {
            "state": "reconciling",
            "route": "reconcile",
            "reason_code": command.get("reason_code") or "canonical_pr_merged",
            "desired_role": None,
            "board_projection": "Done",
            "effect": "reconcile_provenance",
        }
        _ensure_completion_run(
            decision=decision,
            snapshot=snapshot,
            current={},
            actor=actor,
            project=project,
        )
        return {
            "action": "canonical_provenance_observed",
            "head_sha": command.get("head_sha"),
            "decision": decision,
            "reconcile": reconcile_result,
            "snapshot_task_id": snap.get("task_id"),
            "project": project,
            "actor": actor,
        }

    return MissionPorts(
        start_task=start_task,
        mark_ready=mark_ready,
        arm_merge=arm_merge,
        observe_merged=observe_merged,
    )


def _mission_instruction(plan: Mapping[str, Any]) -> str:
    """Render the dossier as the mission tape — full evidence, redacted env."""
    import json

    from switchboard.domain.mission_bot.dossier import prompt_safe_dossier

    dossier = _map(plan.get("dossier"))
    safe = prompt_safe_dossier(dossier)
    role = str(plan.get("role") or "")
    reason = str(plan.get("reason_code") or dossier.get("reason_code") or "")
    header = [
        f"YOUR MISSION ({role or 'agent'}): {dossier.get('mission') or reason}",
        f"task_id={plan.get('task_id')}",
        f"pr_number={plan.get('pr_number') or dossier.get('pr_number') or 0}",
        f"head_sha={plan.get('head_sha') or dossier.get('head_sha') or ''}",
        f"reason_code={reason}",
        "DOSSIER_JSON_BEGIN",
        json.dumps(safe, sort_keys=True, default=str),
        "DOSSIER_JSON_END",
        "Use Switchboard MCP for the how. CI, review, merge, callback, claim, "
        "lease, and other factory failures are your mission to diagnose and "
        "repair; never call agent_requires_human merely because one occurred. "
        "Call agent_requires_human only when you have a concrete question that "
        "requires an operator answer, or after exhausting the available agent "
        "actions and explicitly giving up. Include the evidence and the exact "
        "question or irreducible blocker — do not invent a workaround.",
    ]
    return "\n".join(header)


def run_mission_tick(
    task_id: str,
    *,
    project: str,
    actor: str,
    agent_id: str,
    store_mod: Any = None,
    hydrator: Callable[..., dict[str, Any]] = hydrate_mission_snapshot,
    ports: Optional[MissionPorts] = None,
    shadow_only: bool = False,
    scope_authority: Optional[Mapping[str, Any]] = None,
    scope_project: str = "",
) -> dict[str, Any]:
    """Execute exactly one Mission Bot command for one task."""
    from switchboard.application.mission_bot.shadow import shadow_mission
    from switchboard.storage.repositories import completion_runs, decision_records

    snapshot = hydrator(
        task_id, project=project, actor=actor, store_mod=store_mod,
    )
    if shadow_only:
        return shadow_mission(snapshot)

    current = completion_runs.get_active_completion_run(
        task_id, project=project,
    ) or {}
    command = reduce_mission(snapshot)
    command["decision_attempt"] = int(current.get("attempt") or 0)
    command["state_version"] = int(current.get("state_version") or 0)

    dossier = _map(command.get("dossier"))
    recorded_decision = {
        "state": command.get("state"),
        "route": command.get("route"),
        "reason_code": command.get("reason_code"),
        "desired_role": command.get("desired_role"),
        "board_projection": command.get("board_projection"),
        "effect": command.get("effect"),
        "mission_output": command.get("output"),
        "acceptance_findings": list(dossier.get("acceptance_findings") or []),
    }
    for key in (
        "failing_contexts",
        "failing_check_url",
        "failing_run_attempt",
        "failing_check_summary",
        "missing_artifact",
    ):
        value = dossier.get(key)
        if value:
            recorded_decision[key] = value
    decision_record = decision_records.record_decision_episode(
        project=project,
        snapshot=snapshot,
        decision=recorded_decision,
        classifier_version=MISSION_BOT_VERSION,
        advice_version=None,
    )
    if dossier:
        dossier["evidence_ref"] = {
            "schema": "switchboard.mission_evidence_ref.v1",
            "project": project,
            "task_id": str(task_id or "").strip().upper(),
            "head_sha": str(command.get("head_sha") or ""),
            "decision_id": str(
                decision_record.get("decision_id")
                or decision_record.get("episode_id")
                or ""
            ),
            "read_via": "list_decision_episodes",
        }
        command["dossier"] = dossier

    bound = ports or production_mission_ports(
        project=project, actor=actor, agent_id=agent_id, store_mod=store_mod,
        scope_authority=scope_authority,
        scope_project=scope_project,
    )
    result = execute_mission_command(
        command,
        ports=bound,
        decision=command,
        snapshot=snapshot,
        run=current,
        project=project,
        actor=actor,
    )
    terminal_outcome = str(command.get("terminal_outcome") or "")
    if terminal_outcome == "human_resolved":
        result["terminal_observation"] = decision_records.mark_human_intervention(
            project=project,
            task_id=task_id,
            human_action="mission_bot:terminal_operator_intent",
            resolved=True,
        )
    elif terminal_outcome == "abandoned":
        result["terminal_observation"] = decision_records.abandon_open_episodes(
            project=project,
            task_id=task_id,
        )
    result["snapshot"] = {
        "task_id": snapshot.get("task_id"),
        "head_sha": snapshot.get("head_sha"),
        "pr_number": snapshot.get("pr_number"),
        "snapshot_id": snapshot.get("snapshot_id") or f"snapshot-{uuid.uuid4().hex}",
    }
    result["mission_bot_version"] = MISSION_BOT_VERSION
    result["decision_record"] = decision_record
    result["observed_at"] = time.time()
    return result


__all__ = [
    "hydrate_mission_snapshot",
    "production_mission_ports",
    "run_mission_tick",
]
