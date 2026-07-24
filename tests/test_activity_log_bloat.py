#!/usr/bin/env python3
"""Heartbeat activity rows must log liveness, not a full capacity snapshot.

heartbeat_host used to append the entire resolved capacity struct to `activity`
on every ping. On prod that was 230,331 rows / 201 MB in 26 days, ~97% of which
was that one field, and nothing ever read it back: every reader of `activity` is
kind-scoped (merge.gate, ci.attribution, pr.provenance_gate) or task-scoped, and
no reader selects kind='agent_host.heartbeat'.

Capacity is live state, not history. The authoritative copy belongs in
agent_hosts.capacity_json — which host_status/list_agent_hosts serve — so the log
keeps only a small summary. These assertions pin both halves: the log stays lean
AND the authoritative capacity stays complete.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="activity-log-bloat-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_RUNNER_DIR"] = str(TMP / "runner-state")

import store  # noqa: E402
from db.connection import _conn  # noqa: E402


P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


# A capacity struct shaped like the real one: a big nested inventory that has no
# business being copied into an append-only log on every ping.
FAT_CAPACITY = {
    "active_sessions": 1,
    "max_sessions": 8,
    "allow_work": True,
    "drain_state": "accepting",
    "local_auth": {"available": True, "detail": "x" * 400},
    "runtime_profile": {
        "components": {"agent_host_version": "0.2.27"},
        "inventory": {f"tool_{i}": {"path": f"/usr/local/bin/tool_{i}",
                                    "version": "1.2.3", "notes": "y" * 60}
                      for i in range(30)},
    },
}

try:
    store.init_db(P)
    host_id = "host/activity-bloat-mac"
    principal_id = "principal/activity-bloat-host"
    store.register_host({
        "host_id": host_id, "agent_host_version": "0.2.25",
        "runtimes": [{"runtime": "codex", "lanes": ["BUG"]}],
        "limits": {"max_sessions": 8},
        "capacity": {"active_sessions": 0},
        "heartbeat_ttl_s": 60,
    }, principal_id=principal_id, actor=host_id, project=P)

    store.heartbeat_host(
        host_id, active_sessions=1, capacity=FAT_CAPACITY,
        principal_id=principal_id, actor=host_id, project=P)

    with _conn(P) as c:
        payload_text = c.execute(
            "SELECT payload FROM activity WHERE kind='agent_host.heartbeat' "
            "ORDER BY id DESC LIMIT 1").fetchone()[0]
    payload = json.loads(payload_text)

    ok("capacity" not in payload,
       "heartbeat activity row no longer carries the full capacity struct")
    # The fat inventory is the thing that made these rows 2.4KB each.
    ok("runtime_profile" not in payload_text and "tool_29" not in payload_text,
       "the nested runtime inventory is not copied into the activity log")
    ok(len(payload_text) < 400,
       f"heartbeat activity payload stays small (got {len(payload_text)} bytes)")

    # Still answerable from the log alone: was this host up and accepting work?
    ok(payload.get("host_id") == host_id and payload.get("status") == "online",
       "heartbeat activity row still identifies the host and its status")
    summary = payload.get("capacity_summary") or {}
    ok(summary.get("active_sessions") == 1
       and summary.get("allow_work") is True
       and summary.get("drain_state") == "accepting",
       "heartbeat keeps the small counters that answer 'was it accepting work?'")

    # The other half of the contract: authoritative capacity must be UNCHANGED.
    # Trimming the log must never cost us live state.
    host = store.host_status(host_id, project=P) or {}
    live = host.get("capacity") or {}
    ok(live.get("active_sessions") == 1,
       "authoritative capacity still tracks active_sessions")
    ok(((live.get("runtime_profile") or {}).get("components") or {}).get(
        "agent_host_version") == "0.2.27",
       "authoritative capacity still carries the full runtime profile")
    ok(len((live.get("runtime_profile") or {}).get("inventory") or {}) == 30,
       "authoritative capacity still carries the complete tool inventory")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
