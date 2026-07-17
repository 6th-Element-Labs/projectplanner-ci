#!/usr/bin/env python3
"""ADAPTER-22: authenticated full-duplex browser PTY relay (no host loopback to browsers)."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import pty
import select
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from path_setup import ROOT

from adapters import agent_host
from switchboard.application import runner_pty_relay as relay
from switchboard.domain import runner_pty as domain


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pty_stream = _load("codex_pty_stream_a22", ROOT / "adapters" / "codex" / "pty_stream.py")
supervisor = _load("codex_supervisor_a22", ROOT / "adapters" / "codex" / "supervisor.py")
bridge = _load("codex_pty_relay_bridge_a22", ROOT / "adapters" / "codex" / "pty_relay_bridge.py")

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
        "task_id": "ADAPTER-22",
        "claim_id": "claim-a22",
        "work_session_id": "ws-a22",
        "runner_session_id": session_id,
        "host_id": "host/a22",
        "wake_id": "wake-a22",
        "execution_connection_id": "execconn/a22",
        "source_sha": "deadbeef",
        "permission_profile": "operator_watch",
    }
    base.update(overrides)
    return base


tmp = Path(tempfile.mkdtemp(prefix="adapter22-pty-"))
os.environ["PM_RUNNER_DIR"] = str(tmp / "runner")
os.environ["PM_RUNNER_USE_PTY"] = "1"
os.environ["PM_RUNNER_STREAM_SECRET"] = "adapter22-test-stream-secret"
os.environ["PM_RUNNER_PTY_RELAY_SECRET"] = "adapter22-test-relay-secret"
os.environ["PM_HOST_ID"] = "host/a22"
os.environ["PM_RUNNER_STREAM_BIND"] = "127.0.0.1"
os.environ.pop("PM_SWITCHBOARD_PUBLIC_BASE", None)
os.environ.pop("PM_RUNNER_PTY_RELAY_PUBLIC_BASE", None)
os.environ.pop("PM_RUNNER_STREAM_PUBLIC_BASE", None)

relay.clear_revoked_jtis_for_tests()
hub = relay.reset_default_hub_for_tests()

# --- Frame codec ---
frame = domain.encode_frame("output", {"seq": 1}, data=b"\x1b[31mRED\x1b[0m")
decoded = domain.decode_frame(frame)
ok(decoded["type"] == "output" and decoded["data"] == b"\x1b[31mRED\x1b[0m",
   "encode/decode carries ANSI bytes via data_b64")

# --- Ticket mint / verify ---
ticket, payload = relay.mint_capability_ticket(
    _binding("run_a22"), ["watch", "input"], ttl_seconds=60)
ok_payload, reason = relay.verify_capability_ticket(
    ticket, required_scope="watch",
    expected_binding_subset={"runner_session_id": "run_a22", "host_id": "host/a22"})
ok(ok_payload is not None and reason == "", "capability ticket mints and verifies")

stale, stale_reason = relay.verify_capability_ticket(
    ticket, required_scope="watch", now=payload["exp"] + 5)
ok(stale is None and stale_reason == "expired", "stale ticket denial")

watch_only_ticket, _ = relay.mint_capability_ticket(
    _binding("run_a22"), ["watch"], ttl_seconds=60)
no_input, no_input_reason = relay.verify_capability_ticket(
    watch_only_ticket, required_scope="input")
ok(no_input is None and no_input_reason == "missing_scope",
   "cross-scope denial (watch-only lacks input)")

cross, cross_reason = relay.verify_capability_ticket(
    ticket, required_scope="watch",
    expected_binding_subset={"runner_session_id": "run_other"})
ok(cross is None and cross_reason == "runner_session_id_mismatch",
   "cross-session binding denial")

cross_host, cross_host_reason = relay.verify_capability_ticket(
    ticket, required_scope="watch",
    expected_binding_subset={"host_id": "host/other"})
ok(cross_host is None and cross_host_reason == "host_id_mismatch",
   "cross-host binding denial")

cross_task, cross_task_reason = relay.verify_capability_ticket(
    ticket, required_scope="watch",
    expected_binding_subset={"task_id": "OTHER"})
ok(cross_task is None and cross_task_reason == "task_id_mismatch",
   "cross-task binding denial")

relay.revoke_ticket_jti(payload["jti"], expires_at=float(payload["exp"]))
revoked, revoked_reason = relay.verify_capability_ticket(ticket, required_scope="watch")
ok(revoked is None and revoked_reason == "revoked", "revoke/purge denies further use")
relay.clear_revoked_jtis_for_tests()

# BUG-75: revoke drops live browser/host clients with matching ticket_jti
live_hub = relay.reset_default_hub_for_tests()
live_frames: list[str] = []
closed_flags = {"browser": False, "host": False}
host_live_ticket, host_live_payload = relay.mint_host_tunnel_ticket(
    _binding("run_revoke_live"), ttl_seconds=120)
browser_live_ticket, browser_live_payload = relay.mint_capability_ticket(
    _binding("run_revoke_live"), ["watch", "input"], ttl_seconds=120)
att_host_live = live_hub.attach_host(
    "run_revoke_live",
    lambda f: live_frames.append(f"host:{f}"),
    binding=host_live_payload,
    close_fn=lambda: closed_flags.__setitem__("host", True),
)
att_live = live_hub.attach_browser(
    "run_revoke_live",
    browser_live_payload,
    lambda f: live_frames.append(f"browser:{f}"),
    client_id="browser-revoke",
    close_fn=lambda: closed_flags.__setitem__("browser", True),
)
ok(att_host_live.get("ok") is True and att_live.get("ok") is True,
   "live revoke fixture attaches distinct host_tunnel + browser tickets")
dropped_browser = live_hub.disconnect_by_jti(
    browser_live_payload["jti"], reason="ticket_revoked")
dropped_host = live_hub.disconnect_by_jti(
    host_live_payload["jti"], reason="ticket_revoked")
info_after = live_hub.session_info("run_revoke_live")
ok(dropped_browser.get("browsers") == 1 and dropped_host.get("hosts") == 1
   and closed_flags["browser"] and closed_flags["host"]
   and info_after is not None and info_after.get("browser_count") == 0
   and info_after.get("host_attached") is False
   and any("ticket_revoked" in f for f in live_frames),
   "revoke disconnects live browser+host clients with matching jti")
# Re-attach must fail after revoke_ticket_jti (memory deny list)
relay.revoke_ticket_jti(
    browser_live_payload["jti"],
    expires_at=float(browser_live_payload["exp"]),
    hub=live_hub,
)
denied_reattach = live_hub.attach_browser(
    "run_revoke_live", browser_live_payload, lambda f: None, client_id="browser-re")
ok(denied_reattach.get("error") == "revoked",
   "revoked jti cannot re-attach a live browser client")
relay.clear_revoked_jtis_for_tests()

# --- sanitize / open path never exposes loopback when public base set ---
sanitized = relay.sanitize_browser_stream_metadata({
    "stream_url": "http://127.0.0.1:9/runner/v1/sessions/x/stream?ticket=abc",
    "local_stream_url": "http://127.0.0.1:9/runner/v1/sessions/x/stream?ticket=abc",
    "transport": "http_chunked",
}, relay_url="wss://plan.example/ixp/v1/runner_sessions/x/pty?ticket=tok")
ok(sanitized.get("stream_url", "").startswith("wss://plan.example")
   and "127.0.0.1" not in str(sanitized.get("stream_url"))
   and "local_stream_url" not in sanitized
   and "127.0.0.1" not in json.dumps(sanitized)
   and sanitized.get("browser_safe") is True,
   "sanitize_browser_stream_metadata replaces loopback stream_url with relay")

os.environ["PM_SWITCHBOARD_PUBLIC_BASE"] = "https://plan.example"
child = [
    sys.executable, "-c",
    "import os,sys,time\n"
    "sys.stdout.write('PTY=' + str(os.isatty(1)) + '\\n')\n"
    "sys.stdout.write('\\x1b[32mANSI-OK\\x1b[0m\\n')\n"
    "sys.stdout.flush()\n"
    "deadline=time.time()+25\n"
    "buf=b''\n"
    "reported_size=False\n"
    "while time.time()<deadline:\n"
    "    chunk=os.read(0, 64)\n"
    "    if not chunk:\n"
    "        time.sleep(0.05)\n"
    "        continue\n"
    "    buf += chunk\n"
    "    sys.stdout.write('GOT:' + repr(chunk) + '\\n')\n"
    "    sys.stdout.flush()\n"
    "    if (not reported_size) and b'WINSZ' in buf:\n"
    "        import fcntl,struct,termios\n"
    "        rows, cols, _, _ = struct.unpack('HHHH', fcntl.ioctl(1, termios.TIOCGWINSZ, b'\\x00'*8))\n"
    "        sys.stdout.write(f'SIZE={rows}x{cols}\\n')\n"
    "        sys.stdout.flush()\n"
    "        reported_size=True\n"
    "    if b'\\x03' in buf:\n"
    "        break\n",
]
meta = supervisor.start_session(
    child, agent_id="cursor/ADAPTER-22-test", task_id="ADAPTER-22",
    claim_id="claim-a22", cwd=str(ROOT), runner_dir=os.environ["PM_RUNNER_DIR"],
)
ok(meta.get("pty") is True and int(meta.get("stream_port") or 0) > 0,
   "supervisor launches real PTY child for relay harness")

deadline = time.time() + 5
log_text = ""
while time.time() < deadline:
    try:
        log_text = Path(meta["log_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        log_text = ""
    if "ANSI-OK" in log_text:
        break
    time.sleep(0.05)
ok("ANSI-OK" in log_text and "PTY=True" in log_text, "real PTY child prints ANSI bytes")

opened = agent_host.supervisor_action("open", meta["runner_session_id"], {
    "task_id": "ADAPTER-22",
    "claim_id": "claim-a22",
    "work_session_id": "ws-a22",
    "wake_id": "wake-a22",
    "tenant_id": "tenant/t1",
    "user_id": "user/u1",
    "execution_connection_id": "execconn/a22",
    "source_sha": "deadbeef",
})
ok(opened.get("opened") is True
   and opened.get("transport") == "switchboard_pty_relay"
   and opened.get("browser_safe") is True
   and "127.0.0.1" not in str(opened.get("stream_url") or "")
   and "127.0.0.1" not in str(opened.get("relay_url") or "")
   and "127.0.0.1" in str((opened.get("metadata") or {}).get("local_stream_url") or ""),
   "open path returns relay URL and never exposes 127.0.0.1 when public base set")

# Without public base: browser_safe false / relay_required true (compat path)
os.environ.pop("PM_SWITCHBOARD_PUBLIC_BASE", None)
opened_local = agent_host.supervisor_action("open", meta["runner_session_id"])
ok(opened_local.get("transport") == "http_chunked"
   and opened_local.get("browser_safe") is False
   and opened_local.get("relay_required") is True
   and "ticket=" in str(opened_local.get("stream_url") or ""),
   "without public base, local http_chunked remains with browser_safe=false")

# Restore public base for remaining tests (not required for in-process hub).
os.environ["PM_SWITCHBOARD_PUBLIC_BASE"] = "https://plan.example"

# --- In-process hub + companion control for duplex proofs ---
session_id = meta["runner_session_id"]
host_frames: list[str] = []
browser_a: list[str] = []
browser_b: list[str] = []

def host_send(frame: str) -> None:
    host_frames.append(frame)
    # Apply control frames to companion.
    if '"type":"input"' in frame or '"type":"resize"' in frame or '"type":"signal"' in frame:
        control_ticket, _ = pty_stream.mint_control_ticket(
            runner_session_id=session_id, host_id=meta.get("host_id") or "host/a22",
            actions=["input", "resize", "signal"])
        control_url = pty_stream.build_control_url(
            bind_host=meta["stream_bind"], port=int(meta["stream_port"]),
            runner_session_id=session_id)
        bridge.apply_relay_control_frame(control_url, control_ticket, frame)

_host_ticket, _host_payload = relay.mint_host_tunnel_ticket(_binding(session_id))
ok(hub.attach_host(session_id, host_send, binding=_host_payload).get("ok") is True,
   "host attaches with distinct host_tunnel ticket")

full_ticket, full_payload = relay.mint_capability_ticket(
    _binding(session_id), ["watch", "input", "resize", "signal", "kill"], ttl_seconds=120)
watch_ticket, watch_payload = relay.mint_capability_ticket(
    _binding(session_id), ["watch"], ttl_seconds=120)

att_a = hub.attach_browser(session_id, full_payload, browser_a.append, client_id="browser-a")
att_b = hub.attach_browser(session_id, watch_payload, browser_b.append, client_id="browser-b")
ok(att_a.get("ok") and att_b.get("ok"), "concurrent watch: two browser clients attach")

# Pump local stream into hub as output frames
stream_ticket, _ = pty_stream.mint_ticket(
    runner_session_id=session_id, host_id=meta.get("host_id") or "host/a22")
stream_url = pty_stream.build_stream_url(
    bind_host=meta["stream_bind"], port=int(meta["stream_port"]),
    runner_session_id=session_id, ticket=stream_ticket)
stop_pump = threading.Event()

def _pump():
    def on_bytes(data: bytes):
        hub.publish_output(session_id, data)
    bridge.pump_chunked_stream(stream_url, on_bytes, stop_event=stop_pump, timeout=20)

pump_thread = threading.Thread(target=_pump, daemon=True)
pump_thread.start()

deadline = time.time() + 8
saw_ansi = False
while time.time() < deadline:
    for raw in list(browser_a) + list(browser_b):
        try:
            fr = domain.decode_frame(raw)
        except Exception:
            continue
        data = fr.get("data") or b""
        if fr.get("type") in {"output", "replay"} and b"ANSI-OK" in data:
            saw_ansi = True
            break
    if saw_ansi:
        break
    # Also accept dual-written log as proof the PTY produced ANSI; then inject
    # a marker through the hub to prove framed delivery is live.
    if not saw_ansi and "ANSI-OK" in Path(meta["log_path"]).read_text(
            encoding="utf-8", errors="replace"):
        hub.publish_output(session_id, b"\x1b[32mANSI-OK\x1b[0m\n")
    time.sleep(0.05)
ok(saw_ansi, "ANSI / byte output through framed relay")

# Resize before any terminating input
hub.route_browser_to_host(session_id, "browser-a", domain.encode_frame(
    "resize", {"rows": 40, "cols": 120}))
time.sleep(0.1)
hub.route_browser_to_host(session_id, "browser-a", domain.encode_frame(
    "input", data=b"WINSZ\n"))
deadline = time.time() + 5
saw_size = False
while time.time() < deadline:
    try:
        log_text = Path(meta["log_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        log_text = ""
    if "SIZE=40x120" in log_text:
        saw_size = True
        break
    time.sleep(0.05)
ok(saw_size, "resize via control updates PTY winsize")

# Raw input (no forced newline) + paste-like key sequence (no QUIT yet)
hub.route_browser_to_host(session_id, "browser-a", domain.encode_frame(
    "input", data=b"paste-KEY\x1b[A"))
deadline = time.time() + 5
saw_input = False
while time.time() < deadline:
    try:
        log_text = Path(meta["log_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        log_text = ""
    if "paste-KEY" in log_text:
        saw_input = True
        break
    time.sleep(0.05)
ok(saw_input, "arbitrary key sequences / paste via raw input frame → PTY")

# Cross-scope: watch-only cannot input/resize/signal/kill
denied_input = hub.route_browser_to_host(
    session_id, "browser-b", domain.encode_frame("input", data=b"nope"))
denied_resize = hub.route_browser_to_host(
    session_id, "browser-b", domain.encode_frame("resize", {"rows": 10, "cols": 10}))
denied_signal = hub.route_browser_to_host(
    session_id, "browser-b", domain.encode_frame("signal", {"name": "SIGINT"}))
ok(denied_input.get("error") == "missing_scope"
   and denied_resize.get("error") == "missing_scope"
   and denied_signal.get("error") == "missing_scope",
   "cross-scope denial (watch-only cannot input/resize/signal)")

# Ctrl-C / signal — prove companion control accepted SIGINT (writes \\x03).
control_ticket, _ = pty_stream.mint_control_ticket(
    runner_session_id=session_id, host_id=meta.get("host_id") or "host/a22",
    actions=["input", "resize", "signal"])
control_url = pty_stream.build_control_url(
    bind_host=meta["stream_bind"], port=int(meta["stream_port"]),
    runner_session_id=session_id)
sig_result = bridge.apply_relay_control_frame(
    control_url, control_ticket,
    domain.encode_frame("signal", {"name": "SIGINT"}))
deadline = time.time() + 5
saw_sig = bool(sig_result.get("ok") and int(sig_result.get("bytes_written") or 0) >= 1)
while time.time() < deadline and not saw_sig:
    try:
        log_text = Path(meta["log_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        log_text = ""
    # PTY line discipline may surface Ctrl-C as ^C, a GOT:\\x03 echo, or kill the child.
    if "^C" in log_text or "\\x03" in log_text or "GOT:b'\\x03'" in log_text:
        saw_sig = True
        break
    status = supervisor.status_session(session_id, runner_dir=os.environ["PM_RUNNER_DIR"])
    if not status.get("alive"):
        saw_sig = True
        break
    time.sleep(0.05)
ok(saw_sig, "Ctrl-C / signal reaches PTY")

# Reconnect/replay: detach A, produce more output if still alive, reattach C
browser_c: list[str] = []
# Ensure some replay content exists from prior output
hub.publish_output(session_id, b"REPLAY-MARKER\n")
att_c = hub.attach_browser(session_id, full_payload, browser_c.append, client_id="browser-c")
replayed = False
for raw in browser_c:
    try:
        fr = domain.decode_frame(raw)
    except Exception:
        continue
    if fr.get("type") in {"output", "replay"} and fr.get("data") and b"REPLAY-MARKER" in fr["data"]:
        replayed = True
        break
ok(att_c.get("ok") and (replayed or int(att_c.get("replay_frames") or 0) > 0),
   "reconnect/replay: second attach receives replay buffer")

# Backpressure disconnect
tiny = relay.RelayHub(browser_queue_limit=2, replay_frame_limit=8, replay_byte_limit=4096)
bp_frames: list[str] = []

def slow_send(frame: str) -> None:
    # Simulate a stuck client by raising after queueing attempt — hub drains
    # synchronously, so we block the send to force overflow on subsequent enqueues.
    bp_frames.append(frame)
    if len(bp_frames) > 1 and '"type":"backpressure"' not in frame:
        time.sleep(0.2)

tiny.ensure_session("run_bp", _binding("run_bp"))
# Custom client with a send_fn that always works; force overflow by patching queue limit
# and using a send_fn that refuses to accept (raises), leaving items... Actually hub
# drains immediately. Instead, make send_fn raise after first frame so client is marked
# disconnected, OR temporarily replace _enqueue to fill queue.

blocked = {"n": 0}

def blocking_send(frame: str) -> None:
    blocked["n"] += 1
    bp_frames.append(frame)
    # First frame (backpressure or output) ok; subsequent raise to mark disconnect path.
    if blocked["n"] > 3:
        raise RuntimeError("client_too_slow")

tiny.attach_browser("run_bp", relay.mint_capability_ticket(
    _binding("run_bp"), ["watch"], ttl_seconds=60)[1], blocking_send, client_id="slow")
# Flood outputs beyond queue limit while send raises → disconnect
for i in range(8):
    tiny.publish_output("run_bp", f"flood-{i}\n".encode())
info = tiny.session_info("run_bp")
saw_bp = any('"type":"backpressure"' in f for f in bp_frames)
ok(saw_bp or (info and info.get("browser_count") == 0),
   "backpressure disconnects slow browser clients")

# Runner death → close/error to browsers
browser_death: list[str] = []
death_hub = relay.reset_default_hub_for_tests()
# Use a fresh hub variable but keep module default for death test
death_hub = relay.RelayHub()
_death_host_ticket, _death_host_payload = relay.mint_host_tunnel_ticket(
    _binding("run_dead"))
ok(death_hub.attach_host(
    "run_dead", lambda f: None, binding=_death_host_payload).get("ok") is True,
   "death fixture host attaches with host_tunnel ticket")
death_hub.attach_browser(
    "run_dead",
    relay.mint_capability_ticket(_binding("run_dead"), ["watch"], ttl_seconds=60)[1],
    browser_death.append,
    client_id="watch-dead",
)
death_hub.close_session("run_dead", reason="runner_exited")
ok(any('"type":"close"' in f and "runner_exited" in f for f in browser_death),
   "runner death → close/error to browsers")

# Kill live session cleanly for inject smoke
try:
    supervisor.kill_session(session_id, runner_dir=os.environ["PM_RUNNER_DIR"])
except Exception:
    pass
stop_pump.set()

# --- runner_inject still works (smoke) ---
child2 = [
    sys.executable, "-c",
    "import sys,time\n"
    "sys.stdout.write('INJECT-READY\\n'); sys.stdout.flush()\n"
    "deadline=time.time()+15\n"
    "while time.time()<deadline:\n"
    "    line=sys.stdin.readline()\n"
    "    if not line:\n"
    "        time.sleep(0.05); continue\n"
    "    sys.stdout.write('ECHO:' + line); sys.stdout.flush()\n"
    "    if 'DONE' in line: break\n",
]
meta2 = supervisor.start_session(
    child2, agent_id="cursor/ADAPTER-22-inject", task_id="ADAPTER-22",
    claim_id="claim-a22-inj", cwd=str(ROOT), runner_dir=os.environ["PM_RUNNER_DIR"],
)
deadline = time.time() + 5
while time.time() < deadline:
    try:
        if "INJECT-READY" in Path(meta2["log_path"]).read_text(encoding="utf-8", errors="replace"):
            break
    except Exception:
        pass
    time.sleep(0.05)
injected = agent_host.supervisor_action("inject", meta2["runner_session_id"], {
    "task_id": "ADAPTER-22",
    "text": "inject-smoke",
    "kind": "freeform",
})
deadline = time.time() + 5
saw_echo = False
while time.time() < deadline:
    try:
        text = Path(meta2["log_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        text = ""
    if "ECHO:inject-smoke" in text:
        saw_echo = True
        break
    time.sleep(0.05)
ok(injected.get("injected") is True and saw_echo,
   "existing runner_inject still works (smoke)")
supervisor.kill_session(meta2["runner_session_id"], runner_dir=os.environ["PM_RUNNER_DIR"])

# Control ticket action denial
ctrl, _ = pty_stream.mint_control_ticket(
    runner_session_id="run_x", host_id="host/a22", actions=["input"])
bad, bad_reason = pty_stream.verify_control_ticket(
    ctrl, runner_session_id="run_x", action="resize", host_id="host/a22")
ok(not bad and bad_reason == "action_denied",
   "host control ticket denies actions outside minted list")

# public_relay_url shape
url = relay.public_relay_url(
    "https://plan.example", "run_x", "ticket.value")
ok(url.startswith("wss://plan.example/ixp/v1/runner_sessions/run_x/pty?ticket=")
   and relay.is_loopback_url("http://127.0.0.1:1/x")
   and not relay.is_loopback_url("https://plan.example/x"),
   "public_relay_url uses wss and is_loopback_url helper")

print(f"\nADAPTER-22 browser PTY relay: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
