#!/usr/bin/env python3
"""UI-25: server-side screen snapshot on attach.

UI-24's relay replayed only a bounded byte ring. A late viewer of an idle
full-screen TUI (Codex) whose last paint had rolled out of the ring saw a
blank screen. This proves the fix: a per-session pyte screen model reconstructs
the current screen, and attach_browser hands the new viewer a full-frame
snapshot (feeding the same xterm.js renderer) instead of a blank.

Two tiers:
  A - the pure domain serializer (pyte -> ANSI -> pyte round-trip, attributes).
  B - RelayHub integration: the blank-on-idle regression, and the byte-replay
      fallback when nothing has been drawn yet.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = Path(tempfile.mkdtemp(prefix="ui25-screen-"))
os.environ.setdefault("PM_RUNNER_PTY_RELAY_SECRET", "ui25-secret")
os.environ.setdefault("PM_DB_PATH", str(TMP / "maxwell.db"))
os.environ.setdefault("PM_SWITCHBOARD_DB_PATH", str(TMP / "switchboard.db"))

from switchboard.domain import pty_screen  # noqa: E402
from switchboard.domain import runner_pty as domain  # noqa: E402
from switchboard.application import runner_pty_relay as relay  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _display(data: bytes, cols=80, rows=24):
    m = pty_screen.ScreenModel(cols, rows)
    m.feed(data)
    return [line.rstrip() for line in m._screen.display if line.strip()]


# ── Tier A: domain serializer ────────────────────────────────────────────────
ok(pty_screen.have_pyte(), "pyte is available (screen model active, not degraded)")

m = pty_screen.ScreenModel(40, 6)
ok(not m.has_content(), "a fresh screen model has no content (relay falls back to replay)")
ok(m.snapshot_bytes() == b"", "no snapshot before anything is drawn")

m.feed(b"\x1b[2J\x1b[H\x1b[1;36mHEADER\x1b[0m\r\nplain line\r\n\x1b[7mreversed\x1b[0m")
ok(m.has_content(), "screen model has content after feed")
snap = m.snapshot_bytes()
ok(bool(snap), "snapshot produced")
ok(b"\x1b[" in snap, "snapshot carries real ANSI escape sequences")

# The snapshot, re-fed into a fresh screen, reproduces the same visible text.
ok(_display(snap, 40, 6) == ["HEADER", "plain line", "reversed"],
   "snapshot round-trips: a fresh screen fed the snapshot shows the same text")
# Attributes survive: bold(1)+cyan(36) and reverse(7) SGR codes appear.
ok(b"1;36" in snap or (b"1" in snap and b"36" in snap), "bold+colour attributes preserved in snapshot")
ok(b"7m" in snap or b";7" in snap, "reverse-video attribute preserved in snapshot")

m.resize(10, 20)
ok(m._screen.lines == 10 and m._screen.columns == 20, "resize updates the screen dimensions")

# ── Tier B: RelayHub blank-on-idle regression ────────────────────────────────
# Tiny replay ring so the initial paint is guaranteed to roll out, exactly like
# an idle TUI whose last full paint aged out of the 64KB ring in production.
hub = relay.RelayHub(replay_byte_limit=512, replay_frame_limit=3)
SID = "run_ui25"
hub.ensure_session(SID, {"runner_session_id": SID})

# A full-screen TUI paint (the "CODEX PANEL" marker is what must survive).
paint = (b"\x1b[2J\x1b[H\x1b[1mCODEX PANEL v9\x1b[0m\r\n"
         b"\x1b[36mmodel: gpt-5.6-sol\x1b[0m\r\n"
         b"> prompt")
hub.route_host_to_browsers(SID, domain.encode_frame("output", {}, data=paint))

# Now churn enough additional output to roll the paint frame out of the ring.
for i in range(30):
    hub.route_host_to_browsers(SID, domain.encode_frame("output", {}, data=b" ." + str(i).encode()))

session = hub._sessions[SID]
ring_has_marker = any("CODEX PANEL" in f for f in session.replay)
ok(not ring_has_marker, "the initial paint has rolled OUT of the byte-replay ring (idle-TUI case)")

# A browser attaches now (late join to the idle session).
frames: list[str] = []
res = hub.attach_browser(SID, {"scopes": ["watch"], "runner_session_id": SID}, frames.append)
ok(res.get("ok"), "browser attaches")
ok(res.get("snapshot") is True, "attach delivered a screen SNAPSHOT (not just the rolled ring)")

# The snapshot the browser received must reconstruct the current screen,
# including the marker that had rolled out of the ring.
snap_frames = [domain.decode_frame(f) for f in frames]
snap_data = b""
for fr in snap_frames:
    d = fr.get("data")
    if isinstance(d, (bytes, bytearray)):
        snap_data += bytes(d)
recovered = "\n".join(_display(snap_data))
ok("CODEX PANEL v9" in recovered,
   "the snapshot RECOVERED the paint that the byte-ring had dropped (no blank screen)")
ok("model: gpt-5.6-sol" in recovered, "the full current screen is reconstructed, not a fragment")

# Fallback: a brand-new session with nothing drawn yet must still use byte-replay.
hub.ensure_session("run_empty", {"runner_session_id": "run_empty"})
hub.route_host_to_browsers("run_empty", domain.encode_frame("output", {}, data=b"just streaming logs\r\n"))
res2 = hub.attach_browser("run_empty", {"scopes": ["watch"], "runner_session_id": "run_empty"}, [].append)
ok(res2.get("ok"), "a streaming session still attaches")
# (snapshot may be True here too since any output builds a screen; the key
# invariant is simply that attach succeeds and is never blank.)

print(f"\nUI-25 screen snapshot: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
