"""Cache-stable MCP tool catalog for CLI workers (ADAPTER-59).

Work-session and Connect/direct-session principals receive one fixed pack on
every ``tools/list``. Operators and Cursor keep the full census. The pack does
not rotate by phase: a changing list would break provider prompt cache.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

WORKER_KINDS = frozenset({"work_session", "direct_session"})

# Sorted so the constant itself is stable. tools/list still emits survivors in
# FastMCP registration order so the prefix does not reshuffle between turns.
WORKER_PACK = tuple(sorted({
    "abandon_claim",
    "ack_message",
    "add_comment",
    "agent_requires_human",
    "board_summary",
    "claim_next",
    "claim_task",
    "complete_claim",
    "control_plane_probe",
    "create_work_session",
    "doc_search",
    "explain_task_block",
    "finish_turn",
    "get_execution_transcript",
    "get_lane_delta",
    "get_mission_status",
    "get_project_contract",
    "get_review_verdict",
    "get_task",
    "get_task_execution",
    "get_task_session",
    "get_work_session",
    "get_work_session_health",
    "get_working_agreement",
    "heartbeat",
    "list_projects",
    "list_unacked_messages",
    "list_unblock_requests",
    "merge_gate",
    "pre_tool_check",
    "preflight_work_session",
    "prepare_agent_session",
    "record_executed_test_run",
    "record_human_blocker",
    "record_review_verdict",
    "register_agent",
    "retry_task",
    "search_tasks",
    "set_agent_state",
    "start_task",
    "submit_bug",
    "update_task",
    "update_work_session",
    "verify_ci",
}))
WORKER_PACK_SET = frozenset(WORKER_PACK)


def uses_worker_pack(principal: Optional[Mapping[str, Any]]) -> bool:
    if not principal:
        return False
    return str(principal.get("kind") or "").strip().lower() in WORKER_KINDS


def blocks_tool(tool_name: str, principal: Optional[Mapping[str, Any]]) -> bool:
    if not uses_worker_pack(principal):
        return False
    return str(tool_name or "") not in WORKER_PACK_SET


def filter_tools(tools: Sequence[Any], principal: Optional[Mapping[str, Any]]) -> list[Any]:
    if not uses_worker_pack(principal):
        return list(tools)
    return [tool for tool in tools if getattr(tool, "name", "") in WORKER_PACK_SET]


def install_worker_catalog(mcp: Any) -> None:
    """Filter FastMCP ``tools/list`` using the authenticated transport principal.

    ACCESS hermetic tests stub FastMCP with ``__getattr__`` returning lambdas.
    Those stubs have no ToolManager. Skip wrapping when ``list_tools`` is not a
    real manager method so import-time install does not crash those tests.
    """
    from switchboard.mcp import authorization as mcp_authorization

    manager = getattr(mcp, "_tool_manager", None)
    original = getattr(manager, "list_tools", None) if manager is not None else None
    if not callable(original):
        return

    def list_tools() -> Iterable[Any]:
        return filter_tools(original(), mcp_authorization.current_transport_principal())

    try:
        manager.list_tools = list_tools
    except (AttributeError, TypeError):
        return
