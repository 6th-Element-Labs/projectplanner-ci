#!/usr/bin/env python3
"""BUG-125: relay WebSocket readiness means Agent Host attached, not socket open."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from switchboard.application import runner_pty_relay as relay
from switchboard.domain import runner_pty as domain

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


binding = {
    "tenant_id": "tenant/default",
    "user_id": "user/bug125",
    "project_id": "switchboard",
    "task_id": "BUG-125",
    "claim_id": "claim-bug125",
    "work_session_id": "ws-bug125",
    "runner_session_id": "run_bug125",
    "host_id": "host/bug125",
    "wake_id": "wake-bug125",
    "execution_connection_id": "execconn/bug125",
    "source_sha": "deadbeef",
    "permission_profile": "operator_watch",
}
hub = relay.RelayHub()
browser_frames = []
browser_ticket = {**binding, "jti": "browser-bug125", "scopes": ["watch", "input"]}
attached = hub.attach_browser(
    "run_bug125", browser_ticket, lambda frame: browser_frames.append(frame) or True,
    client_id="browser-bug125")
waiting = domain.decode_frame(browser_frames[-1])
ok(attached.get("ok") is True
   and waiting.get("connection_state") == "waiting_for_host"
   and waiting.get("host_attached") is False,
   "browser attach reports relay-only state while no Agent Host tunnel exists")

host_ticket = {**binding, "jti": "host-bug125", "scopes": ["host_tunnel"]}
host = hub.attach_host("run_bug125", lambda _frame: True, host_ticket)
ready = domain.decode_frame(browser_frames[-1])
ok(host.get("ok") is True
   and ready.get("connection_state") == "host_attached"
   and ready.get("host_attached") is True,
   "Agent Host attach pushes an explicit end-to-end readiness frame")

hub.detach_host("run_bug125")
detached = domain.decode_frame(browser_frames[-1])
ok(detached.get("connection_state") == "waiting_for_host"
   and detached.get("host_attached") is False,
   "host disconnect immediately removes the browser's end-to-end ready state")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
