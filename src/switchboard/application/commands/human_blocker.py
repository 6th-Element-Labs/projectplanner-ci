"""Typed human-blocker handoff (COORD-69).

Agents that cannot satisfy a credentialed/provider acceptance gate must exit
through DHCP sticky closeout: Work Session blocked + board Blocked + one
PROTO-7 Needs-you item + fence the live runner. Free-form
``update_work_session(blocked)`` + ``abandon_claim`` previously reset the
board to Not Started (DOGFOOD-17), which Autopilot could redispatch forever.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Mapping, Optional

from switchboard.domain.completion.human_closeout import (
    CLOSEOUT_SCHEMA,
    PROVIDER,
    _human_action,
)
from switchboard.storage.repositories import attention as attention_repo
from switchboard.storage.repositories import work_sessions as work_sessions_repo

#: Principal bindings that may author an agent-side human-blocker receipt.
#: Relocated from the retired Mission Bot v1 ``domain.mission_bot.facts``
#: (SIMPLIFY-30); the provenance rule outlived the reducer that first named it.
AGENT_PROVENANCE_BINDINGS = frozenset({
    "registered_agent",
    "inferred_registered_agent",
    "direct_session",
})

HUMAN_BLOCKER_TOOL = "record_human_blocker"
#: Canonical Mission Bot name for the same agent-authored sticky receipt.
AGENT_REQUIRES_HUMAN_TOOL = "agent_requires_human"
RESULT_SCHEMA = "switchboard.human_blocker.record_result.v1"
BLOCKER_SCHEMA = "switchboard.work_session_human_blocker.v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _error(code: str, message: str, failure_class: str = "invalid_input",
           **details: Any) -> dict[str, Any]:
    return {
        "recorded": False,
        "error": code,
        "error_code": code,
        "message": message,
        "failure_class": failure_class,
        "repair_tool": HUMAN_BLOCKER_TOOL,
        **details,
    }


def _blocker_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _map(data.get("evidence"))
    # Board/DHCP sticky closeout still projects route=human. Mission Bot does
    # not trust that alone — it requires source_tool + resolve_write_actor
    # binding stamped below.
    return {
        "schema": BLOCKER_SCHEMA,
        "route": "human",
        "reason": _text(data.get("reason")),
        "completed_work": _text(data.get("completed_work")),
        "minimum_human_action": _text(data.get("minimum_human_action")),
        "resume_condition": _text(data.get("resume_condition")),
        "next_automatic_step": _text(data.get("next_automatic_step")),
        "evidence": evidence,
        "source_tool": (
            _text(data.get("source_tool"))
            or AGENT_REQUIRES_HUMAN_TOOL
        ),
        # Authenticated agent provenance required by Mission Bot.
        "actor": _text(data.get("actor")),
        "agent_id": _text(data.get("agent_id") or data.get("actor")),
        "principal_id": _text(data.get("principal_id")),
        "binding": _text(data.get("binding")),
        "provenance_stamp": _text(data.get("provenance_stamp")),
        "execution_id": _text(data.get("execution_id")),
        "execution_generation": int(data.get("execution_generation") or 0),
    }


def build_work_session_human_closeout_request(
    *,
    task_id: str,
    work_session_id: str,
    blocker: Mapping[str, Any],
    session: Mapping[str, Any],
    actor: str,
) -> dict[str, Any]:
    """PROTO-7 attention request for a pre-/post-PR human capacity gate."""
    reason = _text(blocker.get("reason")) or "human_blocker"
    action = _human_action(reason)
    choices = action["choices"]
    head_sha = _text(session.get("head_sha") or session.get("base_sha"))
    completed = _text(blocker.get("completed_work")) or (
        f"Agent stopped at gate {reason}."
    )
    resume = _text(blocker.get("resume_condition")) or (
        "Authorized human decision recorded on this attention_request."
    )
    next_step = _text(blocker.get("next_automatic_step")) or (
        "Re-assess the exact current head; do not start a new coding generation "
        "from this decision."
    )
    min_action = _text(blocker.get("minimum_human_action")) or action["prompt"]
    context = {
        "schema": CLOSEOUT_SCHEMA,
        "task_id": task_id.upper(),
        "deliverable_id": "",
        "milestone_id": "",
        "completion_run_id": "",
        "state_version": 0,
        "pr_number": 0,
        "head_sha": head_sha,
        "completed_work_summary": completed,
        "evidence_refs": {
            "work_session": {"work_session_id": work_session_id},
            "runner": {
                "runner_session_id": _text(session.get("runner_session_id")),
                "live": bool(session.get("runner_session_id")),
            },
            "blocker": _map(blocker.get("evidence")),
        },
        "unresolved_gate": reason,
        "reason_code": reason,
        "why_automation_stopped": action["why"],
        "delivery_impact": (
            "Autopilot stays sticky Blocked(route=human) until an authorized "
            "decision supplies the missing authority."
        ),
        "owner": "operator",
        "resume_condition": resume,
        "next_automatic_action": next_step,
        "minimum_human_action": min_action,
        "authority": "attention_request",
        "mirrors_only": ["pr_comment", "cli_closeout", "agent_message"],
        "source_tool": HUMAN_BLOCKER_TOOL,
        "work_session_id": work_session_id,
    }
    digest = hashlib.sha256(json.dumps({
        "task_id": task_id.upper(),
        "work_session_id": work_session_id,
        "reason": reason,
        "route": "human",
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    idem_key = f"human-blocker:v1:{task_id.upper()}:{digest}"
    return {
        "provider": PROVIDER,
        "provider_request_id": f"human-blocker:{idem_key}",
        "schema_version": CLOSEOUT_SCHEMA,
        "prompt": (
            f"{task_id.upper()} needs you: {reason}. {min_action} "
            "Autopilot stays blocked until an authorized decision."
        ),
        "choices": choices,
        "recommended_default": choices[0],
        "idempotency_key": idem_key,
        "task_id": task_id.upper(),
        "host_id": "operator",
        "runner_session_id": _text(session.get("runner_session_id")) or None,
        "work_session_id": work_session_id,
        "context": context,
    }


def _set_task_blocked(task_id: str, *, project: str, actor: str,
                      reason: str) -> None:
    from db.connection import _conn

    now = time.time()
    with _conn(project) as c:
        c.execute(
            "UPDATE tasks SET status='Blocked', updated_at=? WHERE task_id=?",
            (now, task_id.upper()),
        )
        c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            (task_id.upper(), actor, "task.human_blocker",
             json.dumps({
                 "route": "human",
                 "reason": reason,
                 "board_status": "Blocked",
                 "source_tool": HUMAN_BLOCKER_TOOL,
             }, sort_keys=True), now),
        )


def _fence_session_runner(session: Mapping[str, Any], *, project: str,
                          actor: str, reason: str) -> Optional[dict[str, Any]]:
    runner_id = _text(session.get("runner_session_id"))
    if not runner_id:
        env = _map(session.get("env"))
        runner_id = _text(env.get("runner_session_id"))
    if not runner_id:
        return None
    from switchboard.storage.repositories import runner as runner_repo
    if runner_repo.get_runner_session(runner_id, project=project) is None:
        # No physical execution exists to contradict the sticky board state.
        return {
            "fenced": True,
            "already_stopped": True,
            "runner_session_id": runner_id,
        }
    env = _map(session.get("env"))
    # The repository commit belongs to the Work Session; the execution head
    # belongs to the server-owned execution lease.  Implementation assignments
    # may intentionally be headless, so preserve an explicitly present empty
    # execution head instead of replacing it with the Work Session head.
    if "execution_head_sha" in env:
        execution_head_sha = _text(env.get("execution_head_sha")).lower()
    elif "assignment_exact_head_sha" in env:
        execution_head_sha = _text(env.get("assignment_exact_head_sha")).lower()
    else:
        execution_head_sha = _text(session.get("head_sha")).lower()
    identity = {
        "runner_session_id": runner_id,
        "execution_id": _text(
            env.get("execution_id") or session.get("execution_id")),
        "generation": int(
            session.get("execution_generation")
            or env.get("execution_generation") or 0),
        "fence_epoch": int(session.get("lease_epoch") or env.get("lease_epoch") or 0),
        "role": _text(
            session.get("execution_role") or env.get("execution_role")
            or "implementation").lower(),
        "head_sha": execution_head_sha,
    }
    try:
        from switchboard.application.commands import task_execution
        if identity["execution_id"] and identity["generation"] > 0:
            return task_execution.fence_task_generation(
                session.get("task_id"),
                identity,
                project=project,
                actor=actor,
                reason=reason,
            )
    except Exception as exc:  # noqa: BLE001 - rendered as a typed refusal below
        return {
            "fenced": False,
            "error": type(exc).__name__,
            "error_code": _text(getattr(exc, "code", "")),
            "detail": str(exc)[:300],
            **_map(getattr(exc, "details", {})),
        }
    try:
        result = runner_repo.make_runner_lease_due(
            runner_id,
            reason=reason,
            authority="operator",
            actor=actor,
            project=project,
        )
        return {
            **result,
            "fenced": bool(result.get("updated")),
        }
    except Exception as exc:  # noqa: BLE001
        return {"fenced": False, "error": type(exc).__name__, "detail": str(exc)[:300]}


def promote_human_blocker(
    *,
    task_id: str,
    work_session_id: str,
    blocker: Mapping[str, Any],
    actor: str,
    project: str,
    session: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Atomically park a task on sticky Blocked(route=human) + Needs-you."""
    task_id = _text(task_id).upper()
    work_session_id = _text(work_session_id)
    reason = _text(blocker.get("reason"))
    if not task_id or not work_session_id or not reason:
        return _error(
            "invalid_human_blocker",
            "task_id, work_session_id, and reason are required.",
        )
    session = dict(session or work_sessions_repo.get_work_session(
        work_session_id, project=project) or {})
    if not session:
        return _error(
            "work_session_not_found",
            f"No Work Session {work_session_id}.",
            failure_class="missing_data",
            work_session_id=work_session_id,
        )
    session_task = _text(session.get("task_id")).upper()
    if session_task and session_task != task_id:
        return _error(
            "work_session_task_mismatch",
            "Work Session is bound to a different task.",
            failure_class="failed_gate",
            work_session_task_id=session_task,
        )

    # Fence before committing any lifecycle projection. A stale generation must
    # not yield a success receipt, a Needs-you item, or a Blocked board that still
    # has a contradictory live execution/claim.
    fence = _fence_session_runner(
        session, project=project, actor=actor,
        reason=f"human blocker: {reason}",
    )
    if fence is not None and not fence.get("fenced"):
        return _error(
            "human_blocker_fence_refused",
            "The exact execution generation could not be fenced; no human "
            "blocker state was recorded.",
            failure_class="failed_gate",
            task_id=task_id,
            work_session_id=work_session_id,
            fence=fence,
        )

    hygiene = dict(_map(session.get("hygiene")))
    hygiene["blocker"] = dict(blocker)
    # Avoid re-entry: update_work_session watches status=blocked + route=human.
    updated = work_sessions_repo.update_work_session(
        work_session_id,
        {
            "status": "blocked",
            "hygiene": hygiene,
            "_human_blocker_promoting": True,
        },
        actor=actor,
        project=project,
    )
    if updated.get("error"):
        return {
            "recorded": False,
            **updated,
            "repair_tool": HUMAN_BLOCKER_TOOL,
        }
    session = updated.get("work_session") or session

    _set_task_blocked(task_id, project=project, actor=actor, reason=reason)
    request_data = build_work_session_human_closeout_request(
        task_id=task_id,
        work_session_id=work_session_id,
        blocker=blocker,
        session=session,
        actor=actor,
    )

    def write():
        with attention_repo._conn(project) as c:
            return attention_repo.create_attention_request_in(
                c, request_data, actor=actor, project=project,
            )

    attention = attention_repo._write_through(project, write)
    request = _map(attention.get("request"))
    # BUG-250: the v4 pager reads only the mission journal. Without a HUMAN
    # transition the fenced runner's terminal receipt looks like runner loss
    # and the pager relaunches over this open Human request. Missions the v4
    # journal does not manage return mission_not_found and are unaffected.
    from switchboard.application.commands import human_mission_events

    mission = human_mission_events.record_human_requested(
        project=project,
        task_id=task_id,
        request_id=_text(request.get("request_id")),
        reason=reason,
    )
    return {
        "schema": RESULT_SCHEMA,
        "recorded": True,
        "route": "human",
        "task_id": task_id,
        "work_session_id": work_session_id,
        "board_status": "Blocked",
        "reason": reason,
        "mission": mission,
        "attention_request_id": request.get("request_id"),
        "attention": attention,
        "idempotent_replay": bool(attention.get("idempotent_replay")),
        "fence": fence,
        "source_tool": HUMAN_BLOCKER_TOOL,
        "work_session": session,
        "message": (
            f"Sticky Blocked(route=human) recorded for {task_id}; "
            f"Needs-you {request.get('request_id') or 'pending'}."
        ),
    }


