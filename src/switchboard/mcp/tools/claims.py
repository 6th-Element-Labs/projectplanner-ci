"""Claim-focused MCP tools.

Transport adapter for claim_task / claim_next / complete_claim. Authentication,
identity binding, and MCP serialization remain edge concerns; the shared
application commands used by REST own transport-neutral validation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
import store
from switchboard.application.commands import claim_next as claim_next_command
from switchboard.application.commands import claim_task as claim_task_command
from switchboard.application.commands import complete_claim as complete_claim_command
from switchboard.application.commands import executed_test_runs as executed_test_runs_command
from switchboard.application.pr_ready import PullRequestReadyGateway


@dataclass(frozen=True)
class ClaimToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]
    resolve_write_actor: Callable[..., dict[str, Any]]
    write_binding_comment: Callable[..., None]
    ensure_pr_ready: PullRequestReadyGateway


_SERVICES: ClaimToolServices | None = None


def _services() -> ClaimToolServices:
    if _SERVICES is None:
        raise RuntimeError("claim MCP tools must be registered before use")
    return _SERVICES


def claim_next(agent_id: str, ctx: Context, lanes: str = "", capabilities: str = "",
               max_risk: str = "", max_budget_usd: float = 0.0,
               ttl_seconds: int = 1800, idem_key: str = "",
               override_identity_risk: bool = False,
               work_session_id: str = "", work_session_json: str = "",
               session_policy_profile: str = "",
               require_work_session: bool = False,
               project: str = "maxwell", deliverable_id: str = "",
               board_id: str = "", mission_id: str = "",
               milestone_id: str = "") -> str:
    """Atomically claim the next unblocked task for this agent. This is the first +TXP
    scheduler primitive: dependency-aware, idempotent, constraint-scored, and returns
    dispatch_reason plus budget/model guidance.

    When deliverable_id or board_id/mission_id is set, only tasks linked to that mission
    deliverable are eligible. milestone_id optionally narrows to one milestone."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(claim_next_command.execute_mapping_result(
        {
            "agent_id": agent_id,
            "lanes": lanes,
            "capabilities": capabilities,
            "max_risk": max_risk,
            "max_budget_usd": max_budget_usd,
            "ttl_seconds": ttl_seconds,
            "idem_key": idem_key,
            "override_identity_risk": override_identity_risk,
            "work_session_id": work_session_id,
            "work_session_json": work_session_json,
            "session_policy_profile": session_policy_profile,
            "require_work_session": require_work_session,
            "project": project,
            "deliverable_id": deliverable_id,
            "board_id": board_id,
            "mission_id": mission_id,
            "milestone_id": milestone_id,
        },
        actor=auth.actor(principal),
        principal_id=principal["id"],
    ))


def claim_task(task_id: str, agent_id: str, ctx: Context,
               ttl_seconds: int = 1800, idem_key: str = "",
               override_identity_risk: bool = False,
               work_session_id: str = "", work_session_json: str = "",
               session_policy_profile: str = "",
               require_work_session: bool = False,
               project: str = "maxwell") -> str:
    """Atomically claim one exact ready, unblocked task.

    Use this when a human/operator has selected a specific task. Unlike claim_next,
    this never substitutes a different scheduler-preferred task.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(claim_task_command.execute_mapping_result(
        {
            "task_id": task_id,
            "agent_id": agent_id,
            "ttl_seconds": ttl_seconds,
            "idem_key": idem_key,
            "override_identity_risk": override_identity_risk,
            "work_session_id": work_session_id,
            "work_session_json": work_session_json,
            "session_policy_profile": session_policy_profile,
            "require_work_session": require_work_session,
            "project": project,
        },
        actor=auth.actor(principal),
        principal_id=principal["id"],
    ))


def complete_claim(claim_id: str, ctx: Context, evidence: str = "", final_status: str = "",
                   project: str = "maxwell", agent_id: str = "",
                   system_actor: str = "", system_reason: str = "",
                   mission_project: str = "") -> str:
    """Mark a task claim completed, release its task lease, and record completion evidence.

    This moves the task to In Review. Done is reserved for GitHub/default-branch merge
    provenance; if final_status='Done' is passed, Switchboard records the request but keeps
    the task In Review until merged_sha/default-branch SHA is stamped. evidence should be
    a JSON object string with branch, head_sha, pr_url/pr_number, or a verification note.
    Optional deliverable_id, milestone_id, and mission_project in evidence refresh mission
    progress without counting agent completion as Done."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    target = store.claim_binding_target(claim_id, project=project)
    binding = services.resolve_write_actor(
        principal,
        project=project,
        task_id=target.get("task_id") or "",
        agent_id=agent_id or target.get("agent_id") or "",
        system_actor=system_actor,
        system_reason=system_reason,
    )
    if not binding.get("ok"):
        return services.dumps(binding)
    services.write_binding_comment(target.get("task_id") or "", binding, project)
    return services.dumps(complete_claim_command.execute_mapping_result(
        {
            "claim_id": claim_id,
            "evidence": evidence,
            "final_status": final_status,
            "project": project,
            "mission_project": mission_project,
        },
        actor=binding["actor"],
        ensure_ready=services.ensure_pr_ready,
    ))


