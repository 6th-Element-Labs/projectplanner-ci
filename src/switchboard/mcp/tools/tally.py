"""Tally / outcomes / KPI MCP tools.

Transport adapter extracted in ARCH-MS-52. Authentication and MCP serialization
remain edge concerns; persistence stays behind store / application commands.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
import store


@dataclass(frozen=True)
class TallyToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: TallyToolServices | None = None


def _services() -> TallyToolServices:
    if _SERVICES is None:
        raise RuntimeError("tally MCP tools must be registered before use")
    return _SERVICES


def report_usage(ctx: Context, source: str = "agent_report", confidence: str = "reported",
                 task_id: str = "", claim_id: str = "", agent_id: str = "",
                 outcome_id: str = "",
                 runtime: str = "", provider: str = "", model: str = "",
                 prompt_tokens: int = 0, completion_tokens: int = 0,
                 total_tokens: int = 0, cost_usd: float = 0.0,
                 call_site: str = "coding", request_id: str = "",
                 metadata_json: str = "{}", project: str = "maxwell") -> str:
    """Report usage/cost into Tally. Gateway-measured calls should use source='gateway';
    external coding agents should use source='agent_report' and honest confidence."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        metadata = json.loads(metadata_json or "{}")
    except Exception:
        return services.dumps({"error": "metadata_json must be a JSON object string"})
    return services.dumps(store.report_usage(
        source=source, confidence=confidence, task_id=task_id or None,
        claim_id=claim_id or None, outcome_id=outcome_id or None,
        agent_id=agent_id or None,
        principal_id=principal["id"], runtime=runtime, call_site=call_site,
        provider=provider, model=model, prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens or None, cost_usd=cost_usd,
        metadata=metadata, request_id=request_id or None, project=project))



def record_outcome(ctx: Context, outcome_type: str, title: str,
                   task_id: str = "", claim_id: str = "", epic_id: str = "",
                   status: str = "proposed", verifier: str = "",
                   verification: str = "", evidence_json: str = "{}",
                   value_json: str = "{}", project: str = "maxwell") -> str:
    """Record an OXP outcome. Proposed outcomes are pending value; only verified outcomes
    count in cost-per-outcome denominators."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        evidence = json.loads(evidence_json or "{}")
        value = json.loads(value_json or "{}")
    except Exception:
        return services.dumps({"error": "evidence_json and value_json must be JSON object strings"})
    return services.dumps(store.record_outcome(
        outcome_type=outcome_type, title=title, task_id=task_id or None,
        claim_id=claim_id or None, epic_id=epic_id or None, status=status,
        verifier=verifier, verification=verification, evidence=evidence,
        value=value, actor=auth.actor(principal), project=project))



def verify_outcome(outcome_id: str, ctx: Context, verifier: str = "",
                   verification: str = "", evidence_json: str = "{}",
                   project: str = "maxwell") -> str:
    """Mark an outcome verified so it enters Tally's denominator."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        evidence = json.loads(evidence_json or "{}")
    except Exception:
        return services.dumps({"error": "evidence_json must be a JSON object string"})
    return services.dumps(store.verify_outcome(
        outcome_id, verifier=verifier or auth.actor(principal),
        verification=verification, evidence=evidence,
        actor=auth.actor(principal), project=project))



def reject_outcome(outcome_id: str, reason: str, ctx: Context,
                   verifier: str = "", project: str = "maxwell") -> str:
    """Reject a proposed outcome. Rejected outcomes remain auditable but never count."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(store.reject_outcome(
        outcome_id, verifier=verifier or auth.actor(principal), reason=reason,
        actor=auth.actor(principal), project=project))



def create_kpi(ctx: Context, name: str, unit: str, direction: str,
               owner: str = "", baseline_value: float = 0.0,
               current_value: float = 0.0, target_value: float = 0.0,
               period: str = "", project: str = "maxwell") -> str:
    """Create a KPI that outcomes can move. direction: increase|decrease|maintain."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(store.create_kpi(
        name=name, unit=unit, direction=direction, owner=owner,
        baseline_value=baseline_value if baseline_value else None,
        current_value=current_value if current_value else None,
        target_value=target_value if target_value else None,
        period=period, actor=auth.actor(principal), project=project))



def update_kpi_value(kpi_id: str, current_value: float, ctx: Context,
                     evidence_json: str = "{}", project: str = "maxwell") -> str:
    """Update the current value for a KPI."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        evidence = json.loads(evidence_json or "{}")
    except Exception:
        return services.dumps({"error": "evidence_json must be a JSON object string"})
    return services.dumps(store.update_kpi_value(
        kpi_id, current_value=current_value, evidence=evidence,
        actor=auth.actor(principal), project=project))



def link_outcome_to_kpi(ctx: Context, outcome_id: str, kpi_id: str,
                        contribution: float = 0.0, contribution_unit: str = "",
                        confidence: str = "directional", rationale: str = "",
                        project: str = "maxwell") -> str:
    """Link a verified or proposed outcome to a KPI with measured|estimated|directional
    confidence. Only verified outcome links count in cost-per-KPI movement."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    return services.dumps(store.link_outcome_to_kpi(
        outcome_id=outcome_id, kpi_id=kpi_id,
        contribution=contribution if contribution else None,
        contribution_unit=contribution_unit, confidence=confidence,
        rationale=rationale, actor=auth.actor(principal), project=project))



def get_task_tally(task_id: str, project: str = "maxwell") -> str:
    """Tally rollup for one task: spend by source, total tokens/cost, and outcome denominator."""
    services = _services()
    return services.dumps(store.task_tally(task_id, project=project))



def get_kpi_tally(kpi_id: str, project: str = "maxwell") -> str:
    """KPI rollup: linked outcomes, spend, verified contribution, and cost per movement unit."""
    services = _services()
    return services.dumps(store.kpi_tally(kpi_id, project=project))



def get_deliverable_tally(deliverable_id: str, project: str = "maxwell") -> str:
    """Deliverable/mission economics: spend, verified outcomes, KPI movement, and proven vs in-review split."""
    services = _services()
    return services.dumps(store.deliverable_tally(deliverable_id, project=project))




TALLY_TOOL_NAMES = ('report_usage', 'record_outcome', 'verify_outcome', 'reject_outcome', 'create_kpi', 'update_kpi_value', 'link_outcome_to_kpi', 'get_task_tally', 'get_kpi_tally', 'get_deliverable_tally')


def register_tally_tools(mcp: Any, services: TallyToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in TALLY_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
