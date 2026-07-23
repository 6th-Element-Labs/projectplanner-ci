from __future__ import annotations

import json
import os
import subprocess
import sys

from path_setup import ROOT

from adapters.claude_question_adapter import (
    ClaudeQuestionAdapter, ClaudeQuestionError, hook_settings, pinned_command,
    pinned_version, resume_question,
)
from adapters import claude_question_adapter as question_adapter


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
    claim_count = sum(
        path == "/ixp/v1/attention/decisions/claim"
        for _, path, _ in attention.calls)
    after_lost_output = ClaudeQuestionAdapter(
        binding=BINDING, http=attention, journal_path=str(journal))
    assert after_lost_output.handle_hook(hook()) == delivered
    assert sum(
        path == "/ixp/v1/attention/decisions/claim"
        for _, path, _ in attention.calls) == claim_count
    receipts = after_lost_output.record_continuation({
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


def test_hook_command_denies_unpinned_runtime_before_http(tmp_path):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/bin/sh\nprintf '2.1.203 (Claude Code)\\n'\n")
    fake_claude.chmod(0o755)
    env = {
        **os.environ,
        "PM_CLAUDE_EXECUTABLE": str(fake_claude),
        "PM_CLAUDE_QUESTION_JOURNAL": str(tmp_path / "journal"),
    }
    completed = subprocess.run(
        [sys.executable, str(ROOT / "adapters" / "claude_question_adapter.py")],
        input=json.dumps(hook()), capture_output=True, text=True, env=env,
        check=False,
    )
    assert completed.returncode == 0
    output = json.loads(completed.stdout)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == (
        "Claude question bridge failed closed: ClaudeQuestionError")
    assert not (tmp_path / "journal").exists()


def test_pinned_command_accepts_exact_runtime(tmp_path):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/bin/sh\nprintf '2.1.202 (Claude Code)\\n'\n")
    fake_claude.chmod(0o755)
    assert pinned_command(str(fake_claude)) == [str(fake_claude)]


def test_agent_host_resume_records_receipt_and_redacts_provider_output(
        tmp_path, monkeypatch):
    attention = Attention()
    adapter = ClaudeQuestionAdapter(
        binding=BINDING, http=attention, journal_path=str(tmp_path / "journal"))
    adapter.handle_hook(hook())
    attention.decision = {"answers": {"Which color?": "Blue"}}
    adapter.handle_hook(hook())
    monkeypatch.setattr(
        question_adapter, "pinned_command", lambda executable: [executable])
    provider_output = json.dumps({
        "type": "result",
        "session_id": "session-24",
        "stop_reason": "end_turn",
        "terminal_reason": "completed",
        "result": "provider output must remain redacted",
    })
    resumed = resume_question(
        adapter,
        session_id="session-24",
        run=lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=provider_output, stderr=""),
    )
    assert resumed["receipt_count"] == 1
    assert resumed["provider_output_redacted"] is True
    assert resumed["provider_output_bytes"] == len(provider_output.encode())
    assert "provider output must remain redacted" not in json.dumps(resumed)
    assert attention.calls[-1][1].endswith("/delivery")


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