def record_executed_test_run(test_run_json: str, ctx: Context,
                             project: str = "maxwell") -> str:
    """Record one passing executed test run as typed completion evidence (COORD-62).

    test_run_json follows switchboard.executed_test_run.record_command.v1: task_id,
    work_session_id, commands (list, min 1), passed and/or exit_code, output_sha256
    (64-hex over the combined run output), optional branch/head_sha cross-checks.
    completed_at is stamped server-side. One call writes work_session.hygiene AND
    claim evidence atomically and returns the executed-test gate's verdict, so a
    runner learns immediately whether completion/merge authorization accept it.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        payload = json.loads(test_run_json or "{}")
    except json.JSONDecodeError:
        return services.dumps({"error": "test_run_json must be valid JSON"})
    if not isinstance(payload, dict):
        return services.dumps({"error": "test_run_json must be a JSON object"})
    task_id = str(payload.get("task_id") or "").strip()
    binding = services.resolve_write_actor(
        principal, project=project, task_id=task_id, agent_id="")
    if not binding.get("ok"):
        return services.dumps(binding)
    result = executed_test_runs_command.execute_mapping(
        payload, actor=binding["actor"], principal_id=principal.get("id") or "",
        project=project,
    )
    if result.get("recorded"):
        services.write_binding_comment(task_id, binding, project)
    return services.dumps(result)


def verify_offline_completion(task_id: str, ctx: Context, evidence: str = "",
                              artifact_url: str = "", evidence_hash: str = "",
                              verifier: str = "", reviewed_at: float = 0,
                              project: str = "maxwell") -> str:
    """Mark an In Review non-PR/offline task Done with verifier-stamped evidence.

    Agents still use complete_claim(...) to move work to In Review. This tool is the
    privileged verifier/operator path for work that has no code PR: it records
    provenance_type=offline_evidence, evidence/artifact/hash/verifier/reviewed_at, and
    then marks Done. It fails closed unless the task is already In Review and evidence
    is supplied.
    """
    services = _services()
    principal = services.require_write(ctx, project)
    return services.dumps(store.mark_task_offline_done(
        task_id, evidence=evidence, artifact_url=artifact_url,
        evidence_hash=evidence_hash, verifier=verifier or auth.actor(principal),
        reviewed_at=reviewed_at or None, actor=auth.actor(principal), project=project))



def record_human_blocker(blocker_json: str, ctx: Context,
                         project: str = "maxwell") -> str:
    """Record a sticky agent-authored human-capacity blocker (COORD-69).

    Prefer ``agent_requires_human`` — same receipt, Mission Bot canonical name.
    blocker_json: task_id, work_session_id, reason, plus closeout fields
    (completed_work, minimum_human_action, resume_condition,
    next_automatic_step, evidence). Atomically marks the Work Session blocked,
    sets the board Blocked, creates one PROTO-7 Needs-you item, and fences the
    bound runner when possible. Only agents may author this; orchestration code
    must never synthesize it.
    """
    return agent_requires_human(blocker_json, ctx, project=project)


def agent_requires_human(blocker_json: str, ctx: Context,
                         project: str = "maxwell") -> str:
    """Agent-authored sticky receipt: Mission Bot must stop and surface this.

    GitHub/Switchboard machine facts never create this flag. Only the active LLM
    agent may call this MCP tool. Bound to task/work session; sticky until a
    human resolves the attention request.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        payload = json.loads(blocker_json or "{}")
    except json.JSONDecodeError:
        return services.dumps({"error": "blocker_json must be valid JSON"})
    if not isinstance(payload, dict):
        return services.dumps({"error": "blocker_json must be a JSON object"})
    payload = dict(payload)
    # Client-supplied provenance is discarded before the server stamp.
    for forged in (
        "actor", "agent_id", "principal_id", "binding", "principal_kind",
        "provenance_stamp",
    ):
        payload.pop(forged, None)
    payload["source_tool"] = "agent_requires_human"
    task_id = str(payload.get("task_id") or "").strip()
    # Prefer authenticated principal agent identity over an empty hint so
    # resolve_write_actor can mint registered_agent / direct_session bindings.
    agent_hint = str(
        principal.get("bound_agent_id")
        or (
            auth.actor(principal)
            if str(principal.get("kind") or "").lower()
            in {"agent", "direct_session"}
            else ""
        )
        or ""
    )
    binding = services.resolve_write_actor(
        principal, project=project, task_id=task_id, agent_id=agent_hint,
    )
    if not binding.get("ok"):
        return services.dumps(binding)
    from switchboard.domain.mission_bot.facts import AGENT_PROVENANCE_BINDINGS
    stamped_binding = str(binding.get("binding") or "")
    if stamped_binding not in AGENT_PROVENANCE_BINDINGS:
        return services.dumps({
            "recorded": False,
            "error": "agent_binding_required",
            "error_code": "agent_binding_required",
            "failure_class": "failed_gate",
            "binding": stamped_binding or None,
            "message": (
                "agent_requires_human requires a registered agent or "
                "direct-session principal; principal/system actors cannot "
                "author this receipt."
            ),
        })
    # Server-stamped provenance only.
    payload["actor"] = str(binding.get("actor") or "")
    payload["agent_id"] = str(binding.get("agent_id") or "")
    payload["principal_id"] = str(binding.get("principal_id") or "")
    payload["binding"] = stamped_binding
    payload["provenance_stamp"] = "switchboard.resolve_write_actor.v1"
    from switchboard.application.commands import human_blocker as human_blocker_cmd
    result = human_blocker_cmd.execute_mapping(
        payload, actor=binding["actor"], project=project,
    )
    if result.get("recorded"):
        services.write_binding_comment(task_id, binding, project)
    return services.dumps(result)


