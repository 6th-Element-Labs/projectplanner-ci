"""ADAPTER-31: Connect PTY interactive prompts → PROTO-7/8 Needs-you attention."""
from __future__ import annotations

import time

from path_setup import ROOT  # noqa: F401

from adapters.codex.pty_prompt_attention import (
    CodexPtyAttentionBridge,
    PtyAttentionFault,
    detect_prompt,
    pty_reply_bytes,
)


BINDING = {
    "project": "switchboard",
    "task_id": "ADAPTER-31",
    "work_session_id": "ws-31",
    "runner_session_id": "run-31",
    "host_id": "host-31",
}

RATE_LIMIT_DIALOG = """
■ stream disconnected before completion: Internal server error
  Approaching rate limits — Switch to gpt-5.4-mini for lower credit usage?
  › 1. Switch to gpt-5.4-mini  2. Keep current model  3. Keep current model (never show again)
"""


def test_detects_rate_limit_dialog_as_operator_choice():
    match = detect_prompt(RATE_LIMIT_DIALOG)
    assert match is not None
    assert match.kind == "rate_limit_model_switch"
    assert match.prompt.startswith("Approaching rate limits")
    assert [c["id"] for c in match.choices] == ["1", "2", "3"]
    # Never recommend silent downgrade.
    assert match.recommended_default == "2"


def test_ordinary_codex_output_is_not_a_prompt():
    assert detect_prompt("thinking…\nReading adapters/agent_host.py\n") is None


def test_detects_rate_limit_dialog_through_ansi_noise():
    noisy = (
        "\x1b[36mApproaching rate limits\x1b[0m — Switch to gpt-5.4-mini "
        "for lower credit usage?\n"
        "\x1b[1m› 1. Switch to gpt-5.4-mini  2. Keep current model  "
        "3. Keep current model (never show again)\x1b[0m\n"
    )
    match = detect_prompt(noisy)
    assert match is not None
    assert match.kind == "rate_limit_model_switch"


def test_pty_stream_watcher_requires_task_id(monkeypatch, tmp_path):
    from adapters.codex import pty_stream

    monkeypatch.setenv("PM_CODEX_PTY_ATTENTION", "1")
    assert pty_stream._start_pty_attention_watcher(
        master_fd=1,
        write_lock=__import__("threading").Lock(),
        log_path=str(tmp_path / "stdout.log"),
        runner_session_id="run-31",
        host_id="host-31",
        task_id="",
        child_pid=0,
        child_process=None,
    ) is None


def test_supervisor_pty_companion_preserves_log_with_attention_enabled(tmp_path, monkeypatch):
    """Regression: watcher import must not kill pty_stream after ready (CI failure)."""
    import os
    import sys
    import time

    from adapters.codex import supervisor

    monkeypatch.setenv("PM_RUNNER_DIR", str(tmp_path))
    monkeypatch.setenv("PM_RUNNER_USE_PTY", "1")
    monkeypatch.setenv("PM_TASK_ID", "ADAPTER-31")
    monkeypatch.setenv("PM_HOST_ID", "host-31")
    monkeypatch.setenv("PM_CODEX_PTY_ATTENTION", "1")
    marker = f"adapter31-log-{os.getpid()}"
    rec = supervisor.start_session(
        [sys.executable, "-c",
         f"import time; print({marker!r}, flush=True); time.sleep(1.5)"],
        agent_id="agent/adapter-31",
        task_id="ADAPTER-31",
        cwd=os.getcwd(),
        runner_dir=str(tmp_path),
    )
    rid = rec["runner_session_id"]
    deadline = time.time() + 5
    log_text = ""
    while time.time() < deadline:
        log_text = supervisor._tail(tmp_path / rid / "stdout.log")
        if marker in log_text:
            break
        time.sleep(0.1)
    assert marker in log_text, (
        f"expected child output in PTY log; got {log_text!r}; "
        f"stderr={(tmp_path / rid / 'pty_stream.stderr.log').read_text()[-1500:]!r}"
    )
    supervisor.kill_session(rid, runner_dir=str(tmp_path))


