#!/usr/bin/env python3
"""BUG-319: runner history reads stay bounded and keyset-pageable."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="bug319-runner-pages-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from path_setup import ROOT  # noqa: E402,F401

import mcp_server  # noqa: E402
import store  # noqa: E402
from switchboard.storage.repositories import runner as runner_repo  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def insert_runner(index: int, heartbeat: float, *, snapshot_size: int = 40_000):
    session_id = f"run-{index:04d}"
    metadata = {
        "execution_id": f"exec-{index:04d}",
        "execution_generation": index + 1,
        "execution_role": "implementation",
        "work_session_id": f"work-{index:04d}",
        "branch": f"codex/BUG-319-{index:04d}",
        "source_sha": f"{index:040x}"[-40:],
    }
    with runner_repo._conn(P) as connection:
        connection.execute(
            "INSERT INTO runner_sessions("
            "runner_session_id,host_id,agent_id,runtime,task_id,claim_id,pid,status,cwd,"
            "control_json,metadata_json,last_snapshot_json,principal_id,started_at,"
            "heartbeat_at,heartbeat_ttl_s,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, "host/test", f"agent/test/{index}", "codex", "BUG-319", "",
             1000 + index, "running", "/tmp/bug319", json.dumps({"runner_logs": True}),
             json.dumps(metadata), json.dumps({"log_tail": "x" * snapshot_size}), "test",
             heartbeat - 10, heartbeat, 60, heartbeat),
        )
    return session_id


try:
    store.init_db(P)
    now = time.time()
    # 120 expired rows reproduce the historical query shape; three remain live.
    for number in range(120):
        insert_runner(number, now - 10_000 - number)
    live_ids = [insert_runner(1000 + number, now - number) for number in range(3)]

    first = json.loads(mcp_server.list_runner_sessions(
        project=P, include_stale=True, limit=50))
    ok(isinstance(first, list) and len(first) == 50,
       "historical MCP list is capped to the requested page")
    ok("metadata" not in first[0] and "last_snapshot" not in first[0]
       and "claim" not in first[0],
       "compact list omits expanded JSON blobs and claim records")
    ok(len(json.dumps(first)) < 50_000,
       "compact page stays small despite large stored snapshots")

    cursor = first[-1]
    second = json.loads(mcp_server.list_runner_sessions(
        project=P, include_stale=True, limit=50,
        before_heartbeat_at=cursor["heartbeat_at"],
        before_runner_session_id=cursor["runner_session_id"]))
    ok(len(second) == 50 and not ({row["runner_session_id"] for row in first}
                                  & {row["runner_session_id"] for row in second}),
       "keyset cursor returns the next non-overlapping page")

    current = json.loads(mcp_server.list_runner_sessions(project=P))
    ok({row["runner_session_id"] for row in current} == set(live_ids),
       "default read pushes stale filtering into the bounded query")

    full = json.loads(mcp_server.list_runner_sessions(
        project=P, include_stale=True, full=True))
    ok(isinstance(full, list) and len(full) == 10,
       "full history defaults to a smaller bounded page")
    ok("last_snapshot" in full[0] and "claim" not in full[0],
       "full list expands evidence but avoids list-time claim lookups")

    too_many = json.loads(mcp_server.list_runner_sessions(
        project=P, include_stale=True, full=True, limit=26))
    ok(too_many.get("error") == "runner_session_limit_out_of_range",
       "oversized full pages fail before querying history")
    incomplete = json.loads(mcp_server.list_runner_sessions(
        project=P, before_heartbeat_at=now))
    ok(incomplete.get("error") == "runner_session_cursor_incomplete",
       "partial keyset cursors fail closed")

    detail = json.loads(mcp_server.get_runner_session(live_ids[0], project=P))
    ok(detail.get("runner_session_id") == live_ids[0]
       and "last_snapshot" in detail,
       "exact-session tool returns one full-fidelity record")
    missing = json.loads(mcp_server.get_runner_session("run-missing", project=P))
    ok(missing.get("error") == "runner_session_not_found",
       "exact-session tool reports a missing identity explicitly")

    oversized_id = insert_runner(2000, now + 1, snapshot_size=1_100_000)
    oversized = json.loads(mcp_server.get_runner_session(oversized_id, project=P))
    ok(oversized.get("error") == "runner_session_result_too_large",
       "exact-session reads fail explicitly instead of emitting an oversized response")
    oversized_page = json.loads(mcp_server.list_runner_sessions(
        project=P, include_stale=True, full=True, limit=1))
    ok(oversized_page.get("error") == "runner_session_result_too_large",
       "full pages enforce the serialized response budget")

    with runner_repo._conn(P) as connection:
        indexes = {row["name"] for row in connection.execute(
            "PRAGMA index_list(runner_sessions)").fetchall()}
    ok("ix_runner_sessions_heartbeat_id" in indexes,
       "heartbeat/session keyset index is installed")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
