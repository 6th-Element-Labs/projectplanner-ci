#!/usr/bin/env python3
"""WATCH-4: watchability keys on live relay attachment, not DB-row inference.

The 2026-07-22 dark-Watch incident (and the DOGFOOD-24 follow-up): a native
Agent Host PTY run launched through Switchboard Connect is bound to task+host+wake
but deliberately carries no scheduler ``claim_id``/``work_session_id``. The watch
gate inferred liveness from that bind tuple alone, so a live, host-attached
Connect run resolved ``runner_bind_incomplete`` and the operator terminal hung on
"Starting" -- while a row that said ``running`` with a dead pipe looked watchable.

The RelayHub already knows, per runner_session_id, whether a host tunnel is
attached (``session_info()['host_attached']``, ADAPTER-22 #554) but nothing
consulted it. This pins the WATCH-4 contract:

  * host_attached=True  -> watchable regardless of claim-binding state.
  * host_attached=False -> a running row with no bridge is refused host_not_attached.
  * host_attached=None  -> the signal is opt-in; DB-row inference is unchanged so
                           callers that cannot see the hub keep today's behaviour.

Bind-tuple checks remain for authorization (task/host/wake identity), never for
liveness.
"""
from __future__ import annotations

import os

from path_setup import ROOT  # noqa: F401

os.environ.setdefault("PM_RUNNER_PTY_RELAY_PUBLIC_BASE", "https://plan.example")
os.environ.setdefault("PM_RUNNER_PTY_RELAY_SECRET", "watch4-test-secret")

from switchboard.storage.repositories import runner as runner_repo  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


# A live native-host PTY run started through Switchboard Connect: bound to
# task+host+wake, PTY transport ready, NO claim_id and NO work_session_id -- the
# exact shape of the DOGFOOD-24 acceptance probe.
def connect_pty_session(**overrides):
    session = {
        "runner_session_id": "run_watch4",
        "task_id": "DOGFOOD-24",
        "claim_id": "",
        "host_id": "host/steve-mbp-co16",
        "runtime": "codex",
        "status": "running",
        "control": {"runner_open": True, "runner_kill": True, "managed_process": True},
        "metadata": {
            "wake_id": "wake-watch4",
            "connect_assignment": True,
            "assignment_schema": "switchboard.connect.assignment.v1",
            "native_host_execution": True,
            "pty": True,
            "stream_bind": "127.0.0.1",
            "stream_port": 52380,
        },
    }
    session.update(overrides)
    return session


# 1) LIVE BRIDGE -> watchable regardless of the absent claim/work_session bind.
attached = runner_repo.assert_runner_watchable(
    connect_pty_session(), host_attached=True)
ok(attached.get("watchable") is True,
   "an unbound Connect run with an attached host bridge is watchable "
   f"(got watchable={attached.get('watchable')}, missing={attached.get('missing')})")
ok(not attached.get("missing"),
   "no bind fields are reported missing when the bridge is attached")

# 2) RUNNING ROW, NO BRIDGE -> refused host_not_attached (not a bind-tuple lie).
detached = runner_repo.assert_runner_watchable(
    connect_pty_session(), host_attached=False)
ok(detached.get("watchable") is False,
   "a running row with no attached bridge is not watchable")
ok(detached.get("error_code") == "host_not_attached",
   "the refusal names host_not_attached, not a missing claim_id "
   f"(got {detached.get('error_code')})")

# 3) NO SIGNAL -> falls back to the BUG-130 (#729) DB-row assignment recognition:
#    an unbound Connect run is watchable by its assignment shape for callers that
#    cannot query the hub. WATCH-4 layers on top without regressing that; its added
#    value is that host_attached=False (case 2) overrides this DB-row inference to
#    refuse a live-looking row whose pipe is actually dead.
unknown = runner_repo.assert_runner_watchable(connect_pty_session())
ok(unknown.get("watchable") is True,
   "with no attachment signal an unbound Connect run stays watchable via its assignment shape (BUG-130 #729)")
ok(unknown.get("binding_mode") == "native_assignment",
   "the no-signal watchable verdict uses the unified native transport mode, not relay_attached")

# 4) A fully claim-bound run stays watchable with no signal (regression guard: the
#    new liveness path must not disturb the existing bound-runner contract).
bound = connect_pty_session(
    claim_id="taskclaim-watch4",
    metadata={
        "wake_id": "wake-watch4",
        "work_session_id": "worksession-watch4",
        "native_host_execution": True,
        "pty": True,
        "stream_bind": "127.0.0.1",
        "stream_port": 52380,
    },
)
bound_verdict = runner_repo.assert_runner_watchable(bound)
ok(bound_verdict.get("watchable") is True,
   "a fully claim-bound native run remains watchable with no attachment signal")

# 5) The relay-ticket minter fills the placeholder identity fields for an
#    attached unbound run, so a host_url is actually issued (the browser can attach).
relay = runner_repo._server_relay_options(
    connect_pty_session(), user_id="user-watch4", project="switchboard",
    host_attached=True)
ok(not relay.get("error") and bool(relay.get("host_url")),
   "an attached unbound Connect run mints a relay host_url "
   f"(error={relay.get('error')}, missing={relay.get('missing')})")

# 6) host_attached_for tri-state: an unknown session resolves None (the safe
#    fallback), so a process that does not hold the tunnel NEVER reports a live run
#    as detached -- it defers to DB-row inference instead.
from switchboard.application import runner_pty_relay as relay_mod  # noqa: E402

relay_mod.reset_default_hub_for_tests()
ok(relay_mod.host_attached_for("run_never_seen") is None,
   "host_attached_for returns None for a session this hub has never held")
ok(relay_mod.host_attached_for("") is None,
   "host_attached_for returns None for an empty session id")

# And the tri-state resolves True/False once the hub owns the session, keyed on the
# live host tunnel (session.host_send) rather than any DB row.
hub = relay_mod.get_default_hub()
session = hub.ensure_session(
    "run_hubbed", {"runner_session_id": "run_hubbed", "host_id": "host/steve-mbp-co16"})
ok(relay_mod.host_attached_for("run_hubbed") is False,
   "a hub session with no host tunnel resolves host_attached False")
session.host_send = (lambda frame: True)  # simulate an attached host tunnel
ok(relay_mod.host_attached_for("run_hubbed") is True,
   "a hub session with an attached host tunnel resolves host_attached True")

print(f"\nWATCH-4 relay-attachment liveness: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
