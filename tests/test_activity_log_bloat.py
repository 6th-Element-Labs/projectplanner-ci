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

    # ------------------------------------------------------------------------------
    # register_host logs TRANSITIONS, not keepalives.
    #
    # adapters/agent_host.py re-POSTs its registration every heartbeat_ttl_s // 2
    # (~30s), and the upsert is idempotent — so an unconditional activity write turned
    # a keepalive into permanent history. Prod carried 75,130 `agent_host.registered`
    # rows across only 82 DISTINCT payloads, one host repeating a single identical
    # 527-byte payload 36,897 times. "A host appeared or changed what it advertises"
    # is the event worth keeping; "the same host said the same thing 30 seconds later"
    # is not.
    # ------------------------------------------------------------------------------
    def registration_rows():
        with _conn(P) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM activity WHERE kind='agent_host.registered'"
            ).fetchone()[0]

    def register(runtimes, limits, ttl=60):
        return store.register_host({
            "host_id": "host/reregister-probe", "agent_host_version": "0.2.25",
            "runtimes": runtimes, "limits": limits, "capacity": {"active_sessions": 0},
            "heartbeat_ttl_s": ttl,
        }, principal_id="principal/reregister", actor="host/reregister-probe", project=P)

    base_runtimes = [{"runtime": "codex", "lanes": ["BUG"]}]
    base_limits = {"max_sessions": 4}

    before = registration_rows()
    register(base_runtimes, base_limits)
    ok(registration_rows() == before + 1,
       "a host's FIRST registration is logged")

    after_first = registration_rows()
    for _ in range(25):
        register(base_runtimes, base_limits)
    ok(registration_rows() == after_first,
       "25 identical keepalive re-registrations add ZERO activity rows")

    # ...but a real change must still be recorded, or we would have traded noise for
    # blindness. Each of these is a different kind of advertisement change.
    register(base_runtimes, {"max_sessions": 8})
    ok(registration_rows() == after_first + 1,
       "changing advertised limits IS logged")
    register([{"runtime": "codex", "lanes": ["BUG", "PERF"]}], {"max_sessions": 8})
    ok(registration_rows() == after_first + 2,
       "changing advertised runtimes/lanes IS logged")
    register([{"runtime": "codex", "lanes": ["BUG", "PERF"]}], {"max_sessions": 8}, ttl=120)
    ok(registration_rows() == after_first + 3,
       "changing the heartbeat TTL IS logged")

    with _conn(P) as conn:
        first_payload, last_payload = conn.execute(
            "SELECT payload FROM activity WHERE kind='agent_host.registered' "
            "AND payload LIKE '%reregister-probe%' ORDER BY id LIMIT 1"
        ).fetchone()[0], conn.execute(
            "SELECT payload FROM activity WHERE kind='agent_host.registered' "
            "AND payload LIKE '%reregister-probe%' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    ok(json.loads(first_payload).get("change") == "first_registration",
       "the log distinguishes a host appearing...")
    ok(json.loads(last_payload).get("change") == "advertisement_changed",
       "...from a host changing what it advertises")

    # The keepalives must still do their real job: liveness stays fresh even though
    # nothing was logged. Dropping the log row must not drop the registration.
    live_host = store.host_status("host/reregister-probe", project=P) or {}
    ok((live_host.get("limits") or {}).get("max_sessions") == 8,
       "the authoritative host record still reflects the latest registration")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
