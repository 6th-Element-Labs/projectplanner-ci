from __future__ import annotations

import io
import json

from adapters.codex_app_server import (
    ATTENTION_METHODS, CodexAppServer, _reply, _stable_key,
)


BINDING = {
    "project": "switchboard",
    "task_id": "ADAPTER-23",
    "work_session_id": "ws-23",
    "runner_session_id": "run-23",
    "host_id": "host-23",
}


class FakeProcess:
    def __init__(self, messages):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
        self.stderr = io.StringIO()
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def communicate(self, timeout=None):
        return "", ""


def test_stable_idempotency_is_bound_to_runner_task_and_server_request():
    assert _stable_key(BINDING, 7, "item/tool/requestUserInput") == _stable_key(
        BINDING, 7, "item/tool/requestUserInput")
    assert _stable_key(BINDING, 8, "item/tool/requestUserInput") != _stable_key(
        BINDING, 7, "item/tool/requestUserInput")


def test_all_structured_attention_methods_have_protocol_native_replies():
    assert set(ATTENTION_METHODS) == {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/tool/requestUserInput",
        "item/permissions/requestApproval",
    }
    assert _reply("item/commandExecution/requestApproval", {"id": "accept"}, {}) == {
        "decision": "accept"}
    assert _reply("item/fileChange/requestApproval", {"id": "decline"}, {}) == {
        "decision": "decline"}
    assert _reply("item/tool/requestUserInput", {
        "answers": {"q1": ["blue"]},
    }, {"questions": [{"id": "q1"}]}) == {
        "answers": {"q1": {"answers": ["blue"]}}}
    assert _reply("item/permissions/requestApproval", {
        "id": "accept", "permissions": {"network": {"enabled": True}},
    }, {})["permissions"]["network"]["enabled"] is True


def test_duplicate_server_request_posts_once_and_delivers_recorded_decision(tmp_path):
    calls = []
    stored = {}

    def http(method, path, body):
        calls.append((method, path, body))
        if path == "/ixp/v1/attention/requests":
            if not stored:
                stored.update(body)
                return {"created": True, "request": {
                    "request_id": "attention-23", "version": 1}}
            assert body["idempotency_key"] == stored["idempotency_key"]
            return {"created": False, "idempotent_replay": True, "request": {
                "request_id": "attention-23", "version": 1}}
        if path == "/ixp/v1/attention/decisions/claim":
            return {"claimed": True, "delivery": {
                "request": {"request_id": "attention-23", "version": 3},
                "decision": {"decision_id": "decision-23",
                             "choice": {"id": "accept"}},
            }}
        if path.endswith("/delivery"):
            assert body["receipt"]["events"] == [
                "serverRequest/resolved", "item/completed"]
            return {"status": "resolved"}
        raise AssertionError(path)

    process = FakeProcess([])
    bridge = CodexAppServer(
        ["codex", "app-server"], cwd="/tmp", env={}, http=http,
        binding=BINDING, popen=lambda *a, **k: process, poll_interval=0,
        journal_path=str(tmp_path / "journal.json"),
    )
    request = {
        "jsonrpc": "2.0", "id": 44,
        "method": "item/commandExecution/requestApproval",
        "params": {"command": "git status"},
    }
    bridge._attention(process, request)
    reconnected = CodexAppServer(
        ["codex", "app-server"], cwd="/tmp", env={}, http=http,
        binding=BINDING, popen=lambda *a, **k: process, poll_interval=0,
        journal_path=str(tmp_path / "journal.json"),
    )
    reconnected._attention(process, request)
    reconnected._complete_deliveries("turn-23")
    posted = [call for call in calls if call[1] == "/ixp/v1/attention/requests"]
    assert len({call[2]["idempotency_key"] for call in posted}) == 1
    replies = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
    assert replies == [
        {"jsonrpc": "2.0", "id": 44, "result": {"decision": "accept"}},
        {"jsonrpc": "2.0", "id": 44, "result": {"decision": "accept"}},
    ]
    assert reconnected.receipts[-1]["decision_id"] == "decision-23"
