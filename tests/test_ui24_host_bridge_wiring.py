#!/usr/bin/env python3
"""UI-24: host-side relay bridge wiring.

Before this, RelayHub.attach_host() was only ever called from tests — nothing
in production opened the host side of the ADAPTER-22 relay, so a browser
could hold a perfectly valid ticket to a session with no bytes ever arriving.
Two layers are tested:

  Tier A - HostTunnelConnection against a real, minimal in-process
  websockets.serve() loopback server. Unlike the rest of this repo's PTY
  relay tests (which exercise RelayHub's pure authorization logic via direct
  calls), this class *is* a network transport, so a fake stands a real risk
  of masking a threading/asyncio bug a real socket would catch. The server
  is bare websockets.serve() - no HTTP/auth/FastAPI stack - fast and
  deterministic, not the kind of heavy integration harness this codebase
  otherwise avoids.

  Tier B - agent_host._ensure_host_bridge's ticket/URL/registry logic, via
  direct calls with open_host_bridge stubbed out - the same no-socket style
  as tests/test_adapter22_browser_pty_relay.py and test_bug74_*.py.
"""
from __future__ import annotations

import asyncio
import http.server
import json
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

# agent_host.py puts adapters/ on sys.path as a side effect of import (its own
# module-level shim), which is what makes the codex.* imports below resolve -
# same pattern as test_adapter22_browser_pty_relay.py, no test-local sys.path
# mutation (ARCH-MS-14 forbids those in tests/).
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
        await ws.send(json.dumps({"echo": message}))


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
    conn.send(json.dumps({"type": "output", "data_b64": "aGVsbG8="}))
except Exception:
    send_raised = True
ok(not send_raised, "send() delivers a frame cross-thread via run_coroutine_threadsafe")
time.sleep(0.3)
ok(len(_received) == 1, "the server actually received the frame")
ok(bool(conn_frames) and json.loads(conn_frames[0]).get("echo"),
   "on_frame fires for server -> client messages (this is how relay control "
   "frames reach LocalPtyRelayBridge.handle_control_frame)")

conn.stop(timeout=5)
ok(not conn.connected, "stop() tears the connection down")
raised_after_stop = False
try:
    conn.send(json.dumps({"type": "input"}))
except Exception:
    raised_after_stop = True
ok(raised_after_stop,
   "send() after stop() raises (UI-24 review fix) instead of swallowing the "
   "failure - LocalPtyRelayBridge.on_bytes relies on this exception to "
   "detect a dead tunnel and stop its pump thread")

# ── Self-join + swallowed-exception regression: on_close firing from the ──
# ── pump thread must not deadlock/leak, and a dead send must reach it ──
from codex.pty_relay_bridge import LocalPtyRelayBridge, wait_until  # noqa: E402


class _OneShotStreamHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"hello-from-local-pty"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):  # noqa: D401 - silence per-request logging
        pass


_stream_httpd = http.server.HTTPServer(("127.0.0.1", 0), _OneShotStreamHandler)
_stream_httpd_thread = threading.Thread(target=_stream_httpd.serve_forever, daemon=True)
_stream_httpd_thread.start()
_stream_port = _stream_httpd.server_address[1]

_bridge_events = []
_bridge_holder = {}


def _raising_send(frame):
    # Simulates a dead host-tunnel WebSocket: HostTunnelConnection.send()
    # now raises on failure instead of swallowing it (see above).
    raise ConnectionError("simulated dead host tunnel")


def _self_join_on_close(reason):
    _bridge_events.append(("on_close", reason))
    # Mirrors the real production chain this bug was found in: on_close ->
    # _drop_host_bridge -> HostBridgeSession.stop() -> bridge.stop() --
    # executing ON the pump thread itself, since on_close fires from inside
    # _run(). Before the fix this raised RuntimeError('cannot join current
    # thread'), silently swallowed by the real caller, so
    # HostTunnelConnection.stop() (which actually closes the WS) never ran.
    _bridge_holder["bridge"].stop()
    _bridge_events.append(("stop_returned", None))


_bridge_holder["bridge"] = LocalPtyRelayBridge(
    stream_url=f"http://127.0.0.1:{_stream_port}/",
    control_url="http://127.0.0.1:1/control",
    control_ticket="unused",
    send_to_relay=_raising_send,
    on_close=_self_join_on_close,
)
_bridge_holder["bridge"].start()
ok(wait_until(lambda: len(_bridge_events) >= 2, timeout=5),
   "a raising send_to_relay propagates through on_bytes so the pump thread "
   "stops itself and on_close fires (UI-24 review fix: send() used to "
   "swallow exceptions and return False, making this dead code)")
ok(_bridge_events == [("on_close", "eof"), ("stop_returned", None)],
   "on_close's self-triggered stop() returns cleanly instead of raising on "
   "a self-join (UI-24 review fix)")
ok(not _bridge_holder["bridge"].is_alive(),
   "the pump thread has actually exited after the self-triggered stop() - "
   "no leaked thread/connection on a normal (non-kill) session end")

_stream_httpd.shutdown()

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
    _captured["calls"] = _captured.get("calls", 0) + 1
    return session


ws_client_module.open_host_bridge = _fake_open_host_bridge

binding = _binding("run_ui24")
kwargs = dict(
    runner_session_id="run_ui24",
    host_id="host/ui24",
    binding=binding,
    local_stream_url="http://127.0.0.1:9/runner/v1/sessions/run_ui24/stream?ticket=x",
    stream_bind="127.0.0.1",
    stream_port=9,
    public_base="https://plan.example",
)

session1 = agent_host._ensure_host_bridge(**kwargs)
ok(session1 is _captured["session"], "_ensure_host_bridge opens a bridge on first call")
ok("/pty/host?" in _captured["relay_ws_url"] and "host_id=host" in _captured["relay_ws_url"],
   "the host bridge connects to /pty/host with host_id bound in the URL, "
   "distinct from the browser's /pty path")

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
