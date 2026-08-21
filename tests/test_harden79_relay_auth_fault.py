#!/usr/bin/env python3
"""HARDEN-79: a relay that refuses a credential must fault once, not loop forever.

Breakdown 21 observed 46 consecutive connect->fail cycles on a runner's host
tunnel, with no escalation and no backoff-to-fault, because the Agent Host
still carried a ``PM_MCP_TOKEN`` that predated a rotation. Three halves have to
hold for that to be impossible again:

1. the client counts consecutive AUTH failures, re-reads its credential from
   disk before giving up, and raises exactly ONE typed fault per episode;
2. a rotation that already landed on disk heals with no restart, and a
   credential that cannot heal says "restart required" instead of pretending;
3. the server counts the same rejections against the host row, because a host
   whose bearer is stale cannot report anything itself — its heartbeat 401s too.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="harden79-relay-auth-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_RUNNER_DIR"] = str(TMP / "runner-state")

import store  # noqa: E402
from adapters import relay_auth  # noqa: E402
from db.connection import _conn  # noqa: E402


P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


class _Status:
    """Stand-in for websockets' handshake rejection (has a status_code)."""

    def __init__(self, status):
        self.status_code = status


class _InvalidStatus(Exception):
    def __init__(self, status):
        super().__init__(f"server rejected WebSocket connection: HTTP {status}")
        self.response = _Status(status)


