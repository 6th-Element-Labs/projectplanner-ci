from __future__ import annotations

import json

from path_setup import ROOT

from adapters.claude_question_adapter import (
    ClaudeQuestionAdapter, ClaudeQuestionError, hook_settings, pinned_version,
)


BINDING = {
    "project": "switchboard",
    "task_id": "ADAPTER-24",
    "work_session_id": "ws-24",
    "runner_session_id": "run-24",
    "host_id": "host-24",
}


def hook(tool_name="AskUserQuestion", session_id="session-24"):
    return {
        "session_id": session_id,
        "transcript_path": "/redacted/transcript.jsonl",
        "cwd": "/workspace",
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_use_id": "toolu_24",
        "tool_input": {
            "questions": [{
                "question": "Which color?",
                "header": "Color",
                "options": [
                    {"label": "Blue", "description": "Use blue"},
                    {"label": "Green", "description": "Use green"},
                ],
                "multiSelect": False,
            }],
        },
    }


class Attention:
    def __init__(self):
        self.calls = []
        self.decision = None

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if path == "/ixp/v1/attention/requests":
            return {"created": True, "request": {
                "request_id": "attention-24", "version": 1}}
        if path == "/ixp/v1/attention/decisions/claim":
            if self.decision is None:
                return {"claimed": False}
            return {"claimed": True, "delivery": {
                "request": {"request_id": "attention-24", "version": 3},
                "decision": {
                    "decision_id": "decision-24", "choice": self.decision},
            }}
        if path.endswith("/delivery"):
            return {"status": "resolved"}
        raise AssertionError(path)


def test_replayable_ask_queue_decide_delivery_same_session(tmp_path):
    attention = Attention()
    journal = tmp_path / "claude-question.json"
    adapter = ClaudeQuestionAdapter(
        binding=BINDING, http=attention, journal_path=str(journal))

    deferred = adapter.handle_hook(hook())
    assert deferred["hookSpecificOutput"]["permissionDecision"] == "defer"
    posted = attention.calls[0][2]
    assert posted["context"]["provider_native"] == hook()
    assert posted["provider_request_id"] == "session-24:toolu_24"
    assert posted["runner_session_id"] == "run-24"

    reconnected = ClaudeQuestionAdapter(
        binding=BINDING, http=attention, journal_path=str(journal))
    assert reconnected.handle_hook(hook())["hookSpecificOutput"][
        "permissionDecision"] == "defer"
    attention.decision = {"answers": {"Which color?": "Blue"}}
    delivered = reconnected.handle_hook(hook())
    assert delivered["hookSpecificOutput"] == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {
            **hook()["tool_input"],
            "answers": {"Which color?": "Blue"},
        },
    }
    receipts = reconnected.record_continuation({
        "type": "result",
        "session_id": "session-24",
        "stop_reason": "end_turn",
        "terminal_reason": "completed",
    })
    assert receipts == [{
        "schema": "switchboard.claude_question_receipt.v1",
        "provider_request_id": "session-24:toolu_24",
        "session_id": "session-24",
        "tool_use_id": "toolu_24",
        "decision_id": "decision-24",
        "stop_reason": "end_turn",
        "terminal_reason": "completed",
        "same_session_continuation": True,
    }]
    assert json.loads(journal.read_text())["entries"][
        "session-24:toolu_24"]["state"] == "resolved"
    assert journal.stat().st_mode & 0o777 == 0o600


def test_unsupported_request_kinds_and_malformed_answers_fail_closed(tmp_path):
    attention = Attention()
    adapter = ClaudeQuestionAdapter(
        binding=BINDING, http=attention, journal_path=str(tmp_path / "journal"))
    denied = adapter.handle_hook(hook(tool_name="Bash"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "unsupported" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert attention.calls == []

    adapter.handle_hook(hook())
    attention.decision = {"id": "Blue"}
    denied = adapter.handle_hook(hook())
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "answers keyed by exact question text" in denied[
        "hookSpecificOutput"]["permissionDecisionReason"]


def test_cancellation_and_reconnect_are_session_bound(tmp_path):
    attention = Attention()
    adapter = ClaudeQuestionAdapter(
        binding=BINDING, http=attention, journal_path=str(tmp_path / "journal"))
    adapter.handle_hook(hook())
    attention.decision = {"answers": {"Which color?": "Blue"}}
    adapter.handle_hook(hook())
    try:
        adapter.record_continuation({
            "session_id": "different-session", "stop_reason": "cancelled"})
    except ClaudeQuestionError as exc:
        assert "session binding mismatch" in str(exc)
    else:
        raise AssertionError("cross-session completion must fail closed")


def test_exact_probed_version_is_pinned(monkeypatch):
    pinned_version("2.1.202")
    monkeypatch.setenv("PM_CLAUDE_QUESTION_VERSION", "2.1.203")
    try:
        pinned_version("2.1.202")
    except ClaudeQuestionError as exc:
        assert "version mismatch" in str(exc)
    else:
        raise AssertionError("unprobed Claude Code image must fail closed")


def test_settings_scope_hook_to_native_question_tool():
    assert hook_settings(["python3", "/opt/bridge.py"]) == {
        "hooks": {
            "PreToolUse": [{
                "matcher": "AskUserQuestion",
                "hooks": [{
                    "type": "command",
                    "command": "'python3' '/opt/bridge.py'",
                }],
            }],
        },
    }
