"""Durable adapter from task Start to the content-blind Connect plane.

Task Execution decides *that* a task needs a process.  This adapter records the
opaque assignment and asks the existing durable wake substrate to deliver it.
It deliberately does not create prompts, credentials, workflow roles, claims,
Work Sessions, review policy, or completion instructions.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from typing import Any

from switchboard.application.session_boot import ADVERTISED_LAUNCH_RUNTIMES
from switchboard.connect import Assignment, ResourceLimits
from switchboard.connect.execution_assignment import (
    ExecutionAssignmentError,
    context_profile_from_lifecycle,
    normalize_mission_launch_pointer,
)
from switchboard.domain.coordination.wake_intents import genuine_wake_intents
from switchboard.storage.repositories import coordination as coordination_repo
from switchboard.storage.repositories import projects as projects_repo
from switchboard.storage.repositories import tasks as tasks_repo
from switchboard.application.commands import execution_context


CONNECT_WAKE_MODE = "connect"
_RUNTIMES = {
    "codex": ("codex", "openai"),
    "openai": ("codex", "openai"),
    "claude": ("claude-code", "anthropic"),
    "claude-code": ("claude-code", "anthropic"),
    "anthropic": ("claude-code", "anthropic"),
    "cursor": ("cursor", "cursor"),
}
_UNSUPPORTED_RUNTIME_REPAIR = (
    "Call start_task with a supported runtime; do not use runtime=cli. "
    "Connect boots the CLI worker. From a launcher session do not claim_task."
)
def _runtime(value: str) -> tuple[str, str]:
    selected = _RUNTIMES.get(str(value or "codex").strip().lower())
    if not selected:
        raise ValueError("unsupported_runtime")
    return selected


def unsupported_runtime_payload(requested_runtime: str) -> dict[str, Any]:
    """Structured refusal for unknown Connect launch runtimes."""
    return {
        "dispatched": False,
        "error": "unsupported_runtime",
        "requested_runtime": str(requested_runtime or ""),
        "supported_runtimes": list(ADVERTISED_LAUNCH_RUNTIMES),
        "reason": _UNSUPPORTED_RUNTIME_REPAIR,
        "repair": _UNSUPPORTED_RUNTIME_REPAIR,
        "message": _UNSUPPORTED_RUNTIME_REPAIR,
    }


def _dependency_merge_requirements(
    task: dict[str, Any], *, project: str,
) -> list[str]:
    """Return canonical merge commits that a downstream checkout must contain.

    Dependency status controls whether Coordination may dispatch.  This second,
    immutable fence carries any canonical code provenance into Capacity so the
    host can prove the selected checkout actually contains those completed
    inputs before a runner exists.
    """
    required: list[str] = []
    for dependency_id in task.get("depends_on") or []:
        dependency = tasks_repo.get_task(str(dependency_id), project=project) or {}
        merged_sha = str(
            (dependency.get("git_state") or {}).get("merged_sha") or ""
        ).strip().lower()
        if merged_sha and merged_sha not in required:
            required.append(merged_sha)
    return required


def _hybrid_policy(
    context: dict[str, Any], task: dict[str, Any], runtime_name: str,
) -> dict[str, Any]:
    """Translate immutable execution authority into CO-9 placement input."""
    configured = dict(context.get("placement") or {})
    policy_host_classes = {
        str(item).strip() for item in configured.get("host_classes") or []
        if str(item).strip()
    }
    scheduler_host_classes = {
        "persistent" if item in {"personal", "shared"} else item
        for item in policy_host_classes
    }
    burst = dict(configured.get("burst") or {})
    provider = dict(context.get("provider") or {})
    workspace = dict(context.get("workspace") or {})
    runtime_binary = "claude" if runtime_name == "claude-code" else runtime_name
    return {
        "scheduler": {
            "mode": "hybrid",
            "prefer_persistent": True,
            "allow_persistent": "persistent" in scheduler_host_classes,
            "allow_ephemeral": "ephemeral" in scheduler_host_classes,
            "burst_enabled": burst.get("enabled") is True,
            "max_concurrent_ephemeral": int(
                burst.get("max_concurrent_ephemeral") or 0),
            "fair_share_key": f"project:{context['project_id']}",
        },
        "placement": {
            "canonical_repo": context["repository"],
            "repo_role": context["repo_role"],
            "host_classes": sorted(scheduler_host_classes),
            "trust_zones": sorted({
                str(item).strip() for item in configured.get("trust_zones") or []
                if str(item).strip()
            }),
            "isolation": (
                "task_worktree" if workspace.get("isolation") == "worktree"
                else str(workspace.get("isolation") or "")
            ),
            "workspace_backend": str(workspace.get("isolation") or ""),
            "runtime_binaries": [runtime_binary, "git"],
            "provider": str(provider.get("provider") or ""),
            "account_affinity_id": str(
                provider.get("account_affinity_id") or ""),
            "scm_provider": str((context.get("scm") or {}).get("provider") or ""),
        },
        # A bounded pending wake is the only permitted no-host outcome.
        "no_eligible_host": "wait",
        "deadline_seconds": int(
            os.environ.get("PM_CONNECT_PLACEMENT_DEADLINE_SECONDS", "900")),
    }


def _assignment_id(project: str, task_id: str, runtime: str, generation: str) -> str:
    source = f"{project}:{task_id}:{runtime}:{generation or 'initial'}"
    return "assignment-" + hashlib.sha256(source.encode()).hexdigest()[:24]


def _queued_at(task: dict[str, Any], assignment_id: str) -> float:
    """Return a stable sequence timestamp for an idempotent assignment payload.

    Task rows carry durable update/create timestamps. Using that snapshot makes
    concurrent Start calls byte-identical; a wall-clock value here would reuse
    one idempotency key with two different request hashes. The digest fallback
    exists only for adapters/tests that provide a partial task row.
    """

    for field in ("updated_at", "created_at"):
        try:
            value = float(task.get(field) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    offset = int(hashlib.sha256(assignment_id.encode()).hexdigest()[:8], 16)
    return float(1_700_000_000 + (offset % 100_000_000))


def capacity_readback(
    wake: dict[str, Any], *, project: str, runtime: str = "", lane: str = "",
) -> dict[str, Any]:
    """Join one pending wake to the online hosts that could claim it.

    This is deliberately a read model: wake intents and host heartbeats remain
    the sources of truth.  Start callers therefore learn whether they are
    queued without introducing a second capacity state that could go stale.
    """
    selector = wake.get("selector") if isinstance(wake.get("selector"), dict) else {}
    runtime = str(runtime or selector.get("runtime") or "").strip()
    lane = str(lane or selector.get("lane") or "").strip()
    try:
        hosts = coordination_repo.list_agent_hosts(
            runtime=runtime, lane=lane, project=project) or []
        pending = genuine_wake_intents(
            coordination_repo.list_wake_intents(
                status="pending", runtime=runtime, project=project) or []
        )
    except Exception as exc:
        return {
            "schema": "switchboard.connect.capacity_readback.v1",
            "wake_id": str(wake.get("wake_id") or "") or None,
            "wake_status": str(wake.get("status") or "") or None,
            "queue_position": None,
            "pending_ahead": None,
            "matching_online_hosts": [],
            "matching_online_host_count": 0,
            "readback_error": {
                "reason": "capacity_readback_unavailable",
                "detail": str(exc),
            },
        }
    matching_hosts: list[dict[str, Any]] = []
    for host in hosts:
        if (not isinstance(host, dict) or host.get("stale") or host.get("error")
                or str(host.get("status") or "online").lower() != "online"):
            continue
        capacity = host.get("capacity") if isinstance(host.get("capacity"), dict) else {}
        limits = host.get("limits") if isinstance(host.get("limits"), dict) else {}
        try:
            active = int(capacity.get("active_sessions") or 0)
        except (TypeError, ValueError):
            active = 0
        raw_max = limits.get("max_sessions")
        try:
            maximum = int(raw_max) if raw_max is not None else None
        except (TypeError, ValueError):
            maximum = None
        raw_available = host.get("available_sessions")
        try:
            available = int(raw_available) if raw_available is not None else (
                max(0, maximum - active) if maximum is not None else None)
        except (TypeError, ValueError):
            available = None
        matching_hosts.append({
            "host_id": str(host.get("host_id") or ""),
            "display_name": str(host.get("display_name") or "") or None,
            "active_sessions": active,
            "max_sessions": maximum,
            "available_sessions": available,
        })

    wake_id = str(wake.get("wake_id") or "")
    pending = [
        row for row in pending
        if isinstance(row, dict) and not row.get("error")
        and (not lane or str((row.get("selector") or {}).get("lane") or "") == lane)
    ]
    queue_position = next(
        (index for index, row in enumerate(pending, start=1)
         if str(row.get("wake_id") or "") == wake_id), None)
    ahead = max(0, queue_position - 1) if queue_position is not None else None
    result: dict[str, Any] = {
        "schema": "switchboard.connect.capacity_readback.v1",
        "wake_id": wake_id or None,
        "wake_status": str(wake.get("status") or "") or None,
        "queue_position": queue_position,
        "pending_ahead": ahead,
        "matching_online_hosts": matching_hosts,
        "matching_online_host_count": len(matching_hosts),
    }
    if not matching_hosts:
        result["no_capacity"] = {
            "reason": "no_matching_online_hosts",
            "runtime": runtime or None,
            "lane": lane or None,
        }
    return result


def enqueue_task(
    task: dict[str, Any],
    *,
    project: str,
    actor: str,
    runtime: str = "codex",
    predecessor_wake_id: str = "",
    generation_ref: str = "",
    role: str = "implementation",
    caller_agent_id: str = "",
    principal_id: str = "",
    source_sha: str = "",
    reason_code: str = "",
    acceptance_findings: list[dict[str, Any]] | None = None,
    route: str = "",
    decision_attempt: int = 0,
    state_version: int = 0,
    mission_key: str = "",
    mission_dossier: dict[str, Any] | None = None,
    mission_launch_pointer: dict[str, Any] | None = None,
    session_policy_profile: str = "",
    context_profile: str = "",
    codex_profile: str = "",
    launcher_profile: str = "",
    codex_context_profile: str = "",
    model_profile: str = "",
    requested_context_profile: str = "",
) -> dict[str, Any]:
    """Persist one provider-neutral assignment for any Start surface.

    ``generation_ref`` is an opaque lifecycle generation supplied by Task
    Execution.  Connect neither parses nor persists its meaning; it only uses
    the value to make repeated requests for one generation idempotent.  This is
    what lets an exact-head review dispatch re-arm for a new head without
    replaying forever after a terminal runner on the old head.
    """

    task_id = str(task.get("task_id") or "").strip().upper()
    if not task_id:
        return {"dispatched": False, "error": "task_id_required"}
    try:
        runtime_name, provider = _runtime(runtime)
    except ValueError as exc:
        if str(exc) == "unsupported_runtime":
            return unsupported_runtime_payload(runtime)
        return {"dispatched": False, "error": str(exc), "runtime": runtime}
    generation_ref = str(generation_ref or "").strip()
    lane = str(task.get("_wsId") or task.get("workstream") or "").strip()
    # Task Execution owns successor identity.  ``predecessor_wake_id`` remains
    # a compatibility fallback for direct legacy callers, but Connect never
    # reads wake history or combines delivery state with a named generation.
    generation = generation_ref or str(predecessor_wake_id or "")
    assignment_id = _assignment_id(project, task_id, runtime_name, generation)
    # BUG-345 / ADR-0008: task scope plus canonical repository topology is the
    # launch contract. Execution-policy records are advisory configuration and
    # must not become a second authority that can stop a scoped mission.
    try:
        selected_context_profile = context_profile_from_lifecycle({
            "context_profile": context_profile,
            "codex_profile": codex_profile,
            "launcher_profile": launcher_profile,
            "codex_context_profile": codex_context_profile,
            "model_profile": model_profile,
            "requested_context_profile": requested_context_profile,
        })
    except ExecutionAssignmentError as exc:
        return {
            "dispatched": False,
            "error": exc.code,
            "diagnostic_cause": str(exc),
            "failure_class": "invalid_input",
            "task_id": task_id,
        }
    if selected_context_profile and runtime_name != "codex":
        return {
            "dispatched": False,
            "error": "execution_assignment_context_profile_runtime_invalid",
            "failure_class": "invalid_input",
            "task_id": task_id,
            "runtime": runtime_name,
        }
    # DHCP: mint a per-task worker principal. Never hand the caller's identity
    # to the runner — that clobbers the coordinator's presence and blocks the
    # next start_task (BREAKDOWN 25). caller_agent_id stays wake auth only.
    assignment = Assignment(
        assignment_id=assignment_id,
        principal_ref=f"agent/{runtime_name}/{task_id.lower()}",
        work_ref=f"task:{project}:{task_id}",
        runtime=runtime_name,
        provider=provider,
        workspace_ref="repo:canonical",
        limits=ResourceLimits(
            max_runtime_seconds=int(os.environ.get("PM_CONNECT_MAX_RUNTIME_SECONDS", "7200")),
            spend_limit_microunits=int(
                os.environ.get("PM_CONNECT_SPEND_LIMIT_MICROUNITS", "0")),
            memory_limit_bytes=int(os.environ.get("PM_CONNECT_MEMORY_LIMIT_BYTES", "0")),
        ),
        # A named generation must remain byte-identical even when unrelated
        # task narration/activity updates the task row between coordinator ticks.
        queued_at=(_queued_at({}, assignment_id) if generation_ref
                   else _queued_at(task, assignment_id)),
    )
    selector = {
        "runtime": runtime_name,
        "provider": provider,
        "lane": lane,
        "agent_id": assignment.principal_ref,
        "task_id": task_id,
    }
    selector["capabilities"] = [
        "execution_lease_v2", "runner_lease_enforcement"]
    lifecycle = {
        "schema": "switchboard.execution_lifecycle.v1",
        "role": str(role or "implementation"),
        "head_sha": str(
            source_sha or (task.get("git_state") or {}).get("head_sha") or ""),
        "pr_number": int((task.get("git_state") or {}).get("pr_number") or 0),
        "pr_url": str((task.get("git_state") or {}).get("pr_url") or ""),
        "ttl_seconds": int(
            os.environ.get("PM_CONNECT_MAX_RUNTIME_SECONDS", "7200")),
    }
    if session_policy_profile:
        lifecycle["session_policy_profile"] = str(
            session_policy_profile).strip().lower()
    if selected_context_profile:
        lifecycle["context_profile"] = selected_context_profile
    # DISPATCH-12: ordinary implementation Starts leave identity allocation to
    # the server. Extra lifecycle fields are only for review/remediation or an
    # explicit coordination route (BUG-174 repair claims need route/task_id).
    if lifecycle["role"] in {"review_merge", "remediation"} or route:
        lifecycle.update({
            "task_id": task_id,
            "reason_code": str(reason_code or ""),
            "acceptance_findings": list(acceptance_findings or []),
            "route": str(route or ""),
            "attempt": int(decision_attempt or 0),
            "state_version": int(state_version or 0),
            # A writing/review continuation must reopen the existing PR branch.
            # Keep this out of the byte-stable base lifecycle used by ordinary
            # implementation starts.
            "pr_branch": str((task.get("git_state") or {}).get("branch") or ""),
        })
    if mission_key:
        lifecycle["mission_key"] = str(mission_key)
    requires_mission_pointer = (
        lifecycle["role"] == "remediation"
        and str(mission_key or "").strip().startswith("v4:")
    )
    if mission_launch_pointer or requires_mission_pointer:
        try:
            lifecycle["mission_launch_pointer"] = normalize_mission_launch_pointer(
                mission_launch_pointer,
                expected_head_sha=lifecycle["head_sha"],
            )
        except ExecutionAssignmentError as exc:
            return {
                "dispatched": False,
                "error": exc.code,
                "diagnostic_cause": str(exc),
                "failure_class": "missing_data",
                "role": lifecycle["role"],
                "task_id": task_id,
            }
    if mission_dossier:
        lifecycle["mission_dossier"] = dict(mission_dossier)
    if (lifecycle["role"] in {"review_merge", "remediation"}
            and not lifecycle["head_sha"]):
        return {
            "dispatched": False,
            "error": "exact_head_required",
            "role": lifecycle["role"],
            "task_id": task_id,
        }
    if lifecycle["role"] in {"review_merge", "remediation"}:
        assigned_head = str(lifecycle.get("head_sha") or "").strip().lower()
        persisted_pr_head = str(
            (task.get("git_state") or {}).get("head_sha") or ""
        ).strip().lower()
        if not persisted_pr_head or assigned_head != persisted_pr_head:
            return {
                "dispatched": False,
                "error": "execution_checkout_head_mismatch",
                "role": lifecycle["role"],
                "task_id": task_id,
                "assigned_head_sha": assigned_head or None,
                "persisted_pr_head_sha": persisted_pr_head or None,
            }
    policy = {
        "mode": CONNECT_WAKE_MODE,
        "assignment": {
            "schema": "switchboard.connect.assignment.v1",
            **asdict(assignment),
        },
        # Assignment v1 is an adapter compatibility boundary. Server-owned
        # execution identity travels beside it so older hosts can continue to
        # decode the Assignment byte-for-byte.
        "lifecycle": lifecycle,
    }
    policy["coordination_scope"] = {
        "schema": "switchboard.scoped_start_request.v1",
        "scope_type": "task",
        "task_project": project,
        "task_id": task_id,
        "runtime": runtime_name,
    }
    canonical = dict(
        (projects_repo.get_project_repo_topology(project).get("roles") or {}).get(
            "canonical") or {})
    repository = str(canonical.get("repo") or "").strip()
    if not canonical.get("configured") or not repository:
        return {
            "dispatched": False,
            "error": "canonical_repository_unconfigured",
            "reason": ("the project must name a canonical repository before "
                       "a policy-free CLI can launch"),
            "task_id": task_id,
            "project": project,
        }
    policy["repository_binding"] = {
        "schema": "switchboard.repository_binding.v1",
        "project": project,
        "repo_role": "canonical",
        "repository": repository,
        "default_branch": str(canonical.get("default_branch") or ""),
    }
    # The external effect represents one Task Execution-owned dispatch
    # generation. Diagnostic hints are deliberately excluded: agents reload
    # live evidence after boot, so changing a reason or finding must not turn
    # one mission into a conflicting external effect. A terminal predecessor
    # advances ``generation`` in Task Execution, which gives the replacement a
    # new effect key without making wake delivery state lifecycle authority.
    if lifecycle["role"] in {"review_merge", "remediation"}:
        policy["effect_identity"] = {
            "schema": "switchboard.completion_effect_identity.v1",
            "task_id": task_id,
            "head_sha": lifecycle["head_sha"],
            "route": lifecycle["route"],
            "role": lifecycle["role"],
            "attempt": lifecycle["attempt"],
            "state_version": lifecycle["state_version"],
            "dispatch_generation": generation,
        }
    suffix = generation or "initial"
    # BUG-233: completion dispatch identity changed from interpreted diagnostics
    # to the Task Execution-owned generation. Keep legacy implementation Starts
    # on v1, but cut review/remediation over to a fresh key namespace so a
    # persisted pre-cutover request hash cannot reject every replacement tick.
    idem_version = "v2" if policy.get("effect_identity") else "v1"
    wake = coordination_repo.request_wake(
        selector=selector,
        reason=f"Connect assignment {task_id}",
        source="connect",
        policy=policy,
        task_id=task_id,
        actor=actor,
        principal_id=principal_id,
        caller_agent_id=caller_agent_id,
        enforce_task_ownership=True,
        project=project,
        idem_key=(
            f"connect-start:{idem_version}:"
            f"{project}:{task_id}:{runtime_name}:{suffix}"
        ),
    )
    if str(wake.get("error") or "") == "idempotency conflict":
        # Another start owns this generation with a different request body (a
        # race, or a non-terminal predecessor). Name the condition instead of
        # leaking the storage layer's conflict string to the operator panel.
        return {
            "dispatched": False,
            "error": "dispatch_generation_conflict",
            "reason": ("another start already owns this dispatch generation; "
                       "wait for it to finish or retry"),
            "task_id": task_id,
            "project": project,
        }
    if wake.get("error") or not wake.get("wake_id"):
        return {
            "dispatched": False,
            "error": wake.get("error") or wake.get("reason") or "wake_not_created",
            "task_id": task_id,
            "project": project,
        }
    if str(wake.get("status") or "") == "failed":
        placement = dict(wake.get("placement") or {})
        result = dict(wake.get("result") or {})
        return {
            "dispatched": False,
            "error": str(
                placement.get("reason_code")
                or result.get("reason")
                or "hybrid_placement_denied"),
            "failure_class": "failed_gate",
            "task_id": task_id,
            "project": project,
            "wake_id": wake["wake_id"],
            "wake_status": "failed",
            "placement": placement,
            "scope": wake.get("scope"),
        }
    capacity = capacity_readback(
        wake, project=project, runtime=runtime_name, lane=lane)
    return {
        "dispatched": True,
        "task_id": task_id,
        "project": project,
        "wake_id": wake["wake_id"],
        "wake_status": wake.get("status"),
        "assignment_id": assignment.assignment_id,
        "runtime": runtime_name,
        "provider": provider,
        "execution_mode": CONNECT_WAKE_MODE,
        "capacity": capacity,
        "scope": wake.get("scope"),
    }