try:
    # ---- classification -------------------------------------------------
    ok(relay_auth.classify_relay_failure(_InvalidStatus(401)) == "auth",
       "a 401 handshake rejection classifies as auth")
    ok(relay_auth.classify_relay_failure(_InvalidStatus(403)) == "auth",
       "a 403 handshake rejection classifies as auth")
    ok(relay_auth.classify_relay_failure(
        RuntimeError("HTTP 401 /ixp/v1/mint_host_tunnel_url: detail=denied"))
        == "auth",
       "switchboard_core's RuntimeError('HTTP 401 …') classifies as auth")
    ok(relay_auth.classify_relay_failure(
        ConnectionError("host_tunnel_socket_closed")) == "transport",
       "an ordinary socket drop stays transport, not auth")
    ok(relay_auth.classify_relay_failure(_InvalidStatus(502)) == "transport",
       "a 502 from a proxy is transport, not a credential problem")

    # ---- exactly one fault, and the loop widens -------------------------
    faults = []
    tracker = relay_auth.RelayAuthFaultTracker(
        threshold=5, on_fault=faults.append,
        # No on-disk source in this episode: the fault must say so.
        reload_credential=lambda: {"source": "spawn_env", "changed": False,
                                   "adopted": False, "restart_required": True,
                                   "current_fingerprint": "7938bfc3"},
        label="run-harden79")
    decisions = [tracker.record_failure("auth", "HTTP 401") for _ in range(46)]
    ok(len(faults) == 1,
       "46 consecutive auth rejections raise exactly one fault")
    ok(sum(1 for d in decisions if d.get("escalated")) == 1,
       "exactly one decision is marked escalated")
    ok(decisions[4].get("escalated") is True and decisions[3].get("fault") is None,
       "escalation lands on the 5th cycle, not the 46th")
    fault = faults[0]
    ok(fault.get("reason") == relay_auth.FAULT_REASON
       and fault.get("schema") == relay_auth.FAULT_SCHEMA,
       "the fault is typed (schema + relay_auth_failed reason)")
    ok(fault.get("attempt_count") == 5 and fault.get("first_failure_at") > 0,
       "the fault carries the attempt count and the first-failure time")
    ok(fault.get("restart_required") is True
       and "restart required" in fault.get("remediation", ""),
       "a spawn-env-only credential faults with 'restart required'")
    ok(decisions[-1]["backoff_ceiling_s"] == relay_auth.FAULTED_BACKOFF_CEILING_S
       > relay_auth.DEFAULT_BACKOFF_CEILING_S,
       "after the fault the reconnect ladder backs off instead of tight-looping")

    # ---- a transport blip neither escalates nor clears an episode --------
    blip = tracker.record_failure("transport", "socket dropped")
    ok(blip.get("escalated") is False and len(faults) == 1,
       "a transport failure never raises a second fault")

    # ---- a real connect ends the episode, and the next one can fault -----
    tracker.record_success()
    ok(tracker.faulted is False and tracker.consecutive_failures == 0,
       "one successful connect clears the fault episode")
    for _ in range(5):
        tracker.record_failure("auth", "HTTP 401")
    ok(len(faults) == 2,
       "a fresh lockout after recovery raises its own single fault")

    # ---- self-heal: the rotated credential is already on disk -----------
    identity_path = TMP / "identity.json"
    identity_path.write_text(json.dumps({"host_token": "rotated-913887d0"}),
                             encoding="utf-8")
    env = {"PM_MCP_TOKEN": "stale-7938bfc3"}
    os.environ["PM_AGENT_HOST_IDENTITY_PATH"] = str(identity_path)
    try:
        receipt = relay_auth.reload_credential(env=env)
        ok(receipt.get("adopted") is True and env["PM_MCP_TOKEN"] == "rotated-913887d0",
           "a rotation already on disk is adopted without a restart")
        ok(receipt.get("restart_required") is False,
           "a self-healed reload does not claim a restart is required")
        ok(receipt.get("current_fingerprint")
           == relay_auth.token_fingerprint("stale-7938bfc3")
           and receipt.get("source_fingerprint")
           == relay_auth.token_fingerprint("rotated-913887d0"),
           "the receipt fingerprints both credentials without exposing either")

        healing = []
        healer = relay_auth.RelayAuthFaultTracker(
            threshold=3, on_fault=healing.append,
            reload_credential=lambda: relay_auth.reload_credential(
                env={"PM_MCP_TOKEN": "stale-7938bfc3"}))
        heal_decisions = [healer.record_failure("auth", "HTTP 401")
                          for _ in range(3)]
        ok(not healing and heal_decisions[-1].get("healed") is True,
           "reaching the threshold with a fresh on-disk credential heals, not faults")
        ok(healer.consecutive_failures == 0 and healer.faulted is False,
           "the healed episode resets so a genuinely dead credential still faults")

        # Disk copy is the stale one too: restarting would change nothing.
        identity_path.write_text(json.dumps({"host_token": "stale-7938bfc3"}),
                                 encoding="utf-8")
        same = relay_auth.reload_credential(env={"PM_MCP_TOKEN": "stale-7938bfc3"})
        ok(same.get("adopted") is False and same.get("restart_required") is True,
           "an on-disk credential identical to the live one still demands operator action")
    finally:
        os.environ.pop("PM_AGENT_HOST_IDENTITY_PATH", None)

    ok(relay_auth.credential_source()["kind"] == "spawn_env"
       and relay_auth.credential_source()["restart_required"] is True,
       "without an identity path the only source is spawn-time env")

    # ---- the companion hands its fault to the credential-owning host ----
    session_dir = TMP / "session"
    ok(relay_auth.publish_fault(session_dir, fault) is True
       and relay_auth.fault_path(session_dir).exists(),
       "the companion publishes its fault into the session directory")
    consumed = relay_auth.consume_fault(session_dir)
    ok(consumed and consumed.get("reason") == relay_auth.FAULT_REASON,
       "the Agent Host consumes the published fault")
    ok(relay_auth.consume_fault(session_dir) is None,
       "consuming clears it, so one episode is reported once")

    # ---- the real reconnect ladder honours the policy -------------------
    from adapters.codex import pty_host_ws_client  # noqa: E402
    from unittest import mock  # noqa: E402
    import asyncio  # noqa: E402

    ladder_faults = []
    ladder_policy = relay_auth.RelayAuthFaultTracker(
        threshold=3, on_fault=ladder_faults.append,
        reload_credential=lambda: {"source": "spawn_env", "changed": False,
                                   "adopted": False, "restart_required": True})
    conn = pty_host_ws_client.HostTunnelConnection(
        "wss://relay.example/revoked", require_initial=False,
        auth_policy=ladder_policy)
    slept = []
    dials = {"n": 0}

    def _refuse(_url, **_kwargs):
        dials["n"] += 1
        if dials["n"] >= 6:
            conn._stopped.set()
        raise _InvalidStatus(401)

    async def _sleep(seconds):
        slept.append(seconds)

    with mock.patch.object(pty_host_ws_client.websockets, "connect", _refuse), \
            mock.patch.object(pty_host_ws_client.asyncio, "sleep", _sleep):
        asyncio.run(conn._main())

    ok(len(ladder_faults) == 1,
       "a 401 loop through the real reconnect ladder raises exactly one fault")
    ok(max(slept) >= relay_auth.FAULTED_BACKOFF_CEILING_S * 0.8,
       "the ladder's sleep widens to the faulted ceiling instead of 8s")
    ok(slept[0] < relay_auth.FAULTED_BACKOFF_CEILING_S * 0.8,
       "and it only widens after the fault, not on the first rejection")

    # ---- server side: visible off-box without the host authenticating ---
    store.init_db(P)
    host_id = "host/harden79-mac"
    principal_id = "principal/harden79-host"
    store.register_host({
        "host_id": host_id, "agent_host_version": "0.2.27",
        "runtimes": [{"runtime": "codex", "lanes": ["HARDEN"]}],
        "limits": {"max_sessions": 2},
        "capacity": {"active_sessions": 0},
        "heartbeat_ttl_s": 60,
    }, principal_id=principal_id, actor=host_id, project=P)

    results = [
        store.record_host_relay_auth_event(
            host_id, outcome="rejected", reason="ticket_expired",
            runner_session_id="run-harden79", project=P)
        for _ in range(7)
    ]
    ok(sum(1 for r in results if r.get("escalated")) == 1,
       "seven server-side rejections escalate exactly once")
    ok(results[4].get("escalated") is True,
       "the server escalates on its own 5th consecutive rejection")
    status = store.host_status(host_id, project=P)
    server_fault = status.get("fault") or {}
    ok(server_fault.get("reason") == "relay_auth_failed",
       "host_status surfaces the typed relay_auth_failed fault")
    ok(server_fault.get("attempt_count") == 7
       and server_fault.get("first_failure_at") > 0,
       "the host row carries the live attempt count and first-failure time")
    ok(server_fault.get("runner_session_id") == "run-harden79",
       "the fault names the session whose tunnel is being refused")
    with _conn(P) as c:
        announcements = c.execute(
            "SELECT COUNT(*) n FROM activity WHERE kind=?",
            ("agent_host.relay_auth_failed",)).fetchone()["n"]
    ok(announcements == 1,
       "the streak is announced once, not once per rejection")

    cleared = store.record_host_relay_auth_event(
        host_id, outcome="accepted", project=P)
    ok(cleared.get("cleared") is True,
       "a tunnel that actually attaches clears the episode")
    recovered = store.host_status(host_id, project=P)
    ok(recovered.get("fault") is None
       and (recovered.get("relay_auth") or {}).get("consecutive_failures") == 0,
       "host_status stops reporting a fault once the host reconnects")

    unknown = store.record_host_relay_auth_event(
        "host/never-registered", outcome="rejected", project=P)
    ok(unknown.get("recorded") is False,
       "an unregistered host_id cannot conjure a host row")

    # ---- the host's own account lands when it can still heartbeat -------
    heartbeat = store.heartbeat_host(
        host_id, active_sessions=0,
        relay_auth_fault={
            "schema": relay_auth.FAULT_SCHEMA,
            "reason": relay_auth.FAULT_REASON,
            "attempt_count": 5, "first_failure_at": 1.0,
            "credential_source": "spawn_env", "restart_required": True,
        },
        principal_id=principal_id, actor=host_id, project=P)
    reported = ((heartbeat.get("relay_auth") or {}).get("host_reported") or {})
    ok(reported.get("restart_required") is True
       and reported.get("credential_source") == "spawn_env",
       "a host-reported fault records the credential source only the host knows")
    ok((heartbeat.get("fault") or {}).get("reason") == "relay_auth_failed",
       "a host-reported fault also surfaces as the host row's fault")

    ambiguous = store.heartbeat_host(
        host_id, active_sessions=0, principal_id=principal_id,
        actor=host_id, project=P)
    ok((ambiguous.get("fault") or {}).get("reason") == "relay_auth_failed",
       "a healthy heartbeat does not erase a fault with no runner identity")

    store.record_host_relay_auth_event(
        host_id, outcome="accepted", actor=host_id, project=P)
    runner_session_id = "run-harden79-recovered"
    now = time.time()
    with _conn(P) as c:
        c.execute(
            "INSERT INTO runner_sessions("
            "runner_session_id, host_id, agent_id, runtime, task_id, status, "
            "heartbeat_at, heartbeat_ttl_s, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (runner_session_id, host_id, "agent/harden79", "codex",
             "HARDEN-79", "starting", now, 3600, now))
    store.heartbeat_host(
        host_id, active_sessions=1,
        relay_auth_fault={
            "schema": relay_auth.FAULT_SCHEMA,
            "reason": relay_auth.FAULT_REASON,
            "runner_session_id": runner_session_id,
            "attempt_count": 5,
            "first_failure_at": now,
        },
        principal_id=principal_id, actor=host_id, project=P)
    live_fault = store.heartbeat_host(
        host_id, active_sessions=1, principal_id=principal_id,
        actor=host_id, project=P)
    ok((live_fault.get("fault") or {}).get("runner_session_id")
       == runner_session_id,
       "an authenticated heartbeat keeps the fault while its runner is starting")

    with _conn(P) as c:
        c.execute(
            "UPDATE runner_sessions SET heartbeat_at=1, heartbeat_ttl_s=1 "
            "WHERE runner_session_id=?", (runner_session_id,))
    reconciled = store.heartbeat_host(
        host_id, active_sessions=0, principal_id=principal_id,
        actor=host_id, project=P)
    ok(reconciled.get("fault") is None
       and (reconciled.get("relay_auth") or {}).get("recovery_reason")
       == "fault_runner_not_live",
       "a healthy heartbeat clears the fault after its runner is not live")
    with _conn(P) as c:
        recovery = c.execute(
            "SELECT payload FROM activity WHERE kind=? ORDER BY id DESC LIMIT 1",
            ("agent_host.relay_auth_recovered",)).fetchone()
    recovery_payload = json.loads(recovery["payload"]) if recovery else {}
    ok(recovery_payload.get("reason") == "fault_runner_not_live"
       and recovery_payload.get("runner_session_id") == runner_session_id,
       "the one recovery transition records why the stale fault was cleared")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nHARDEN-79 relay auth fault: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
