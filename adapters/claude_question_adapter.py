"""Claude Code 2.1.202 deferred-question bridge for Switchboard attention.

Claude Code's native ``PreToolUse`` hook is the provider boundary.  The first
hook invocation defers ``AskUserQuestion`` and persists the exact provider
payload.  After an operator decision, resuming the same Claude session invokes
the hook again; the bridge supplies ``updatedInput.answers`` and records a
delivery receipt only after the resumed turn completes.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Callable


PINNED_CLAUDE_CODE_VERSION = "2.1.202"
PROVIDER = "anthropic-claude-code"
SCHEMA_VERSION = "claude-code.pre-tool-use.defer.v1"


class ClaudeQuestionError(RuntimeError):
    """A fail-closed provider or attention-contract violation."""


def _provider_request_id(payload: dict[str, Any]) -> str:
    session_id = str(payload.get("session_id") or "").strip()
    tool_use_id = str(payload.get("tool_use_id") or "").strip()
    if not session_id or not tool_use_id:
        raise ClaudeQuestionError("Claude hook is missing session_id or tool_use_id")
    return f"{session_id}:{tool_use_id}"


def _stable_key(binding: dict[str, str], provider_request_id: str) -> str:
    raw = "\x1f".join((
        binding["project"], binding["task_id"], binding["runner_session_id"],
        provider_request_id,
    ))
    return "claude-question:" + hashlib.sha256(raw.encode()).hexdigest()


def _questions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("hook_event_name") != "PreToolUse":
        raise ClaudeQuestionError("unsupported Claude hook event")
    if payload.get("tool_name") != "AskUserQuestion":
        raise ClaudeQuestionError(
            f"unsupported Claude attention request kind: {payload.get('tool_name')!r}")
    tool_input = payload.get("tool_input")
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
        raise ClaudeQuestionError("AskUserQuestion must contain 1-4 questions")
    for question in questions:
        options = question.get("options") if isinstance(question, dict) else None
        if (not isinstance(question, dict)
                or not str(question.get("question") or "").strip()
                or not isinstance(options, list)
                or not 2 <= len(options) <= 4):
            raise ClaudeQuestionError("Claude question schema is invalid")
    return questions


def _choices(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        for option in question["options"]:
            label = str(option.get("label") or "").strip()
            if not label:
                raise ClaudeQuestionError("Claude question option label is missing")
            choices.append({
                "id": f"{index}:{label}",
                "question": question["question"],
                "label": label,
                "description": option.get("description"),
                "multi_select": bool(question.get("multiSelect")),
            })
    return choices


def _answers(choice: Any, questions: list[dict[str, Any]]) -> dict[str, str]:
    if not isinstance(choice, dict) or not isinstance(choice.get("answers"), dict):
        raise ClaudeQuestionError(
            "Claude decision must contain answers keyed by exact question text")
    supplied = choice["answers"]
    answers: dict[str, str] = {}
    for question in questions:
        text = str(question["question"])
        answer = supplied.get(text)
        if isinstance(answer, list):
            answer = ", ".join(str(value) for value in answer)
        if not isinstance(answer, str) or not answer.strip():
            raise ClaudeQuestionError(f"Claude decision is missing answer for {text!r}")
        answers[text] = answer
    return answers


def _hook_output(decision: str, *, reason: str = "",
                 updated_input: dict[str, Any] | None = None) -> dict[str, Any]:
    specific: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
    }
    if reason:
        specific["permissionDecisionReason"] = reason
    if updated_input is not None:
        specific["updatedInput"] = updated_input
    return {"hookSpecificOutput": specific}


class ClaudeQuestionAdapter:
    """Durable hook handler bound to one host execution."""

    def __init__(
        self, *, binding: dict[str, str],
        http: Callable[[str, str, dict[str, Any]], dict[str, Any]],
        journal_path: str,
    ):
        required = {
            "project", "task_id", "work_session_id", "runner_session_id", "host_id",
        }
        if required - binding.keys() or any(not binding[key] for key in required):
            raise ClaudeQuestionError("Claude adapter binding is incomplete")
        self.binding = dict(binding)
        self.http = http
        self.journal_path = Path(journal_path).resolve()
        self.entries: dict[str, dict[str, Any]] = {}
        if self.journal_path.is_file():
            journal = json.loads(self.journal_path.read_text(encoding="utf-8"))
            if journal.get("binding") != self.binding:
                raise ClaudeQuestionError("Claude question journal binding mismatch")
            self.entries = dict(journal.get("entries") or {})

    def _save(self) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(
            prefix=".claude-question-", dir=self.journal_path.parent)
        temporary = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                json.dump({
                    "schema": "switchboard.claude_question_journal.v1",
                    "binding": self.binding,
                    "entries": self.entries,
                }, target, sort_keys=True, separators=(",", ":"))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.journal_path)
        finally:
            temporary.unlink(missing_ok=True)

    def handle_hook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Capture/defer first invocation or answer the resumed invocation."""
        try:
            questions = _questions(payload)
            provider_request_id = _provider_request_id(payload)
        except ClaudeQuestionError as exc:
            return _hook_output("deny", reason=str(exc))

        entry = self.entries.get(provider_request_id)
        if entry is None:
            key = _stable_key(self.binding, provider_request_id)
            created = self.http("POST", "/ixp/v1/attention/requests", {
                "project": self.binding["project"],
                "provider": PROVIDER,
                "provider_request_id": provider_request_id,
                "schema_version": SCHEMA_VERSION,
                "prompt": "\n".join(str(item["question"]) for item in questions),
                "choices": _choices(questions),
                "idempotency_key": key,
                "host_id": self.binding["host_id"],
                "runner_session_id": self.binding["runner_session_id"],
                "work_session_id": self.binding["work_session_id"],
                "task_id": self.binding["task_id"],
                "context": {
                    "request_kind": "question",
                    "provider_native": payload,
                    "session_id": payload["session_id"],
                    "tool_use_id": payload["tool_use_id"],
                },
            })
            request = created.get("request") or {}
            request_id = str(request.get("request_id") or "")
            if not request_id:
                return _hook_output(
                    "deny", reason="Switchboard did not persist the Claude question")
            entry = {
                "request_id": request_id,
                "provider_request_id": provider_request_id,
                "session_id": payload["session_id"],
                "tool_use_id": payload["tool_use_id"],
                "provider_native": payload,
                "state": "deferred",
            }
            self.entries[provider_request_id] = entry
            self._save()
            return _hook_output("defer")

        if entry.get("state") == "answer_delivered":
            # The hook reply can be lost after the decision was durably claimed
            # (process crash, reconnect, or transport failure).  Never try to
            # claim the one-shot decision again: replay the journaled provider
            # input for the same native session/tool identity.
            updated_input = entry.get("updated_input")
            if not isinstance(updated_input, dict):
                return _hook_output(
                    "deny", reason="Claude delivered-answer journal is incomplete")
            return _hook_output("allow", updated_input=dict(updated_input))

        if entry.get("state") == "resolved":
            return _hook_output(
                "deny", reason="Claude question was already resolved")

        claimed = self.http("POST", "/ixp/v1/attention/decisions/claim", {
            "project": self.binding["project"],
            "host_id": self.binding["host_id"],
            "provider": PROVIDER,
            "request_id": entry["request_id"],
        })
        delivery = claimed.get("delivery") if claimed.get("claimed") else None
        if delivery is None:
            return _hook_output("defer")
        try:
            answers = _answers((delivery.get("decision") or {}).get("choice"), questions)
        except ClaudeQuestionError as exc:
            entry["state"] = "decision_rejected"
            entry["error"] = str(exc)
            self._save()
            return _hook_output("deny", reason=str(exc))
        updated_input = dict(payload["tool_input"])
        updated_input["answers"] = answers
        entry.update({
            "state": "answer_delivered",
            "expected_version": int((delivery.get("request") or {}).get("version") or 3),
            "decision_id": (delivery.get("decision") or {}).get("decision_id"),
            "updated_input": updated_input,
        })
        self._save()
        return _hook_output("allow", updated_input=updated_input)

    def record_continuation(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        """Resolve delivered requests only after the same Claude session continues."""
        session_id = str(result.get("session_id") or "")
        if not session_id:
            raise ClaudeQuestionError("Claude completion is missing session_id")
        receipts: list[dict[str, Any]] = []
        for entry in self.entries.values():
            if entry.get("state") != "answer_delivered":
                continue
            if entry["session_id"] != session_id:
                raise ClaudeQuestionError("Claude continuation session binding mismatch")
            if result.get("stop_reason") == "tool_deferred":
                raise ClaudeQuestionError("Claude question remained deferred after delivery")
            receipt = {
                "schema": "switchboard.claude_question_receipt.v1",
                "provider_request_id": entry["provider_request_id"],
                "session_id": session_id,
                "tool_use_id": entry["tool_use_id"],
                "decision_id": entry["decision_id"],
                "stop_reason": result.get("stop_reason"),
                "terminal_reason": result.get("terminal_reason"),
                "same_session_continuation": True,
            }
            resolved = self.http(
                "POST",
                f"/ixp/v1/attention/requests/{entry['request_id']}/delivery",
                {
                    "project": self.binding["project"],
                    "host_id": self.binding["host_id"],
                    "expected_version": entry["expected_version"],
                    "receipt": receipt,
                },
            )
            if resolved.get("status") != "resolved":
                raise ClaudeQuestionError(
                    "Switchboard did not resolve the Claude question delivery")
            entry["state"] = "resolved"
            entry["receipt"] = receipt
            receipts.append(receipt)
        self._save()
        return receipts


def pinned_version(actual: str) -> None:
    """Fail closed when the runtime differs from the probed Claude Code image."""
    expected = str(
        os.environ.get("PM_CLAUDE_QUESTION_VERSION")
        or PINNED_CLAUDE_CODE_VERSION).strip()
    if actual.strip() != expected:
        raise ClaudeQuestionError(
            f"Claude Code version mismatch: expected {expected}, found {actual.strip() or 'unknown'}")


def pinned_command(executable: str) -> list[str]:
    completed = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=False)
    output = (completed.stdout or completed.stderr or "").strip()
    actual = output.split()[0] if output else ""
    if completed.returncode:
        raise ClaudeQuestionError("Claude Code version probe failed")
    pinned_version(actual)
    return [executable]


