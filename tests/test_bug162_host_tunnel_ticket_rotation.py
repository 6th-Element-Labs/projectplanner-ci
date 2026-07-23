#!/usr/bin/env python3
"""BUG-162: host tunnel tickets rotate only near expiry, not every heartbeat.

Heartbeat mints a fresh host_url JWT every Agent Host tick (~10s). Applying that
URL tears down the live WebSocket and flashes Watch "Bridge detached". A healthy
tunnel must keep its applied ticket until the skew window.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = Path(tempfile.mkdtemp(prefix="bug162-host-relay-"))
os.environ["PM_RUNNER_PTY_RELAY_SECRET"] = "bug162-relay-secret"
os.environ["PM_RUNNER_STREAM_SECRET"] = "bug162-stream-secret"
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")

from adapters import agent_host  # noqa: E402
import codex.pty_host_ws_client as ws_client_module  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


class _FakeSession:
    def __init__(self, runner_session_id, relay_ws_url, **kw):
        self.runner_session_id = runner_session_id
        self.relay_ws_url = relay_ws_url
        self.kw = kw
        self.stopped = False
        self.url_updates = []

    def is_alive(self):
        return not self.stopped

    def stop(self):
        self.stopped = True

    def update_relay_url(self, relay_ws_url):
        self.relay_ws_url = relay_ws_url
        self.url_updates.append(relay_ws_url)


_captured = {"calls": 0}


def _fake_open_host_bridge(*, runner_session_id, relay_ws_url, **kw):
    session = _FakeSession(runner_session_id, relay_ws_url, **kw)
    _captured["session"] = session
    _captured["calls"] = _captured.get("calls", 0) + 1
    return session


ws_client_module.open_host_bridge = _fake_open_host_bridge
agent_host._HOST_BRIDGES.clear()
if hasattr(agent_host, "_HOST_RELAY_APPLIED"):
    agent_host._HOST_RELAY_APPLIED.clear()

# Pure helper: missing/invalid expiry rotates (fail closed — don't go dark).
ok(agent_host._host_relay_needs_rotation(None) is True,
   "missing applied expiry needs rotation")
ok(agent_host._host_relay_needs_rotation(0) is True,
   "zero applied expiry needs rotation")
ok(agent_host._host_relay_needs_rotation("") is True,
   "empty applied expiry needs rotation")

now = 1_700_000_000.0
skew = float(agent_host.HOST_RELAY_ROTATE_SKEW_S)
ok(agent_host._host_relay_needs_rotation(now + skew + 1, now=now) is False,
   "ticket with more than skew remaining does not rotate")
ok(agent_host._host_relay_needs_rotation(now + skew, now=now) is True,
   "ticket at the skew boundary rotates")
ok(agent_host._host_relay_needs_rotation(now + 1, now=now) is True,
   "ticket inside the skew window rotates")
ok(agent_host._host_relay_needs_rotation(now - 1, now=now) is True,
   "already-expired ticket rotates")

binding = {
    "tenant_id": "tenant/t1",
    "user_id": "user/u1",
    "project_id": "switchboard",
    "task_id": "BUG-162",
    "claim_id": "claim-bug162",
    "work_session_id": "ws-bug162",
    "runner_session_id": "run_bug162",
    "host_id": "host/bug162",
    "wake_id": "wake-bug162",
    "execution_connection_id": "execconn/bug162",
    "source_sha": "deadbeef",
    "permission_profile": "operator_watch",
}
base_kwargs = dict(
    runner_session_id="run_bug162",
    host_id="host/bug162",
    binding=binding,
    public_base="https://plan.example",
)

url_a = "wss://plan.example/pty/host?ticket=aaa&host_id=host%2Fbug162"
url_b = "wss://plan.example/pty/host?ticket=bbb&host_id=host%2Fbug162"
url_c = "wss://plan.example/pty/host?ticket=ccc&host_id=host%2Fbug162"

# First apply with a far-future expiry opens once and records the applied ticket.
# Use wall-clock expiries: the gate compares against time.time().
far_exp = time.time() + 900
session = agent_host._ensure_host_bridge(
    **base_kwargs, host_relay_url=url_a, expires_at=far_exp)
ok(session is _captured["session"], "first ensure opens the host bridge")
ok(_captured["calls"] == 1, "first ensure dials open_host_bridge once")
ok(session.relay_ws_url == url_a, "first ensure applies url_a")
ok(session.url_updates == [], "first open does not call update_relay_url")

# Mid-lifetime heartbeat with a freshly minted URL must keep the live tunnel.
session2 = agent_host._ensure_host_bridge(
    **base_kwargs, host_relay_url=url_b, expires_at=time.time() + 900)
ok(session2 is session, "mid-lifetime ensure reuses the live bridge")
ok(_captured["calls"] == 1, "mid-lifetime ensure does not re-open the bridge")
ok(session.url_updates == [],
   "BUG-162: mid-lifetime ticket mint does not rotate the live host tunnel")
ok(session.relay_ws_url == url_a,
   "BUG-162: applied URL stays on the first ticket until near expiry")

# Near applied-ticket expiry, accept the new URL (teardown is intentional then).
# Force the applied clock into the skew window without waiting 15 minutes.
agent_host._HOST_RELAY_APPLIED["run_bug162"] = {
    "url": url_a,
    "expires_at": time.time() + max(5.0, skew / 2),
}
session3 = agent_host._ensure_host_bridge(
    **base_kwargs, host_relay_url=url_c, expires_at=time.time() + 900)
ok(session3 is session, "near-expiry ensure still reuses the live bridge object")
ok(session.url_updates == [url_c],
   "BUG-162: near-expiry heartbeat rotates once onto the fresh ticket")
ok(session.relay_ws_url == url_c, "near-expiry apply stores the fresh URL")

agent_host._drop_host_bridge("run_bug162")
ok("run_bug162" not in agent_host._HOST_RELAY_APPLIED,
   "drop clears the applied-ticket ledger so a restart can attach cleanly")

# Attach without expires_at must still suppress mid-lifetime rotation (assume ttl).
url_d = "wss://plan.example/pty/host?ticket=ddd&host_id=host%2Fbug162"
url_e = "wss://plan.example/pty/host?ticket=eee&host_id=host%2Fbug162"
session4 = agent_host._ensure_host_bridge(**base_kwargs, host_relay_url=url_d)
ok(session4.relay_ws_url == url_d, "attach without expires_at still opens")
applied = agent_host._HOST_RELAY_APPLIED.get("run_bug162") or {}
ok(float(applied.get("expires_at") or 0) > time.time() + 600,
   "missing expires_at records an assumed mid-lifetime expiry, not zero")
session5 = agent_host._ensure_host_bridge(
    **base_kwargs, host_relay_url=url_e, expires_at=time.time() + 900)
ok(session5 is session4 and session4.url_updates == [],
   "attach-without-expires_at does not flap on the next heartbeat mint")
agent_host._drop_host_bridge("run_bug162")

print(f"\nBUG-162 host tunnel ticket rotation: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