def abandon_claim(claim_id: str, reason: str, ctx: Context,
                  project: str = "maxwell") -> str:
    """Abandon a task claim, release its task lease, and return the task to the ready queue.

    When the claim's Work Session is blocked with route=human (COORD-69), the
    board stays Blocked — abandon does not wipe sticky human closeout.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(store.abandon_claim(claim_id, reason=reason,
                                     actor=auth.actor(principal), project=project))



def revoke_claim(claim_id: str, reason: str, ctx: Context,
                 project: str = "maxwell", reassign_to: str = "",
                 sort_order: int = 0, partial_evidence: str = "",
                 notify: bool = True, ack_deadline_minutes: float = 5) -> str:
    """Operator override for a live claim.

    Revokes the active task claim, releases its task lease, requeues the task,
    optionally redirects/reprioritizes it, preserves partial evidence, and sends
    the displaced agent an ack-required claim_revoked message.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(store.revoke_claim(
        claim_id,
        reason=reason,
        reassign_to=reassign_to,
        sort_order=sort_order if sort_order > 0 else None,
        partial_evidence=partial_evidence,
        notify=notify,
        ack_deadline_minutes=ack_deadline_minutes,
        actor=auth.actor(principal),
        project=project,
    ))



CLAIM_TOOL_NAMES = (
    "claim_next",
    "claim_task",
    "complete_claim",
    "record_executed_test_run",
    "record_human_blocker",
    "agent_requires_human",
    "verify_offline_completion",
    "abandon_claim",
    "revoke_claim",
)


def register_claim_tools(mcp: Any, services: ClaimToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the claim tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in CLAIM_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
