#!/usr/bin/env python3
"""SIMPLIFY-9: one binary session transport (browser ↔ hub ↔ host executor).

Acceptance-focused contract tests:
- binary encode/decode for ready/exit/out/in/resize/signal/snapshot
- no data_b64 / JSON text frames on the new encode path
- host disconnect detaches only; host can reattach
- backpressure slows/signals without disconnecting browsers
- host open path does not require LocalPtyRelayBridge / localhost stream
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = Path(tempfile.mkdtemp(prefix="simplify9-"))
os.environ["PM_RUNNER_PTY_RELAY_SECRET"] = "simplify9-relay-secret"
os.environ["PM_RUNNER_STREAM_SECRET"] = "simplify9-stream-secret"
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_SWITCHBOARD_PUBLIC_BASE"] = "https://plan.example"

from adapters import agent_host  # noqa: E402
import store  # noqa: E402
from switchboard.application import runner_pty_relay as relay  # noqa: E402
from switchboard.application.commands import runner_control  # noqa: E402
from switchboard.application.commands import runner_pty as runner_pty_command  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402
from switchboard.domain import runner_pty as domain  # noqa: E402
import codex.pty_host_ws_client as ws_client_module  # noqa: E402

store.init_db("switchboard")

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def _binding(session_id: str, **overrides):
    base = {
        "tenant_id": "tenant/t1",
        "user_id": "user/u1",
        "project_id": "switchboard",
        "task_id": "SIMPLIFY-9",
        "claim_id": "claim-s9",
        "work_session_id": "ws-s9",
        "runner_session_id": session_id,
        "host_id": "host/s9",
        "wake_id": "wake-s9",
        "execution_connection_id": "execconn/s9",
        "source_sha": "deadbeef",
        "permission_profile": "operator_watch",
    }
    base.update(overrides)
    return base


relay.clear_revoked_jtis_for_tests()
hub = relay.reset_default_hub_for_tests()

# ── Binary frame codec ──────────────────────────────────────────────────────
EXPECTED_TYPES = ("ready", "exit", "out", "in", "resize", "signal", "snapshot")
ok(set(EXPECTED_TYPES).issubset(set(domain.FRAME_TYPES)),
   "FRAME_TYPES includes the six SIMPLIFY-9 message kinds")

samples = [
    ("ready", {"pid": 42}, None),
    ("exit", {"reason": "done", "code": 0}, None),
    ("out", {}, b"\x1b[31mRED\x1b[0m"),
    ("in", {}, b"hello\r"),
    ("resize", {"rows": 40, "cols": 120}, None),
    ("signal", {"name": "SIGINT"}, None),
    ("snapshot", {}, b"\x1b[H\x1b[2Jscreen"),
]
roundtrip_ok = True
for kind, payload, data in samples:
    encoded = domain.encode_frame(kind, payload, data=data)
    if not isinstance(encoded, (bytes, bytearray)):
        roundtrip_ok = False
        break
    if not bytes(encoded).startswith(b"SB1\0"):
        roundtrip_ok = False
        break
    decoded = domain.decode_frame(encoded)
    if decoded.get("type") != kind:
        roundtrip_ok = False
        break
    if data is not None and decoded.get("data") != data:
        roundtrip_ok = False
        break
    for key, value in payload.items():
        if decoded.get(key) != value:
            roundtrip_ok = False
            break
ok(roundtrip_ok, "binary encode/decode roundtrip for out/in/resize/signal/ready/exit/snapshot")

out_frame = domain.encode_frame("out", {"seq": 1}, data=b"abc")
ok(isinstance(out_frame, (bytes, bytearray)) and b"data_b64" not in out_frame,
   "new encode path is binary bytes with no data_b64")
ok(b'"type"' not in out_frame or out_frame.startswith(b"SB1\0"),
   "new encode path is not a JSON text WebSocket frame")
# Wire header shape: magic + type_id + header_len + data_len
ok(len(out_frame) >= 11 and out_frame[4] == domain.TYPE_IDS["out"],
   "wire header carries magic + type_id for out")

# Legacy names decode-only (migration helpers); production encode rejects them.
legacy_ok = True
try:
    domain.encode_frame("output", data=b"x")
    legacy_ok = False
except ValueError:
    pass
# If a legacy JSON helper exists, decode may map; binary path must not emit old names.
ok(legacy_ok, "production encode rejects legacy type names (output/input/...)")

# Starting reserves the exact execution id and attach capability before the
# runner row exists. The eventual host uses the same deterministic id.
planned_sid = domain.planned_runner_session_id("wake-s9-start", "host/s9-start")
pending_ticket = runner_pty_command.mint_ticket_for_pending_direct_session(
    runner_session_id=planned_sid,
    task_id="SIMPLIFY-9",
    wake_id="wake-s9-start",
    host_id="host/s9-start",
    project="switchboard",
    user_id="user/s9",
    scopes=["watch", "input", "resize", "signal"],
)
ok(planned_sid.startswith("run_") and pending_ticket.get("runner_session_id") == planned_sid,
   "Start and host derive one deterministic execution id")
ok(pending_ticket.get("pending") is True and pending_ticket.get("relay_url")
   and pending_ticket.get("ticket"),
   "pending Start returns session id + browser relay ticket immediately")
ok((pending_ticket.get("binding") or {}).get("claim_id") == f"pending/{planned_sid}"
   and (pending_ticket.get("binding") or {}).get("permission_profile")
   == "operator_watch_pending",
   "pending browser bind is explicit and restricted to the reserved execution")

task_id = store.create_task(
    {"workstream_id": "SIMPLIFY", "title": "SIMPLIFY-9 pending start fixture"},
    actor="simplify9-test", project="switchboard")["task_id"]
start_result = task_execution.start_task(
    task_id,
    project="switchboard",
    actor="simplify9-test",
    principal_id="user/s9",
    launcher=lambda *_args, **_kwargs: {
        "action": "started", "started": True, "attached": False,
        "runner_session_id": planned_sid,
        "wake_id": "wake-s9-start", "host_id": "host/s9-start",
    },
)
ok(start_result.get("execution_id") == planned_sid
   and start_result.get("relay_url") and start_result.get("ticket"),
   "task Start response opens the reserved browser attachment without polling")
reservation_hub = relay.RelayHub()
pending_payload, pending_reason = relay.verify_capability_ticket(
    str(pending_ticket.get("ticket") or ""), required_scope="watch")
reservation_browser = reservation_hub.attach_browser(
    planned_sid, pending_payload or {}, lambda _frame: True,
    client_id="pending-browser")
queued_before_host = reservation_hub.route_browser_to_host(
    planned_sid, "pending-browser", domain.encode_frame("in", data=b"queued-input"))
actual_binding = _binding(
    planned_sid,
    task_id="SIMPLIFY-9",
    host_id="host/s9-start",
    wake_id="wake-s9-start",
    claim_id="claim-s9-actual",
    work_session_id="ws-s9-actual",
)
_, actual_host_payload = relay.mint_host_tunnel_ticket(
    actual_binding, ttl_seconds=120)
reserved_host_frames: list[bytes] = []
reservation_host = reservation_hub.attach_host(
    planned_sid, lambda frame: reserved_host_frames.append(frame) or True,
    binding=actual_host_payload)
reservation_hub.detach_browser(planned_sid, "pending-browser")
reservation_reconnect = reservation_hub.attach_browser(
    planned_sid, pending_payload or {}, lambda _frame: True,
    client_id="pending-browser-reconnect")
ok(not pending_reason and reservation_browser.get("ok")
   and reservation_host.get("ok") and reservation_reconnect.get("ok"),
   "reserved Watch attach upgrades to the exact host claim and can reconnect")
ok(queued_before_host.get("ok") and any(
    domain.decode_frame(frame).get("data") == b"queued-input"
    for frame in reserved_host_frames),
   "hub buffers browser input until the reserved host dials in")

# ── Hub: host disconnect ≠ session close; host can reattach ─────────────────
hub = relay.reset_default_hub_for_tests()
host_frames: list[bytes] = []
browser_frames: list[bytes] = []
host_ticket, host_payload = relay.mint_host_tunnel_ticket(
    _binding("run_s9_reattach"), ttl_seconds=120)
browser_ticket, browser_payload = relay.mint_capability_ticket(
    _binding("run_s9_reattach"), ["watch", "input", "resize", "signal"], ttl_seconds=120)

att1 = hub.attach_host(
    "run_s9_reattach",
    lambda f: host_frames.append(f),
    binding=host_payload,
)
ok(att1.get("ok") is True, "host attaches with host_tunnel ticket")
att_b = hub.attach_browser(
    "run_s9_reattach", browser_payload, lambda f: browser_frames.append(f),
    client_id="browser-s9")
ok(att_b.get("ok") is True, "browser attaches while host is live")

hub.route_host_to_browsers(
    "run_s9_reattach", domain.encode_frame("out", data=b"before-detach\n"))
hub.detach_host("run_s9_reattach")
info = hub.session_info("run_s9_reattach")
ok(info is not None and info.get("closed") is False
   and info.get("host_attached") is False,
   "host disconnect detaches host but does not close the hub session")
ok(info.get("browser_count", 0) >= 1,
   "browser stays attached after host disconnect")

# Host can reattach (symmetric reconnect).
att2 = hub.attach_host(
    "run_s9_reattach",
    lambda f: host_frames.append(f),
    binding=host_payload,
)
ok(att2.get("ok") is True, "host can reattach after detach")
info2 = hub.session_info("run_s9_reattach")
ok(info2 is not None and info2.get("host_attached") is True
   and info2.get("closed") is False,
   "reattached host restores host_attached without a new session")

# Buffered output before host dial-in: session exists, browser watches, then host joins.
hub2 = relay.reset_default_hub_for_tests()
early_browser: list[bytes] = []
_, early_browser_payload = relay.mint_capability_ticket(
    _binding("run_s9_buffer"), ["watch"], ttl_seconds=120)
hub2.ensure_session("run_s9_buffer", _binding("run_s9_buffer"))
hub2.attach_browser(
    "run_s9_buffer", early_browser_payload, lambda f: early_browser.append(f),
    client_id="early")
# Host dials in later and publishes — browser receives.
_, early_host_payload = relay.mint_host_tunnel_ticket(
    _binding("run_s9_buffer"), ttl_seconds=120)
hub2.attach_host(
    "run_s9_buffer", lambda f: None, binding=early_host_payload)
hub2.route_host_to_browsers(
    "run_s9_buffer", domain.encode_frame("out", data=b"late-host\n"))
saw_late = False
for raw in early_browser:
    try:
        fr = domain.decode_frame(raw)
    except Exception:
        continue
    if fr.get("type") == "out" and fr.get("data") == b"late-host\n":
        saw_late = True
ok(saw_late, "Watch is attach; hub delivers once host dials in")

# ── Hub: backpressure slows/signals without disconnecting browser ───────────
tiny = relay.RelayHub(browser_queue_limit=2)
_, bp_host_payload = relay.mint_host_tunnel_ticket(
    _binding("run_s9_bp"), ttl_seconds=120)
_, bp_browser_payload = relay.mint_capability_ticket(
    _binding("run_s9_bp"), ["watch"], ttl_seconds=120)
bp_browser_frames: list[bytes] = []
block_sends = {"n": 0}


def _slow_browser_send(frame: bytes) -> None:
    block_sends["n"] += 1
    # First two deliveries succeed; further attempts raise to simulate a stuck client.
    if block_sends["n"] > 2:
        raise BlockingIOError("browser_outbound_full")
    bp_browser_frames.append(frame)


tiny.attach_host("run_s9_bp", lambda f: None, binding=bp_host_payload)
tiny.attach_browser(
    "run_s9_bp", bp_browser_payload, _slow_browser_send, client_id="slow")

bp_results = []
for i in range(6):
    bp_results.append(tiny.publish_output("run_s9_bp", f"flood-{i}\n".encode()))

info_bp = tiny.session_info("run_s9_bp")
ok(info_bp is not None and info_bp.get("browser_count", 0) >= 1
   and info_bp.get("closed") is False,
   "backpressure keeps the browser attached (no silent disconnect)")
ok(any(r.get("backpressure") for r in bp_results),
   "backpressure is signaled on hub publish results so host can slow reads")

# The frame that crosses the high-water mark is retained, not used as a drop
# signal. Once the browser writer drains, flush_browser delivers it in order.
lossless = relay.RelayHub(browser_queue_limit=1)
_, lossless_host = relay.mint_host_tunnel_ticket(
    _binding("run_s9_lossless"), ttl_seconds=120)
_, lossless_browser = relay.mint_capability_ticket(
    _binding("run_s9_lossless"), ["watch"], ttl_seconds=120)
accepting = {"value": False}
lossless_frames: list[bytes] = []


def _toggle_send(frame: bytes) -> bool:
    if not accepting["value"]:
        return False
    lossless_frames.append(frame)
    return True


lossless.attach_host("run_s9_lossless", lambda _f: True, binding=lossless_host)
lossless.attach_browser(
    "run_s9_lossless", lossless_browser, _toggle_send, client_id="lossless")
lossless.publish_output("run_s9_lossless", b"must-not-drop")
paused_before = lossless.host_should_pause("run_s9_lossless")
accepting["value"] = True
flushed = lossless.flush_browser("run_s9_lossless", "lossless")
decoded_lossless = [domain.decode_frame(frame) for frame in lossless_frames]
ok(paused_before and flushed and any(
    frame.get("data") == b"must-not-drop" for frame in decoded_lossless),
   "backpressure retains the boundary frame and delivers it after drain")

# Registration/heartbeat is the host ticket renewal path. These capabilities
# are returned ephemerally and not written into session metadata.
registered = runner_control.upsert_session_mapping_result({
    "project": "switchboard",
    "runner_session_id": "run_s9_registered",
    "host_id": "host/s9",
    "agent_id": "codex/SIMPLIFY-9",
    "runtime": "codex",
    "task_id": "SIMPLIFY-9",
    "status": "running",
    "control": {"managed_process": True, "runner_open": True},
    "metadata": {
        "wake_id": "wake-s9",
        "direct_assignment": True,
        "native_host_execution": True,
        "assignment_schema": "switchboard.direct_cli_assignment.v1",
        "source_sha": "direct/run_s9_registered",
        "execution_connection_id": "direct/run_s9_registered",
    },
}, actor="host/s9", principal_id="agent-host/s9")
server_relay = registered.get("server_relay") or {}
ok("/pty/host" in str(server_relay.get("host_url") or "")
   and "/pty?" in str(server_relay.get("browser_url") or ""),
   "host registration returns fresh ephemeral host/browser relay capabilities")

# Vendor cloud sessions remain job APIs and cannot be mistaken for xterms.
store.upsert_runner_session({
    "runner_session_id": "run_s9_vendor",
    "host_id": "vendor/claude",
    "agent_id": "claude/SIMPLIFY-9",
    "runtime": "claude-cloud",
    "task_id": "SIMPLIFY-9",
    "claim_id": "claim-vendor",
    "status": "running",
    "metadata": {"vendor_id": "job-123", "wake_id": "wake-vendor",
                 "work_session_id": "ws-vendor"},
}, actor="simplify9-test", project="switchboard")
vendor_ticket = runner_pty_command.mint_ticket_for_session(
    runner_session_id="run_s9_vendor", project="switchboard", scopes=["watch"])
ok(vendor_ticket.get("error") == "vendor_cloud_job_api_not_pty",
   "vendor cloud job sessions are explicitly refused as xterms")

# ── Host open path: no LocalPtyRelayBridge / localhost stream required ──────
sig = inspect.signature(ws_client_module.open_host_bridge)
params = set(sig.parameters)
ok("local_stream_url" not in params and "local_control_url" not in params,
   "open_host_bridge no longer requires localhost stream/control URLs")
src = Path(ws_client_module.__file__).read_text(encoding="utf-8")
ok("from adapters.codex.pty_relay_bridge" not in src
   and "from codex.pty_relay_bridge" not in src
   and "import pty_relay_bridge" not in src,
   "host WS client is not built around LocalPtyRelayBridge")

supervisor_src = (ROOT / "adapters" / "codex" / "supervisor.py").read_text(
    encoding="utf-8")
executor_src = (ROOT / "adapters" / "codex" / "pty_stream.py").read_text(
    encoding="utf-8")
ok("pty.openpty()" not in supervisor_src and "pty.openpty()" in executor_src
   and "--child-command-json" in supervisor_src,
   "one executor process owns openpty + child spawn on Mac/AWS hosts")
ok("PM_AGENT_HOST_PLATFORM" in executor_src and "target_label" in executor_src,
   "Mac/AWS share the same executor code and differ only by target label")

# _ensure_host_bridge must work without local_stream_url / stream port companion.
_captured = {}


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


def _fake_open_host_bridge(*, runner_session_id, relay_ws_url, **kw):
    _captured["session"] = _FakeSession(runner_session_id, relay_ws_url, **kw)
    _captured["kw"] = kw
    _captured["relay_ws_url"] = relay_ws_url
    _captured["calls"] = _captured.get("calls", 0) + 1
    return _captured["session"]


ws_client_module.open_host_bridge = _fake_open_host_bridge
agent_host._HOST_BRIDGES.clear()

# New contract: no local_stream_url / stream_port required for host tunnel open.
ensure_sig = inspect.signature(agent_host._ensure_host_bridge)
ensure_params = set(ensure_sig.parameters)
ok("local_stream_url" not in ensure_params or ensure_sig.parameters[
    "local_stream_url"].default is not inspect.Parameter.empty,
   "_ensure_host_bridge does not require local_stream_url")

bridge_kwargs = dict(
    runner_session_id="run_s9_open",
    host_id="host/s9",
    binding=_binding("run_s9_open"),
    public_base="https://plan.example",
)
# Prefer calling without stream companion args when signature allows.
call_kwargs = {k: v for k, v in bridge_kwargs.items() if k in ensure_params}
# Optional leftovers with defaults
for optional in ("local_stream_url", "stream_bind", "stream_port", "host_relay_url"):
    if optional in ensure_params and optional not in call_kwargs:
        param = ensure_sig.parameters[optional]
        if param.default is inspect.Parameter.empty and optional == "stream_bind":
            call_kwargs[optional] = "127.0.0.1"
        elif param.default is inspect.Parameter.empty and optional == "stream_port":
            call_kwargs[optional] = 0
        elif param.default is inspect.Parameter.empty and optional == "local_stream_url":
            # If still required, the test fails the acceptance contract below.
            call_kwargs[optional] = ""

opened = None
open_raised = False
try:
    opened = agent_host._ensure_host_bridge(**call_kwargs)
except TypeError:
    open_raised = True
ok(not open_raised and opened is _captured.get("session"),
   "host path opens without requiring localhost stream for Watch/open")
ok("local_stream_url" not in (_captured.get("kw") or {})
   or not str((_captured.get("kw") or {}).get("local_stream_url") or "").strip(),
   "open_host_bridge is not passed a localhost stream URL")
ok("/pty/host" in str(_captured.get("relay_ws_url") or ""),
   "host bridge still dials the Switchboard /pty/host tunnel")

agent_host._drop_host_bridge("run_s9_open")
ws_client_module.open_host_bridge = getattr(
    ws_client_module, "_simplify9_orig_open_host_bridge",
    ws_client_module.open_host_bridge)

# API router: host WebSocketDisconnect must detach, not close_session.
router_src = (
    ROOT / "src" / "switchboard" / "api" / "routers" / "runner_pty.py"
).read_text(encoding="utf-8")
ok("close_session(runner_session_id, reason=\"host_disconnect\")" not in router_src
   and "detach_host" in router_src
   and ("send_bytes" in router_src or "receive_bytes" in router_src),
   "API host path detaches on disconnect and uses binary WS frames")

print(f"\nSIMPLIFY-9 single session transport: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
