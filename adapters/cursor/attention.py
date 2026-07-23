"""Fail-closed Cursor attention capability derived from pinned live probes.

Cursor's stream-json channel is useful for lifecycle observation, but the tested
build does not expose a human question or approval request/reply round trip.
This module deliberately refuses to normalize lookalike text and tool events
into Switchboard attention requests.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


CURSOR_PROBED_VERSION = "2026.07.23-e383d2b"
CURSOR_ATTENTION_SCHEMA = "switchboard.cursor_attention_capability.v1"


class CursorAttentionUnsupported(ValueError):
    """Raised when a caller tries to productize an unproven request kind."""

    def __init__(self, request_kind: str, reason: str) -> None:
        self.code = "cursor_attention_unsupported"
        self.request_kind = request_kind
        self.reason = reason
        super().__init__(f"{request_kind}: {reason}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "provider": "cursor",
            "provider_version": CURSOR_PROBED_VERSION,
            "request_kind": self.request_kind,
            "reason": self.reason,
            "visible": True,
        }


@dataclass(frozen=True)
class CursorStreamEvidence:
    session_id: str
    request_id: str
    terminal: bool
    event_count: int
    question_request_count: int
    approval_request_count: int
    elicitation_actions: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CURSOR_ATTENTION_SCHEMA,
            "provider": "cursor",
            "provider_version": CURSOR_PROBED_VERSION,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "terminal": self.terminal,
            "event_count": self.event_count,
            "question_request_count": self.question_request_count,
            "approval_request_count": self.approval_request_count,
            "elicitation_actions": list(self.elicitation_actions),
            "capabilities": {
                "stream_session_binding": bool(self.session_id),
                "stream_completion": self.terminal,
                "human_question_round_trip": False,
                "tool_approval_round_trip": False,
                "mcp_elicitation_human_round_trip": False,
                "same_process_decision_delivery": False,
            },
        }


def _event_session(event: Mapping[str, Any]) -> str:
    return str(event.get("session_id") or "")


def inspect_stream(events: Iterable[Mapping[str, Any]]) -> CursorStreamEvidence:
    """Summarize a captured stream without upgrading lookalikes to requests."""
    captured = [dict(event) for event in events]
    session_ids = {_event_session(event) for event in captured if _event_session(event)}
    if len(session_ids) > 1:
        raise CursorAttentionUnsupported(
            "session_binding", "stream contains more than one Cursor session_id")
    session_id = next(iter(session_ids), "")
    terminal_events = [
        event for event in captured
        if event.get("type") == "result" and event.get("subtype") in {"success", "error"}
    ]
    request_id = str(terminal_events[-1].get("request_id") or "") if terminal_events else ""

    # These counts remain zero until a live probe establishes an exact provider
    # request/reply schema. Assistant text and skipApproval:false are not enough.
    question_requests = [
        event for event in captured
        if event.get("type") == "attention_request"
        and event.get("request_kind") == "question"
    ]
    approval_requests = [
        event for event in captured
        if event.get("type") == "attention_request"
        and event.get("request_kind") == "approval"
    ]
    actions: list[str] = []
    for event in captured:
        if event.get("probe") != "mcp_elicitation_reply":
            continue
        action = str((event.get("native_reply") or {}).get("action") or "")
        if action:
            actions.append(action)
    return CursorStreamEvidence(
        session_id=session_id,
        request_id=request_id,
        terminal=bool(terminal_events),
        event_count=len(captured),
        question_request_count=len(question_requests),
        approval_request_count=len(approval_requests),
        elicitation_actions=tuple(actions),
    )


def normalize_attention_request(
    event: Mapping[str, Any], *, task_id: str, host_id: str,
    runner_session_id: str,
) -> dict[str, Any]:
    """Refuse all Cursor request kinds until a pinned live round trip proves one."""
    del task_id, host_id, runner_session_id
    event_type = str(event.get("type") or "unknown")
    if event_type == "assistant":
        reason = "assistant text is not a provider-native human request"
    elif event_type == "tool_call":
        reason = (
            "tool start/completion has no external decision reply channel; "
            "skipApproval:false did not pause print mode"
        )
    elif event.get("probe") == "mcp_elicitation_reply":
        reason = "Cursor returned action=decline without exposing a human decision"
    else:
        reason = "no pinned Cursor request/reply schema has been proven"
    raise CursorAttentionUnsupported(event_type, reason)


def capability_manifest() -> dict[str, Any]:
    return {
        "schema": CURSOR_ATTENTION_SCHEMA,
        "provider": "cursor",
        "provider_version": CURSOR_PROBED_VERSION,
        "support_status": "unsupported_fail_closed",
        "supported_request_kinds": [],
        "capture": {
            "stream_json_session_id": "proven",
            "stream_json_request_id": "proven",
            "tool_call_started_completed": "proven",
            "human_question_request": "not_exposed",
            "tool_approval_request": "not_exposed",
            "mcp_elicitation_reply": "automatic_decline",
        },
        "reply_mechanism": None,
        "session_binding": "stream session_id only",
        "cancellation": "process interrupt only; no attention cancellation receipt",
        "reconnect": "vendor --resume exists but no pending attention delivery binding",
        "completion": "terminal result event",
    }


__all__ = [
    "CURSOR_ATTENTION_SCHEMA",
    "CURSOR_PROBED_VERSION",
    "CursorAttentionUnsupported",
    "CursorStreamEvidence",
    "capability_manifest",
    "inspect_stream",
    "normalize_attention_request",
]
