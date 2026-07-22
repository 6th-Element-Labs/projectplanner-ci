#!/usr/bin/env python3
"""WATCH-1: a claim-bound Connect runner mints a relay host_url.

The 2026-07-22 dark-Watch incident: once a native Agent Host runner claimed its
task and opened a Work Session, ``_server_relay_options`` stopped minting a
``host_url`` for it -- the direct-only bind fallback left
``execution_connection_id``/``source_sha`` empty, so ``missing_ticket_bind_fields``
failed closed. With no host_url the heartbeat could not renew the tunnel and the
BUG-126 launch bridge recorded ``missing_host_url``; the browser terminal went
dark. This test pins that a claim-bound Connect runner -- carrying neither an
execution_connection_id nor a source_sha in its metadata -- still yields a
host_url so Watch/Chat can stream.
"""
from __future__ import annotations

import os

from path_setup import ROOT  # noqa: F401

os.environ.setdefault("PM_RUNNER_PTY_RELAY_PUBLIC_BASE", "https://plan.example")
os.environ.setdefault("PM_RUNNER_PTY_RELAY_SECRET", "watch1-test-secret")

from switchboard.storage.repositories import runner as runner_repo  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


# A Connect runner that has claimed its task and opened a Work Session. It
# carries NO execution_connection_id and NO source_sha -- exactly the shape of
# the six live sessions that went dark on 2026-07-22.
claim_bound_connect = {
    "runner_session_id": "run_watch1",
    "task_id": "WATCH-1",
    "claim_id": "taskclaim-watch1",
    "host_id": "host/steve-mbp-co16",
    "runtime": "codex",
    "status": "running",
    "metadata": {
        "wake_id": "wake-watch1",
        "work_session_id": "worksession-watch1",
        "connect_assignment": True,
        "assignment_schema": "switchboard.connect.assignment.v1",
        "native_host_execution": True,
    },
}

relay = runner_repo._server_relay_options(
    claim_bound_connect, user_id="user-watch1", project="switchboard")

ok(not relay.get("error"),
   "claim-bound Connect runner mints without a bind error "
   f"(got {relay.get('error') or 'ok'}, missing={relay.get('missing')})")
ok(bool(relay.get("host_url")),
   "claim-bound Connect runner yields a relay host_url")
binding = relay.get("binding") or {}
ok(binding.get("claim_id") == "taskclaim-watch1",
   "the real claim_id is carried into the ticket binding")
ok(binding.get("work_session_id") == "worksession-watch1",
   "the real work_session_id is carried into the ticket binding")
ok(bool(binding.get("execution_connection_id")),
   "the absent execution_connection_id is filled, not left empty")
ok(bool(binding.get("source_sha")),
   "the absent source_sha is filled, not left empty")

# Boundary: an UNBOUND Connect runner (no claim, no Work Session) still fails
# still mints -- Connect launches its PTY before a claim/Work Session exists (the
# DHCP model), so an unclaimed native Connect run is watchable on its task+host+wake
# identity with placeholder bind fields. (Superseded the old "fails closed" edge:
# that pinning left every Connect run dark for its whole life.)
unbound_connect = {
    "runner_session_id": "run_watch1_unbound",
    "task_id": "WATCH-1",
    "claim_id": "",
    "host_id": "host/steve-mbp-co16",
    "runtime": "codex",
    "status": "running",
    "metadata": {
        "wake_id": "wake-watch1-unbound",
        "connect_assignment": True,
        "assignment_schema": "switchboard.connect.assignment.v1",
        "native_host_execution": True,
    },
}
unbound = runner_repo._server_relay_options(
    unbound_connect, user_id="user-watch1", project="switchboard")
ok(not unbound.get("error") and bool(unbound.get("host_url")),
   "an unclaimed native Connect runner mints a host_url (Watch works pre-claim)")

# A row with NO native/connect identity at all still fails closed -- the trust
# anchor is native_host_execution + connect_assignment, not "any unbound row".
stranger = runner_repo._server_relay_options(
    {"runner_session_id": "run_stranger", "task_id": "WATCH-1", "claim_id": "",
     "host_id": "host/x", "status": "running", "metadata": {}},
    user_id="user-watch1", project="switchboard")
ok(stranger.get("error") == runner_repo.RUNNER_BIND_ERROR,
   "a non-native unbound row still fails closed (trust anchor preserved)")

print(f"\nWATCH-1 Connect relay mint: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
