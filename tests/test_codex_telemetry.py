"""Codex token and compaction telemetry stays useful, bound, and sanitized."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

from adapters.codex import supervisor
from switchboard.application.codex_telemetry import (
    CodexRolloutTelemetryCollector,
    CodexTelemetryWriter,
    binding_from_environment,
    sanitize_app_server_message,
    sanitize_rollout_record,
)

BINDING = {
    "project": "maxwell",
    "task_id": "AGENT-26",
    "execution_id": "execlease-agent26",
    "generation": 8,
    "runner_session_id": "run-agent26",
    "host_id": "host/test",
    "profile": "luna-max-long-running",
    "model": "gpt-5.6-luna",
    "reasoning_effort": "max",
    "configured_context_window": 800000,
    "auto_compact_token_limit": 720000,
}


def token_record(timestamp: str = "2026-08-20T08:45:26Z") -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": 900000,
                    "cached_input_tokens": 700000,
                    "output_tokens": 20000,
                    "reasoning_output_tokens": 10000,
                    "total_tokens": 920000,
                },
                "last_token_usage": {
                    "input_tokens": 380000,
                    "cached_input_tokens": 300000,
                    "output_tokens": 1000,
                    "reasoning_output_tokens": 400,
                    "total_tokens": 381000,
                },
                "model_context_window": 760000,
            },
            "rate_limits": {
                "credits": {"balance": "SECRET-BALANCE"},
                "plan_type": "SECRET-PLAN",
            },
        },
    }


def test_binding_reads_only_safe_launch_configuration():
    binding = binding_from_environment(
        runner_session_id="run-agent26",
        task_id="AGENT-26",
        host_id="host/test",
        command=[
            "codex", "-c", 'model="gpt-5.6-luna"',
            "-c", 'model_reasoning_effort="max"',
            "-c", "model_context_window=800000",
            "-c", "model_auto_compact_token_limit=720000",
            "-c", 'mcp_servers.secret.bearer_token="DO-NOT-CAPTURE"',
        ],
        environment={
            "SWITCHBOARD_TELEMETRY_PROJECT": "maxwell",
            "SWITCHBOARD_EXECUTION_ASSIGNMENT_JSON": json.dumps({
                "execution_id": "execlease-agent26",
                "generation": 8,
                "context_profile": "luna-max-long-running",
            }),
        },
    )
    assert binding == BINDING
    assert "DO-NOT-CAPTURE" not in json.dumps(binding)


def test_rollout_token_event_excludes_account_and_model_content():
    event = sanitize_rollout_record(token_record(), BINDING)
    assert event is not None
    assert event["event_type"] == "token_usage"
    assert event["latest"]["input_tokens"] == 380000
    assert event["context_utilization"] == 0.5
    assert event["configured_context_window"] == 800000
    assert event["model_context_window"] == 760000
    encoded = json.dumps(event)
    assert "SECRET-BALANCE" not in encoded
    assert "SECRET-PLAN" not in encoded
    assert "rate_limits" not in encoded
    assert sanitize_rollout_record({
        "type": "response_item",
        "payload": {"type": "reasoning", "content": "SECRET-REASONING"},
    }, BINDING) is None


def test_writer_is_idempotent_and_projects_peak_context(tmp_path: Path):
    event_path = tmp_path / "runner" / "codex-telemetry.jsonl"
    writer = CodexTelemetryWriter(event_path)
    event = sanitize_rollout_record(token_record(), BINDING)
    assert writer.append(event) is True
    assert writer.append(event) is False
    compaction = sanitize_rollout_record({
        "timestamp": "2026-08-20T08:46:00Z",
        "type": "event_msg",
        "payload": {"type": "context_compacted"},
    }, BINDING)
    assert writer.append(compaction) is True
    lines = event_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["sequence"] for line in lines] == [1, 2]
    summary = json.loads(
        (event_path.parent / "codex-telemetry-summary.json").read_text(
            encoding="utf-8"))
    assert summary["event_count"] == 2
    assert summary["token_update_count"] == 1
    assert summary["compaction_count"] == 1
    assert summary["peak_request_input_tokens"] == 380000
    assert summary["peak_context_utilization"] == 0.5


def test_rollout_collector_matches_the_exact_workspace(tmp_path: Path):
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions" / "2026" / "08" / "20"
    sessions.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    thread_id = "thread-agent26"
    database = sqlite3.connect(codex_home / "state_5.sqlite")
    database.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, created_at INTEGER)")
    database.execute(
        "INSERT INTO threads (id, cwd, created_at) VALUES (?, ?, ?)",
        (thread_id, str(workspace.resolve()), 1001),
    )
    database.commit()
    database.close()
    rollout = sessions / f"rollout-2026-08-20T00-00-00-{thread_id}.jsonl"
    rollout.write_text(
        json.dumps(token_record()) + "\n"
        + json.dumps({
            "timestamp": "2026-08-20T08:46:00Z",
            "type": "event_msg",
            "payload": {"type": "context_compacted"},
        }) + "\n",
        encoding="utf-8",
    )
    event_path = tmp_path / "runner" / "codex-telemetry.jsonl"
    collector = CodexRolloutTelemetryCollector(
        codex_home=codex_home,
        cwd=workspace,
        binding=BINDING,
        event_path=event_path,
        started_at=1000,
        poll_interval=0.02,
    )
    assert collector.collect_once() == 2
    assert collector.collect_once() == 0
    assert collector.writer.summary["compaction_count"] == 1
    assert len(event_path.read_text(encoding="utf-8").splitlines()) == 2


def test_app_server_events_use_the_same_contract():
    token = sanitize_app_server_message({
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": "thread-26",
            "turnId": "turn-26",
            "tokenUsage": token_record()["payload"]["info"],
        },
    }, BINDING)
    assert token is not None
    assert token["event_type"] == "token_usage"
    assert token["thread_id"] == "thread-26"
    compaction = sanitize_app_server_message({
        "method": "item/completed",
        "params": {
            "threadId": "thread-26",
            "turnId": "turn-26",
            "item": {"type": "contextCompaction", "id": "compact-1"},
        },
    }, BINDING)
    assert compaction is not None
    assert compaction["event_type"] == "context_compaction"
    assert compaction["phase"] == "completed"


def test_supervisor_snapshot_exposes_only_the_safe_summary(tmp_path: Path):
    runner = tmp_path / "run-agent26"
    runner.mkdir(parents=True)
    log_path = runner / "stdout.log"
    log_path.write_text("terminal output", encoding="utf-8")
    writer = CodexTelemetryWriter(runner / "codex-telemetry.jsonl")
    writer.append(sanitize_rollout_record(token_record(), BINDING))
    summary = supervisor._codex_telemetry_summary({
        "command": ["/opt/bin/codex"],
        "log_path": str(log_path),
    })
    assert summary["status"] == "captured"
    assert summary["latest_total"]["total_tokens"] == 920000
    assert "SECRET" not in json.dumps(summary)


def main() -> None:
    test_binding_reads_only_safe_launch_configuration()
    test_rollout_token_event_excludes_account_and_model_content()
    test_app_server_events_use_the_same_contract()
    with tempfile.TemporaryDirectory(prefix="codex-telemetry-test-") as temp:
        root = Path(temp)
        test_writer_is_idempotent_and_projects_peak_context(root / "writer")
        test_rollout_collector_matches_the_exact_workspace(root / "collector")
        test_supervisor_snapshot_exposes_only_the_safe_summary(root / "supervisor")
    print("PASS: Codex telemetry captures token, context, and compaction facts")
    print("PASS: telemetry excludes reasoning, account, secret, and terminal content")


if __name__ == "__main__":
    main()