def test_watcher_feeds_log_chunk_into_bridge_once(tmp_path):
    from adapters.codex.pty_prompt_attention import PtyPromptWatcher

    written = []
    calls = []

    def http(method, path, body):
        calls.append((method, path, body))
        if path == "/ixp/v1/attention/requests":
            return {"created": True, "request": {
                "request_id": "attention-watch", "version": 1}}
        if path == "/ixp/v1/attention/decisions/claim":
            return {"claimed": True, "delivery": {
                "request": {"request_id": "attention-watch", "version": 3},
                "decision": {"decision_id": "d-watch", "choice": {"id": "2"}},
            }}
        if path.endswith("/delivery"):
            return {"status": "resolved"}
        raise AssertionError(path)

    bridge = CodexPtyAttentionBridge(
        http=http, binding=BINDING, write_pty=written.append,
        poll_interval=0, journal_path=str(tmp_path / "w.json"),
    )
    watcher = PtyPromptWatcher(bridge=bridge)
    watcher.feed(RATE_LIMIT_DIALOG.encode())
    watcher.feed(RATE_LIMIT_DIALOG.encode())  # same prompt still on screen
    assert written == [b"2\r"]
    assert len([c for c in calls if c[1] == "/ixp/v1/attention/requests"]) == 1


def test_pty_reply_maps_choice_to_numbered_keystroke():
    assert pty_reply_bytes({"id": "2"}) == b"2\r"
    assert pty_reply_bytes({"id": "1"}) == b"1\r"


def test_bridge_posts_one_attention_request_and_writes_pty_answer(tmp_path):
    calls = []
    written = []
    stored = {}

    def http(method, path, body):
        calls.append((method, path, body))
        if path == "/ixp/v1/attention/requests":
            if not stored:
                stored.update(body)
                return {"created": True, "request": {
                    "request_id": "attention-31", "version": 1}}
            assert body["idempotency_key"] == stored["idempotency_key"]
            return {"created": False, "idempotent_replay": True, "request": {
                "request_id": "attention-31", "version": 1}}
        if path == "/ixp/v1/attention/decisions/claim":
            return {"claimed": True, "delivery": {
                "request": {"request_id": "attention-31", "version": 3},
                "decision": {"decision_id": "decision-31",
                             "choice": {"id": "2"}},
            }}
        if path.endswith("/delivery"):
            return {"status": "resolved"}
        raise AssertionError(path)

    bridge = CodexPtyAttentionBridge(
        http=http,
        binding=BINDING,
        write_pty=written.append,
        poll_interval=0,
        journal_path=str(tmp_path / "journal.json"),
    )
    match = detect_prompt(RATE_LIMIT_DIALOG)
    bridge.handle_match(match)
    # Replay must not re-post or re-write.
    bridge.handle_match(match)
    bridge.complete_delivery()

    posted = [c for c in calls if c[1] == "/ixp/v1/attention/requests"]
    # Journaled idempotency: second handle_match is a no-op (no second POST).
    assert len(posted) == 1
    assert posted[0][2]["provider"] == "openai-codex-pty"
    assert posted[0][2]["task_id"] == "ADAPTER-31"
    assert posted[0][2]["runner_session_id"] == "run-31"
    assert posted[0][2].get("auto_proceed") in (None, False)
    assert written == [b"2\r"]


def test_unanswered_expiry_faults_without_writing_pty_or_auto_switch(tmp_path):
    calls = []

    def http(method, path, body):
        calls.append((method, path, body))
        if path == "/ixp/v1/attention/requests":
            return {"created": True, "request": {
                "request_id": "attention-expired", "version": 1,
                "expires_at": time.time() - 1,
                "status": "expired",
            }}
        if path == "/ixp/v1/attention/decisions/claim":
            return {
                "claimed": False,
                "error": "attention_request_expired",
                "request": {"request_id": "attention-expired", "status": "expired"},
            }
        raise AssertionError(path)

    written = []
    bridge = CodexPtyAttentionBridge(
        http=http,
        binding=BINDING,
        write_pty=written.append,
        poll_interval=0,
        journal_path=str(tmp_path / "journal-exp.json"),
        claim_deadline_s=0.01,
    )
    match = detect_prompt(RATE_LIMIT_DIALOG)
    try:
        bridge.handle_match(match)
        raised = False
    except PtyAttentionFault as exc:
        raised = True
        assert "expired" in str(exc).lower() or "unanswered" in str(exc).lower()
    assert raised
    assert written == []
    assert not any(
        c[2].get("choice", {}).get("id") == "1"
        for c in calls if isinstance(c[2], dict)
    )
