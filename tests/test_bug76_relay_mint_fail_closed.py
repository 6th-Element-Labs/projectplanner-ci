#!/usr/bin/env python3
"""BUG-76: relay mint failure fails closed — no loopback stream_url persisted."""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest import mock

from path_setup import ROOT  # noqa: F401  — shared sys.path shim (ARCH-MS-14)

from adapters import agent_host
from switchboard.application import runner_pty_relay as relay

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def _status_meta(runner_session_id: str) -> dict:
    return {
        "runner_session_id": runner_session_id,
        "pty": True,
        "alive": True,
        "streamer_pid": os.getpid(),
        "stream_port": 41999,
        "stream_bind": "127.0.0.1",
        "host_id": "host/bug76",
        "task_id": "BUG-76",
        "control": {"runner_open": True, "runner_inject": True},
    }


def _fake_status_run(cmd, capture_output=True, text=True, timeout=15):
    # supervisor status → JSON meta on stdout
    runner_session_id = cmd[-1] if cmd else "rs-bug76"
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(_status_meta(str(runner_session_id))),
        stderr="",
    )


# --- open path: non-loopback public base + mint failure fails closed ---
os.environ["PM_SWITCHBOARD_PUBLIC_BASE"] = "https://plan.example"
os.environ.pop("PM_RUNNER_PTY_RELAY_PUBLIC_BASE", None)

_orig_mint = relay.mint_capability_ticket


def _boom(*_a, **_k):
    raise RuntimeError("forced_mint_failure")


relay.mint_capability_ticket = _boom  # type: ignore[assignment]
try:
    with mock.patch.object(agent_host.subprocess, "run", side_effect=_fake_status_run), \
            mock.patch.object(agent_host, "_pid_alive", return_value=True), \
            mock.patch.object(agent_host, "_tcp_port_open", return_value=True), \
            mock.patch("codex.pty_stream.mint_ticket", return_value=("local-ticket", 9999.0)), \
            mock.patch(
                "codex.pty_stream.build_stream_url",
                return_value="http://127.0.0.1:41999/runner/v1/sessions/rs-bug76/stream?ticket=local-ticket",
            ):
        failed_open = agent_host.supervisor_action("open", "rs-bug76", {
            "task_id": "BUG-76",
            "claim_id": "claim-bug76",
            "work_session_id": "ws-bug76",
            "wake_id": "wake-bug76",
            "tenant_id": "tenant/t1",
            "user_id": "user/u1",
            "execution_connection_id": "execconn/bug76",
            "source_sha": "deadbeef",
        })
finally:
    relay.mint_capability_ticket = _orig_mint  # type: ignore[assignment]

serialized = json.dumps(failed_open)
ok(
    failed_open.get("opened") is False
    and failed_open.get("error") == "relay_mint_failed"
    and failed_open.get("relay_required") is True
    and failed_open.get("browser_safe") is False
    and "stream_url" not in failed_open
    and "local_stream_url" not in failed_open
    and "127.0.0.1" not in serialized
    and "localhost" not in serialized.lower(),
    "open fails closed on mint failure with no loopback stream_url",
)

# --- handle_runner_controls: never reintroduce loopback via result fallback ---
register_calls = []
complete_calls = []


def _try_stub(method, path, body=None):
    if "claim_runner_control" in path or path.endswith("claim_runner_control"):
        return {"claimed": True}
    if "register_runner" in path or "runner_sessions" in path and method == "POST":
        register_calls.append(body or {})
        return {"ok": True}
    if "complete_runner_control" in path:
        complete_calls.append(body or {})
        return {"ok": True}
    if "list_runner_controls" in path or "runner_controls" in path:
        return {
            "requests": [{
                "request_id": "req-bug76",
                "action": "open",
                "runner_session_id": "rs-bug76",
                "options": {},
            }],
        }
    return {}


# Simulate a buggy opened=True result that still carries a loopback stream_url.
# sanitize strips it; snapshot must not fall back to result.stream_url.
buggy_result = {
    "opened": True,
    "stream_url": "http://127.0.0.1:41999/runner/v1/sessions/rs-bug76/stream?ticket=x",
    "relay_url": None,
    "transport": "http_chunked",
    "expires_at": 9999.0,
    "browser_safe": False,
    "relay_required": True,
    "metadata": {
        "stream_url": "http://127.0.0.1:41999/runner/v1/sessions/rs-bug76/stream?ticket=x",
        "local_stream_url": "http://127.0.0.1:41999/runner/v1/sessions/rs-bug76/stream?ticket=x",
        "transport": "http_chunked",
        "browser_safe": False,
        "relay_required": True,
        "pty": True,
    },
}

with mock.patch.object(agent_host, "_try", side_effect=_try_stub), \
        mock.patch.object(agent_host, "supervisor_action", return_value=buggy_result):
    handled = agent_host.handle_runner_controls({"host_id": "host/bug76"})

ok(handled and handled[0].get("status") == "completed",
   "handle_runner_controls processes open request")

snap = (complete_calls[0] or {}).get("snapshot") if complete_calls else {}
meta = (register_calls[0] or {}).get("metadata") if register_calls else {}
snap_blob = json.dumps(snap)
meta_blob = json.dumps(meta)
ok(
    "127.0.0.1" not in snap_blob
    and "127.0.0.1" not in meta_blob
    and not (snap or {}).get("stream_url")
    and "local_stream_url" not in (meta or {}),
    "snapshot/register metadata never persist loopback stream_url after sanitize",
)

# Compat path without public base still allows local open.
os.environ.pop("PM_SWITCHBOARD_PUBLIC_BASE", None)
os.environ.pop("PM_RUNNER_PTY_RELAY_PUBLIC_BASE", None)
with mock.patch.object(agent_host.subprocess, "run", side_effect=_fake_status_run), \
        mock.patch.object(agent_host, "_pid_alive", return_value=True), \
        mock.patch.object(agent_host, "_tcp_port_open", return_value=True), \
        mock.patch("codex.pty_stream.mint_ticket", return_value=("local-ticket", 9999.0)), \
        mock.patch(
            "codex.pty_stream.build_stream_url",
            return_value="http://127.0.0.1:41999/runner/v1/sessions/rs-bug76/stream?ticket=local-ticket",
        ):
    local_open = agent_host.supervisor_action("open", "rs-bug76")

ok(
    local_open.get("opened") is True
    and local_open.get("transport") == "http_chunked"
    and local_open.get("relay_required") is True
    and local_open.get("browser_safe") is False
    and "ticket=" in str(local_open.get("stream_url") or ""),
    "without public base, local http_chunked compat path remains",
)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
