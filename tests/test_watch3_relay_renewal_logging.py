#!/usr/bin/env python3
"""WATCH-3: relay renewal failures stay visible on host and server."""
from __future__ import annotations

import os
import sqlite3

from path_setup import ROOT  # noqa: F401

from adapters import agent_host
from switchboard.application.commands import runner_control
from switchboard.storage.repositories import runner as runner_repo


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


session = {
    "runner_session_id": "run-watch3",
    "task_id": "WATCH-3",
    "claim_id": "taskclaim-watch3",
    "host_id": "host/watch3",
    "agent_id": "codex/watch3",
    "runtime": "codex",
    "status": "running",
    "alive": True,
    "pid": 123,
    "cwd": "/repo",
    "metadata": {
        "wake_id": "wake-watch3",
        "work_session_id": "worksession-watch3",
        "native_host_execution": True,
    },
}

# Host-side heartbeat summary: a successful HTTP heartbeat that omitted host_url
# must no longer look like a successful renewal in agent-host.log.
saved = {
    "runners": agent_host._drain_runners,
    "preflight": agent_host._host_repo_preflight,
    "try": agent_host._try,
}
try:
    agent_host._drain_runners = lambda _host_id: [dict(session)]
    agent_host._host_repo_preflight = lambda *_args, **_kwargs: None
    agent_host._try = lambda *_args, **_kwargs: {
        "runner_session_id": "run-watch3",
        "server_relay": {
            "error": runner_repo.RUNNER_BIND_ERROR,
            "missing": ["source_sha", "execution_connection_id"],
        },
    }
    entries = agent_host.renew_live_direct_runners({
        "host_id": "host/watch3", "repo_root": "/repo"})
finally:
    agent_host._drain_runners = saved["runners"]
    agent_host._host_repo_preflight = saved["preflight"]
    agent_host._try = saved["try"]

entry = entries[0] if entries else {}
ok(entry.get("renewed") is True,
   "the HTTP runner heartbeat remains separately reported as renewed")
ok(entry.get("relay_url_minted") is False,
   "the host summary explicitly reports that no relay URL was minted")
ok(entry.get("server_relay_error") == runner_repo.RUNNER_BIND_ERROR,
   "the host summary preserves server_relay.error")
ok(entry.get("server_relay_missing") == ["source_sha", "execution_connection_id"],
   "the host summary preserves server_relay.missing")

# The public-base failure itself names the missing configuration field.
saved_base = os.environ.pop("PM_RUNNER_PTY_RELAY_PUBLIC_BASE", None)
try:
    relay_failure = runner_repo._server_relay_options(
        session, user_id="user-watch3", project="switchboard")
finally:
    if saved_base is not None:
        os.environ["PM_RUNNER_PTY_RELAY_PUBLIC_BASE"] = saved_base
ok(relay_failure.get("error") == "relay_public_base_unavailable",
   "the server preserves the relay_public_base_unavailable error")
ok(relay_failure.get("missing") == ["relay_public_base"],
   "the public-base failure includes its missing-field list")

# Heartbeat command path emits the structured activity signal after the runner
# transaction has closed. Keep this unit-scoped so it does not need a live DB.
recorded = []
saved_repo = {
    "upsert": runner_repo.upsert_runner_session,
    "get": runner_repo.get_runner_session,
    "mint": runner_repo._server_relay_options,
    "record": runner_repo.record_server_relay_failure,
}
try:
    runner_repo.upsert_runner_session = lambda *_args, **_kwargs: {
        "runner_session_id": "run-watch3"}
    runner_repo.get_runner_session = lambda *_args, **_kwargs: dict(session)
    runner_repo._server_relay_options = lambda *_args, **_kwargs: {
        "error": runner_repo.RUNNER_BIND_ERROR,
        "missing": ["claim_id", "work_session_id"],
    }
    runner_repo.record_server_relay_failure = (
        lambda sess, failure, **kwargs: recorded.append(
            runner_repo._server_relay_failure_event(sess, failure)) or recorded[-1])
    command_result = runner_control.upsert_session_mapping_result(
        {"project": "switchboard", "runner_session_id": "run-watch3"},
        actor="agent-host/watch3", principal_id="principal/watch3")
finally:
    runner_repo.upsert_runner_session = saved_repo["upsert"]
    runner_repo.get_runner_session = saved_repo["get"]
    runner_repo._server_relay_options = saved_repo["mint"]
    runner_repo.record_server_relay_failure = saved_repo["record"]

event = recorded[0] if recorded else {}
ok(command_result.get("server_relay", {}).get("error") == runner_repo.RUNNER_BIND_ERROR,
   "the heartbeat response still carries the original relay failure")
ok(event.get("schema") == runner_repo.SERVER_RELAY_FAILURE_SCHEMA,
   "the server emits a typed structured relay-failure event")
ok(event.get("runner_session_id") == "run-watch3",
   "the structured event names the affected runner session")
ok(event.get("missing") == ["claim_id", "work_session_id"],
   "the structured event carries the missing bind fields")

# Persistence is bounded: the same runner/error/missing tuple produces one
# activity row per five-minute window rather than one row per host heartbeat.
with sqlite3.connect(":memory:") as conn:
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE activity (id INTEGER PRIMARY KEY, task_id TEXT, actor TEXT, "
        "kind TEXT, payload TEXT, created_at REAL)")
    first = runner_repo._record_server_relay_failure_in(
        conn, session, command_result["server_relay"],
        actor="agent-host/watch3", now=1000)
    duplicate = runner_repo._record_server_relay_failure_in(
        conn, session, command_result["server_relay"],
        actor="agent-host/watch3", now=1001)
    event_count = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
ok(first.get("recorded") is True and duplicate.get("recorded") is False,
   "the server records the first failure and deduplicates repeated heartbeats")
ok(event_count == 1,
   "relay failure deduplication prevents activity-stream flooding")

print(f"\nWATCH-3 relay renewal logging: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