def execute_mapping(
    data: Mapping[str, Any], *, actor: str, project: str,
) -> dict[str, Any]:
    """Validate and record one typed human-blocker closeout.

    ``binding`` / ``agent_id`` / ``actor`` must be server-stamped by the MCP
    edge via ``resolve_write_actor``. Client-supplied strings alone are refused.
    """
    data = _map(data)
    # Never invent an agent binding here — only the authenticated write edge
    # may stamp AGENT_PROVENANCE_BINDINGS onto the payload.
    data.setdefault("actor", actor)
    if not _text(data.get("agent_id")):
        data["agent_id"] = _text(data.get("actor"))
    # Lift execution identity from the work session when the caller omitted it.
    if not _text(data.get("execution_id")):
        session = work_sessions_repo.get_work_session(
            _text(data.get("work_session_id")), project=project,
        ) or {}
        env = _map(session.get("env"))
        data.setdefault(
            "execution_id",
            env.get("execution_id") or session.get("execution_id") or "",
        )
        data.setdefault(
            "execution_generation",
            session.get("execution_generation")
            or env.get("execution_generation")
            or 0,
        )
    blocker = _blocker_payload(data)
    if not blocker["reason"]:
        return _error(
            "invalid_human_blocker",
            "reason is required (e.g. provider_acceptance_capacity_missing).",
        )
    if blocker["binding"] not in AGENT_PROVENANCE_BINDINGS:
        return _error(
            "agent_provenance_required",
            "agent_requires_human requires a server-stamped agent or "
            "direct-session binding from resolve_write_actor.",
            failure_class="failed_gate",
            binding=blocker["binding"] or None,
        )
    if not blocker["agent_id"]:
        return _error(
            "agent_provenance_required",
            "agent_requires_human requires a server-stamped agent_id.",
            failure_class="failed_gate",
        )
    # Application/MCP callers that already resolved an agent binding may omit
    # the audit stamp; stamp it here so Mission Bot and storage agree.
    if not blocker["provenance_stamp"]:
        blocker["provenance_stamp"] = "switchboard.resolve_write_actor.v1"
    return promote_human_blocker(
        task_id=_text(data.get("task_id")),
        work_session_id=_text(data.get("work_session_id")),
        blocker=blocker,
        actor=actor,
        project=project,
    )