def hook_settings(command: list[str]) -> dict[str, Any]:
    """Return the exact Claude settings fragment for the question hook."""
    shell_command = " ".join(_shell_quote(part) for part in command)
    return {
        "hooks": {
            "PreToolUse": [{
                "matcher": "AskUserQuestion",
                "hooks": [{"type": "command", "command": shell_command}],
            }],
        },
    }


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _environment_binding() -> dict[str, str]:
    mapping = {
        "project": "PM_PROJECT",
        "task_id": "PM_TASK_ID",
        "work_session_id": "PM_WORK_SESSION_ID",
        "runner_session_id": "PM_RUNNER_SESSION_ID",
        "host_id": "PM_HOST_ID",
    }
    return {key: str(os.environ.get(env_name) or "").strip()
            for key, env_name in mapping.items()}


def main() -> int:
    """Command-hook entrypoint. It emits only Claude's structured hook reply."""
    try:
        executable = str(
            os.environ.get("PM_CLAUDE_EXECUTABLE") or "claude").strip()
        if not executable:
            raise ClaudeQuestionError("PM_CLAUDE_EXECUTABLE is empty")
        # This check deliberately lives in the command-hook entrypoint.  A
        # caller cannot reach capture, claim, or replay on an unprobed runtime.
        pinned_command(executable)
        try:
            from switchboard_core import _http
        except ModuleNotFoundError:
            from adapters.switchboard_core import _http
        payload = json.load(sys.stdin)
        journal = str(os.environ.get("PM_CLAUDE_QUESTION_JOURNAL") or "").strip()
        if not journal:
            raise ClaudeQuestionError("PM_CLAUDE_QUESTION_JOURNAL is required")
        adapter = ClaudeQuestionAdapter(
            binding=_environment_binding(),
            http=lambda method, path, body: _http(method, path, body),
            journal_path=journal,
        )
        print(json.dumps(adapter.handle_hook(payload), separators=(",", ":")))
        return 0
    except Exception as exc:
        # Hook failures are explicit denials. Never dump environment, HTTP bodies,
        # credentials, or traceback text into Claude's transcript.
        print(json.dumps(_hook_output(
            "deny", reason=f"Claude question bridge failed closed: {type(exc).__name__}"),
            separators=(",", ":")))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
