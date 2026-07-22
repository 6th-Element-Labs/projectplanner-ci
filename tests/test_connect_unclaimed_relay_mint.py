#!/usr/bin/env python3
"""An UNCLAIMED native Connect runner must mint a relay ticket (Watch works pre-claim).

Connect (the content-blind DHCP plane) launches a host PTY before any task claim
or Work Session exists -- that is the whole point of the model. assert_runner_watchable
already accepts such a runner (is_connect_assignment_runner), but _server_relay_options
only granted the pre-bind placeholder identity to is_direct_assignment_runner, so every
Connect run was told "watchable" and then refused a ticket -- dark for its entire life.
This pins that an unclaimed native Connect runner mints a host_url, exactly like the
unclaimed native *direct* runner already does.
"""
from __future__ import annotations

import os

from path_setup import ROOT  # noqa: F401

os.environ.setdefault("PM_RUNNER_PTY_RELAY_PUBLIC_BASE", "https://plan.example")
os.environ.setdefault("PM_RUNNER_PTY_RELAY_SECRET", "connect-unclaimed-secret")

from switchboard.storage.repositories import runner as runner_repo  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


# The WATCH-7 shape: a native Connect runner that has launched but NOT yet claimed
# its task (claim_id + work_session_id are empty -- Connect starts before them).
unclaimed_connect = {
    "runner_session_id": "run_connect_unclaimed",
    "task_id": "WATCH-7",
    "claim_id": "",
    "host_id": "host/steve-mbp-co16",
    "runtime": "codex",
    "status": "running",
    "metadata": {
        "wake_id": "wake-connect-unclaimed",
        "connect_assignment": True,
        "assignment_schema": "switchboard.connect.assignment.v1",
        "native_host_execution": True,
        # deliberately NO claim, work_session_id, execution_connection_id, source_sha
    },
}

ok(runner_repo.is_connect_assignment_runner(unclaimed_connect),
   "the runner is recognized as an unclaimed native Connect run")

relay = runner_repo._server_relay_options(
    unclaimed_connect, user_id="user-x", project="switchboard")

ok(not relay.get("error"),
   "unclaimed Connect runner mints without a bind error "
   f"(got {relay.get('error') or 'ok'}, missing={relay.get('missing')})")
ok(bool(relay.get("host_url")),
   "unclaimed Connect runner yields a relay host_url (Watch can stream pre-claim)")

# Parity: the direct equivalent already works -- prove we match its behaviour.
unclaimed_direct = {
    **unclaimed_connect,
    "runner_session_id": "run_direct_unclaimed",
    "metadata": {
        "wake_id": "wake-direct-unclaimed",
        "direct_assignment": True,
        "assignment_schema": "switchboard.direct_cli_assignment.v1",
        "native_host_execution": True,
    },
}
d = runner_repo._server_relay_options(unclaimed_direct, user_id="user-x", project="switchboard")
ok(bool(d.get("host_url")),
   "parity check: the unclaimed native *direct* runner also mints (unchanged)")

print(f"\nConnect unclaimed relay mint: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
