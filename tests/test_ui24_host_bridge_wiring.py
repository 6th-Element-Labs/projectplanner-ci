#!/usr/bin/env python3
"""UI-24 / SIMPLIFY-9: host-side relay bridge wiring.

Tier A - HostTunnelConnection against a real in-process websockets.serve()
loopback server (binary frames).

Tier B - agent_host._ensure_host_bridge ticket/URL/registry logic with
open_host_bridge stubbed — no localhost stream/control URLs required.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = Path(tempfile.mkdtemp(prefix="ui24-host-bridge-"))
os.environ["PM_RUNNER_PTY_RELAY_SECRET"] = "ui24-relay-secret"
os.environ["PM_RUNNER_STREAM_SECRET"] = "ui24-stream-secret"
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")

import websockets  # noqa: E402

from adapters import agent_host  # noqa: E402
from switchboard.application import runner_pty_relay as relay  # noqa: E402
from switchboard.domain import runner_pty as domain  # noqa: E402
from codex.pty_host_ws_client import HostTunnelConnection  # noqa: E402
import codex.pty_host_ws_client as ws_client_module  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def _binding(session_id, **overrides):
    base = {
        "tenant_id": "tenant/t1",
        "user_id": "user/u1",
        "project_id": "switchboard",
        "task_id": "UI-24",
        "claim_id": "claim-ui24",
        "work_session_id": "ws-ui24",
        "runner_session_id": session_id,
        "host_id": "host/ui24",
        "wake_id": "wake-ui24",
        "execution_connection_id": "execconn/ui24",
        "source_sha": "deadbeef",
        "permission_profile": "operator_watch",
    }
    base.update(overrides)
    return base


relay.clear_revoked_jtis_for_tests()

# ── Tier A: HostTunnelConnection against a real loopback WS server ──
_received = []
_server_holder = {}
_server_ready = threading.Event()


async def _echo_handler(ws):
    async for message in ws:
        _received.append(message)
        # Echo a binary ready ack.
        await ws.send(domain.encode_frame("ready", {"echo": True}))


def _run_server():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _serve():
        server = await websockets.serve(_echo_handler, "127.0.0.1", 0)
        _server_holder["server"] = server
        _server_holder["loop"] = loop
        _server_ready.set()
        await server.wait_closed()

    loop.run_until_complete(_serve())


server_thread = threading.Thread(target=_run_server, name="ui24-test-ws-server", daemon=True)
server_thread.start()
ok(_server_ready.wait(timeout=5), "test WS server starts")
port = list(_server_holder["server"].sockets)[0].getsockname()[1]
ws_url = f"ws://127.0.0.1:{port}"

conn_frames = []
conn = HostTunnelConnection(ws_url, on_frame=lambda f: conn_frames.append(f))
conn.start(timeout=5)
ok(conn.connected, "HostTunnelConnection connects to a real WS server")

send_raised = False
try:
    conn.send(domain.encode_frame("out", data=b"hello"))
except Exception:
    send_raised = True
ok(not send_raised, "send() delivers a binary frame cross-thread via run_coroutine_threadsafe")
time.sleep(0.3)
ok(len(_received) == 1 and isinstance(_received[0], (bytes, bytearray)),
   "the server actually received the binary frame")
ok(bool(conn_frames) and domain.decode_frame(conn_frames[0]).get("type") == "ready",
   "on_frame fires for server -> client binary messages")

conn.stop(timeout=5)
ok(not conn.connected, "stop() tears the connection down")
raised_after_stop = False
try:
    conn.send(domain.encode_frame("in", data=b"x"))
except Exception:
    raised_after_stop = True
ok(raised_after_stop,
   "send() after stop() raises instead of swallowing the failure")

# Chat ack: ready frame with request_id (replaces control_ack).
ack_frame = domain.encode_frame(
    "ready", {"request_id": "runner-chat-ack-1", "ok": True, "ack": True})
ack_decoded = domain.decode_frame(ack_frame)
ok(ack_decoded.get("type") == "ready"
   and ack_decoded.get("request_id") == "runner-chat-ack-1"
   and ack_decoded.get("ok") is True,
   "delivery acknowledgement binds success to the exact browser request id")

bad_conn = HostTunnelConnection("ws://127.0.0.1:1", on_frame=lambda f: None)
raised = False
try:
    bad_conn.start(timeout=3)
except Exception:
    raised = True
ok(raised, "start() raises (fails closed) when the host tunnel can't connect")

_server_holder["loop"].call_soon_threadsafe(_server_holder["server"].close)

# ── Tier B: _ensure_host_bridge — ticket scope, URL shape, registry ──


class _FakeSession:
    def __init__(self, runner_session_id, relay_ws_url, **kw):
        self.runner_session_id = runner_session_id
        self.relay_ws_url = relay_ws_url
        self.kw = kw
        self.stopped = False

    def is_alive(self):
        return not self.stopped

    def stop(self):
        self.stopped = True


_captured = {}


def _fake_open_host_bridge(*, runner_session_id, relay_ws_url, **kw):
    session = _FakeSession(runner_session_id, relay_ws_url, **kw)
    _captured["session"] = session
    _captured["relay_ws_url"] = relay_ws_url
    _captured["kw"] = kw
    _captured["calls"] = _captured.get("calls", 0) + 1
    return session


ws_client_module.open_host_bridge = _fake_open_host_bridge

binding = _binding("run_ui24")
kwargs = dict(
    runner_session_id="run_ui24",
    host_id="host/ui24",
    binding=binding,
    public_base="https://plan.example",
)

session1 = agent_host._ensure_host_bridge(**kwargs)
ok(session1 is _captured["session"], "_ensure_host_bridge opens a bridge on first call")
ok("/pty/host?" in _captured["relay_ws_url"] and "host_id=host" in _captured["relay_ws_url"],
   "the host bridge connects to /pty/host with host_id bound in the URL, "
   "distinct from the browser's /pty path")
ok("local_stream_url" not in (_captured.get("kw") or {}),
   "host bridge open does not pass localhost stream URLs (SIMPLIFY-9)")

ticket = _captured["relay_ws_url"].split("ticket=")[1].split("&")[0]
verified, reason = relay.verify_capability_ticket(ticket, required_scope=domain.HOST_TUNNEL_SCOPE)
ok(verified is not None and domain.HOST_TUNNEL_SCOPE in (verified.get("scopes") or []),
   "the minted ticket actually carries the host_tunnel scope")
ok(not (set(verified.get("scopes") or []) & domain.BROWSER_CAPABILITY_SCOPES),
   "the minted ticket carries no browser scopes (BUG-74 shape: host and "
   "browser tickets are never interchangeable)")

session2 = agent_host._ensure_host_bridge(**kwargs)
ok(session2 is session1, "a second open() call against a healthy bridge is a no-op "
   "(no duplicate host tunnel from repeated poll-loop iterations)")
ok(_captured["calls"] == 1, "open_host_bridge is called exactly once across two open() calls")

agent_host._drop_host_bridge("run_ui24")
ok(session1.stopped, "_drop_host_bridge stops the underlying bridge")
ok("run_ui24" not in agent_host._HOST_BRIDGES, "_drop_host_bridge clears the registry entry")

session3 = agent_host._ensure_host_bridge(**kwargs)
ok(session3 is not session1, "a dropped bridge is replaced by a fresh one on the next open()")
ok(_captured["calls"] == 2, "the replacement actually re-opened a new host tunnel")

agent_host._drop_host_bridge("run_ui24")
agent_host._drop_host_bridge("does-not-exist")  # must not raise

print(f"\nUI-24 host bridge wiring: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
