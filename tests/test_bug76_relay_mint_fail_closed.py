#!/usr/bin/env python3
"""BUG-76/WATCH-11: relay failures fail closed; no local browser fallback."""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest import mock

from path_setup import ROOT  # noqa: F401

from adapters import agent_host
from switchboard.application import runner_pty_relay as relay

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def _fake_status_run(cmd, capture_output=True, text=True, timeout=15):
    return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
        "runner_session_id": cmd[-1],
        "pty": True,
        "alive": True,
        "host_id": "host/bug76",
        "task_id": "BUG-76",
        "control": {"runner_open": True},
    }))


options = {
    "task_id": "BUG-76",
    "claim_id": "claim-bug76",
    "work_session_id": "ws-bug76",
    "wake_id": "wake-bug76",
    "tenant_id": "tenant/t1",
    "user_id": "user/u1",
    "execution_connection_id": "execconn/bug76",
    "source_sha": "deadbeef",
}

# A non-loopback deployment must refuse when relay ticket minting fails.
os.environ["PM_SWITCHBOARD_PUBLIC_BASE"] = "https://plan.example"
os.environ.pop("PM_RUNNER_PTY_RELAY_PUBLIC_BASE", None)
with mock.patch.object(agent_host.subprocess, "run", side_effect=_fake_status_run), \
        mock.patch.object(relay, "mint_capability_ticket", side_effect=RuntimeError("forced_mint_failure")):
    failed_open = agent_host.supervisor_action("open", "rs-bug76", options)

serialized = json.dumps(failed_open)
ok(
    failed_open.get("opened") is False
    and failed_open.get("error") == "relay_mint_failed"
    and failed_open.get("relay_required") is True
    and failed_open.get("browser_safe") is False
    and "stream_url" not in failed_open
    and "127.0.0.1" not in serialized,
    "relay mint failure has no local browser fallback",
)

# No public relay base is an explicit refusal, even if obsolete stream
# coordinates happen to survive in a supervisor receipt from an older host.
os.environ.pop("PM_SWITCHBOARD_PUBLIC_BASE", None)
with mock.patch.object(agent_host.subprocess, "run", side_effect=_fake_status_run):
    refused = agent_host.supervisor_action("open", "rs-bug76", options)
ok(
    refused.get("opened") is not True
    and refused.get("error") == "not_supported"
    and "non-loopback relay public base" in refused.get("reason", ""),
    "missing public relay base fails closed",
)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
