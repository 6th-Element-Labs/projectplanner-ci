"""RAG doc-search and plan-agent MCP tools (ARCH-MS-70).

Transport adapter extracted from ``mcp_server_impl``. Authentication and MCP
serialization remain edge concerns; persistence/search stay behind ``store``/``rag``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import project_contract as project_contract_service
import rag
import store
from switchboard.mcp.authorization import require_current_access
from switchboard.storage.repositories import ai_admission


@dataclass(frozen=True)
class PlanToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]


_SERVICES: PlanToolServices | None = None


def _services() -> PlanToolServices:
    if _SERVICES is None:
        raise RuntimeError("plan MCP tools must be registered before use")
    return _SERVICES


def doc_search(query: str, project: str = "maxwell") -> str:
    """Search the selected project's segmented corpus and return cited snippets: [{file, text}]."""
    services = _services()
    hits = rag.search(query, top_k=5, project=project)
    return services.dumps([{"file": h["file"], "text": h["text"]} for h in hits]) if hits else "no matches"


def get_working_agreement(project: str = "maxwell", if_none_match: str = "") -> str:
    """Connect-time policy for agents: definition of done, branch convention, merge strategy,
    canonical main SHA, and the session-start sequence. Call before register_agent.

    Repeat calls in the same worker session return ``unchanged: true`` plus a digest.
    Pass ``if_none_match`` with the prior ``cache.digest`` to skip the fat body.
    """
    from switchboard.application.queries.working_agreement import execute
    from switchboard.mcp.authorization import current_transport_principal
    from switchboard.mcp.handshake_cache import cache_for_process
    services = _services()
    principal = current_transport_principal() or {}
    return cache_for_process().wrap(
        "working_agreement",
        project,
        execute(project=project),
        principal_id=str(principal.get("id") or ""),
        if_none_match=if_none_match,
        dumps=services.dumps,
    )


def ask_plan(question: str, project: str = "maxwell") -> str:
    """Queue a project-native plan-agent run and return immediately.

    Poll get_background_job_run(project, run_id) until status is completed or failed.
    The completed step result contains answer, sources, and confirmable task proposals.
    """
    services = _services()
    selected = project_contract_service.resolve_project_input(project) or store.DEFAULT_PROJECT
    if not store.has_project(selected):
        return services.dumps({"error": "unknown_project", "project": project})
    principal = require_current_access(selected, ("use:llm",))
    authorization = ai_admission.authorization_snapshot(principal)
    try:
        decision = ai_admission.admit(
            project=selected, surface="mcp_ask_plan", authorization=authorization,
            question=question)
        run = store.enqueue_background_job(
            project=selected, job_name="plan_agent_run",
            params={"question": question, "history": [], "record_chat": False,
                    "ai_admission_id": decision.admission_id,
                    "ai_authorization": authorization},
            actor=str(principal.get("id") or "mcp/ask_plan"),
            start_worker=decision.status == ai_admission.ACTIVE)
        ai_admission.bind_run(selected, decision.admission_id, run["run_id"])
    except ai_admission.AdmissionDenied as exc:
        return services.dumps({"error": "ai_admission_denied",
                               "reason_code": exc.decision.reason_code,
                               "project": selected})
    return services.dumps({
        "run_id": run["run_id"],
        "project": selected,
        "status": "pending",
        "poll_with": "get_background_job_run",
    })


PLAN_TOOL_NAMES = ("doc_search", "get_working_agreement", "ask_plan")


def register_plan_tools(mcp: Any, services: PlanToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the plan/doc-search tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in PLAN_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