def maybe_promote_from_work_session_update(
    work_session_id: str,
    payload: Mapping[str, Any],
    *,
    existing: Mapping[str, Any],
    actor: str,
    project: str,
) -> Optional[dict[str, Any]]:
    """If an update is a human-route block, promote into sticky DHCP closeout."""
    payload = _map(payload)
    if payload.get("_human_blocker_promoting") is True:
        return None
    status = _text(payload.get("status")).lower()
    hygiene = payload.get("hygiene")
    if isinstance(hygiene, str):
        try:
            hygiene = json.loads(hygiene)
        except (TypeError, ValueError):
            hygiene = {}
    hygiene = _map(hygiene)
    if not hygiene and status == "blocked":
        hygiene = _map(existing.get("hygiene"))
    blocker = _map(hygiene.get("blocker"))
    if status != "blocked" and _text(blocker.get("route")).lower() != "human":
        return None
    if _text(blocker.get("route")).lower() != "human":
        return None
    if not _text(blocker.get("reason")):
        return None
    # Forgeable payload strings must not promote sticky human closeout.
    stamped = _blocker_payload({**blocker, "reason": blocker.get("reason")})
    if (
        stamped["binding"] not in AGENT_PROVENANCE_BINDINGS
        or not stamped["agent_id"]
    ):
        return None
    if not stamped["provenance_stamp"]:
        stamped["provenance_stamp"] = "switchboard.resolve_write_actor.v1"
    task_id = _text(payload.get("task_id") or existing.get("task_id"))
    return promote_human_blocker(
        task_id=task_id,
        work_session_id=work_session_id,
        blocker=stamped,
        actor=actor,
        project=project,
        session=existing,
    )


def session_has_human_blocker(session: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(session, Mapping):
        return False
    if _text(session.get("status")).lower() != "blocked":
        return False
    blocker = _map(_map(session.get("hygiene")).get("blocker"))
    return _text(blocker.get("route")).lower() in {
        "human", "agent_requires_human",
    }


__all__ = [
    "HUMAN_BLOCKER_TOOL",
    "RESULT_SCHEMA",
    "BLOCKER_SCHEMA",
    "build_work_session_human_closeout_request",
    "execute_mapping",
    "maybe_promote_from_work_session_update",
    "promote_human_blocker",
    "session_has_human_blocker",
]
