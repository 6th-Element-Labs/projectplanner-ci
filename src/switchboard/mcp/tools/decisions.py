"""Decision-log MCP tools (ADR-lite + COORD-3 explainable coordinator trail)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
import store


@dataclass(frozen=True)
class DecisionToolServices:
    """Host services needed by the decision-log adapter."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: DecisionToolServices | None = None


def _services() -> DecisionToolServices:
    if _SERVICES is None:
        raise RuntimeError("decision MCP tools must be registered before use")
    return _SERVICES


def record_decision(title: str, context: str, decision: str, rationale: str,
                    author: str, ctx: Context, project: str = "maxwell",
                    task_id: str = "", supersedes: int = 0) -> str:
    """Append an immutable architectural decision record so all agents share settled
    conclusions without re-litigating them. Use this when you've just chosen an
    approach — especially a non-obvious one that another agent might reverse.
    Examples: choosing a library, fixing an interface contract, deciding NOT to do X.

    Fields:
      title:     short (≤80 chars) — 'Use advisory leases, not hard file locks'
      context:   why the decision was needed (1-3 sentences)
      decision:  exactly what was decided (1-3 sentences, present tense)
      rationale: why this option was chosen over the alternatives
      author:    your agent-session id (e.g. 'claude/ENGINE-11')
      task_id:   related task (optional)
      supersedes: id of an earlier decision this replaces (optional, set to 0 if none)

    Decisions are append-only. To reverse one, record a new decision with the old id
    in 'supersedes' — the old record is marked 'superseded' automatically.
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
    principal = _services().require_write(ctx, project, ("write:ixp",))
    return _services().dumps(store.record_decision(
        task_id=task_id or None, author=author or auth.actor(principal), title=title,
        context=context, decision=decision, rationale=rationale,
        supersedes=supersedes or None, project=project,
    ))


def record_coordinator_decision(title: str, policy_rule: str,
                                chosen_action_json: str, ctx: Context,
                                inputs_snapshot_json: str = "{}",
                                skipped_alternatives_json: str = "[]",
                                result_json: str = "{}",
                                author: str = "", project: str = "maxwell",
                                task_id: str = "", deliverable_id: str = "",
                                coordinator_agent_id: str = "",
                                decision_kind: str = "recommendation",
                                stable_key: str = "", context: str = "",
                                rationale: str = "") -> str:
    """Persist one explainable coordinator recommendation/action (COORD-3).

    Required structured fields: policy_rule, chosen_action_json (object), plus optional
    inputs_snapshot_json / skipped_alternatives_json / result_json. Returns a stable
    decision_id (coorddec-…) that is idempotent under the same inputs+rule+action
    (or an explicit stable_key). Cockpit/UI reads these via list_coordinator_decisions
    without needing chat transcripts.
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
    principal = _services().require_write(ctx, project, ("write:ixp",))
    try:
        chosen = json.loads(chosen_action_json or "{}")
        inputs = json.loads(inputs_snapshot_json or "{}")
        skipped = json.loads(skipped_alternatives_json or "[]")
        outcome = json.loads(result_json or "{}")
    except json.JSONDecodeError as exc:
        return _services().dumps({"error": "invalid_json", "message": str(exc)})
    return _services().dumps(store.record_coordinator_decision(
        author=author or auth.actor(principal),
        title=title,
        inputs_snapshot=inputs,
        policy_rule=policy_rule,
        chosen_action=chosen,
        skipped_alternatives=skipped,
        result=outcome,
        project=project,
        task_id=task_id,
        deliverable_id=deliverable_id,
        coordinator_agent_id=coordinator_agent_id or author or auth.actor(principal),
        decision_kind=decision_kind,
        stable_key=stable_key,
        context=context,
        rationale=rationale,
    ))


def list_decisions(project: str = "maxwell", task_id: str = "",
                   status: str = "", deliverable_id: str = "",
                   decision_kind: str = "", limit: int = 0) -> str:
    """List architectural decisions recorded by any agent.
    Filter by task_id (decisions about that task) and/or status ('accepted',
    'superseded', 'proposed'). Optional deliverable_id / decision_kind filters
    surface coordinator trail entries. Returns newest-first with structured
    coordinator fields decoded when present.
    Check this at session start to know what's already been decided before
    choosing an approach. project: 'maxwell' (default), 'helm', or 'switchboard'."""
    return _services().dumps(store.list_decisions(
        task_id=task_id or None, status=status, project=project,
        deliverable_id=deliverable_id, decision_kind=decision_kind, limit=limit,
    ))


def list_coordinator_decisions(project: str = "maxwell", task_id: str = "",
                               deliverable_id: str = "", decision_kind: str = "",
                               limit: int = 100) -> str:
    """List explainable coordinator decisions (COORD-3 trail) newest-first.

    Only returns keyed coordinator records (stable decision_id, inputs_snapshot,
    policy_rule, chosen_action, skipped_alternatives, result). Use this for the
    cockpit trail without reading chat. project: 'maxwell', 'helm', or 'switchboard'."""
    return _services().dumps(store.list_coordinator_decisions(
        task_id=task_id, deliverable_id=deliverable_id,
        decision_kind=decision_kind, limit=limit, project=project,
    ))


def get_decision(decision_id: str, project: str = "maxwell") -> str:
    """Fetch a single decision by integer id or stable decision_id (coorddec-…).
    Use when list_decisions / list_coordinator_decisions refers to a record you
    want in full (context + rationale + structured fields).
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
    raw = (decision_id or "").strip()
    key = int(raw) if raw.isdigit() else raw
    record = store.get_decision(key, project=project)
    return _services().dumps(record) if record else "decision not found"


DECISION_TOOL_NAMES = (
    "record_decision",
    "record_coordinator_decision",
    "list_decisions",
    "list_coordinator_decisions",
    "get_decision",
)


def register_decision_tools(mcp: Any,
                            services: DecisionToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the decision-log tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in DECISION_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
