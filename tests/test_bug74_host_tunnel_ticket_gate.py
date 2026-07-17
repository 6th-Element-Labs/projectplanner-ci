#!/usr/bin/env python3
"""BUG-74: browser watch tickets cannot impersonate the host PTY tunnel."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = Path(tempfile.mkdtemp(prefix="bug74-host-tunnel-"))
os.environ["PM_RUNNER_PTY_RELAY_SECRET"] = "bug74-relay-secret"
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")

from switchboard.application import runner_pty_relay as relay  # noqa: E402
from switchboard.domain import runner_pty as domain  # noqa: E402

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
        "task_id": "BUG-74",
        "claim_id": "claim-bug74",
        "work_session_id": "ws-bug74",
        "runner_session_id": session_id,
        "host_id": "host/bug74",
        "wake_id": "wake-bug74",
        "execution_connection_id": "execconn/bug74",
        "source_sha": "deadbeef",
        "permission_profile": "operator_watch",
    }
    base.update(overrides)
    return base


relay.clear_revoked_jtis_for_tests()
hub = relay.reset_default_hub_for_tests()

# Browser watch ticket must not attach as host.
watch_ticket, watch_payload = relay.mint_capability_ticket(
    _binding("run_bug74"), ["watch", "input", "resize", "signal"], ttl_seconds=120)
denied_watch = hub.attach_host(
    "run_bug74", lambda f: None, binding=watch_payload)
ok(
    denied_watch.get("ok") is not True
    and denied_watch.get("error") in {"missing_scope", "browser_ticket_forbidden"}
    and denied_watch.get("reason") in {
        "host_tunnel_required", "browser_ticket_forbidden",
    },
    "browser watch ticket cannot attach_host",
)

# verify for host_tunnel rejects watch tickets.
verified, reason = relay.verify_capability_ticket(
    watch_ticket, required_scope=domain.HOST_TUNNEL_SCOPE)
ok(verified is None and reason == "missing_scope",
   "watch ticket fails host_tunnel verify with missing_scope")

allowed, allow_reason = relay.ticket_allows_host_tunnel(watch_payload)
ok(not allowed and allow_reason == "host_tunnel_required",
   "ticket_allows_host_tunnel rejects browser scopes")

# Distinct host_tunnel ticket attaches once.
host_ticket, host_payload = relay.mint_host_tunnel_ticket(
    _binding("run_bug74"), ttl_seconds=120)
ok(domain.HOST_TUNNEL_SCOPE in (host_payload.get("scopes") or [])
   and not (set(host_payload.get("scopes") or []) & domain.BROWSER_CAPABILITY_SCOPES),
   "host_tunnel ticket has no browser scopes")

att1 = hub.attach_host("run_bug74", lambda f: None, binding=host_payload)
ok(att1.get("ok") is True, "host_tunnel ticket attaches host")

# Second active host tunnel is rejected (no silent replace).
att2 = hub.attach_host(
    "run_bug74", lambda f: None,
    binding=relay.mint_host_tunnel_ticket(_binding("run_bug74"))[1])
ok(
    att2.get("ok") is not True
    and att2.get("error") == "host_already_attached"
    and att2.get("reason") == "single_host_tunnel",
    "second host tunnel fails closed (single active host)",
)

# host_id mismatch fails closed.
hub.detach_host("run_bug74")
wrong_host = relay.mint_host_tunnel_ticket(
    _binding("run_bug74", host_id="host/attacker"))[1]
# Seed session bind with the legitimate host first.
ok(hub.attach_host(
    "run_bug74", lambda f: None, binding=host_payload).get("ok") is True,
   "re-attach legitimate host after detach")
hub.detach_host("run_bug74")
# Bind session to legitimate host via ensure_session, then reject attacker.
hub.ensure_session("run_bug74", _binding("run_bug74"))
mismatch = hub.attach_host("run_bug74", lambda f: None, binding=wrong_host)
ok(
    mismatch.get("ok") is not True and mismatch.get("error") == "host_mismatch",
    "attach_host fails closed on host_id mismatch",
)

# Missing host_id is denied.
no_host = dict(host_payload)
no_host["host_id"] = ""
missing_host = hub.attach_host("run_bug74", lambda f: None, binding=no_host)
ok(
    missing_host.get("ok") is not True
    and missing_host.get("error") == "host_id_required",
    "attach_host requires host_id on the ticket",
)

# Host URL path is distinct from browser /pty.
host_url = relay.public_host_relay_url(
    "https://plan.example", "run_bug74", host_ticket)
browser_url = relay.public_relay_url(
    "https://plan.example", "run_bug74", watch_ticket)
ok(
    host_url.startswith("wss://plan.example")
    and "/pty/host?" in host_url
    and "ticket=" in host_url,
    "public_host_relay_url uses /pty/host path",
)
ok(
    "/pty?" in browser_url
    and "/pty/host" not in browser_url,
    "browser relay URL stays on /pty (not /pty/host)",
)

# Mixed ticket (host_tunnel + watch) is forbidden on host attach.
mixed_ticket, mixed_payload = relay.mint_capability_ticket(
    _binding("run_mixed"), ["host_tunnel", "watch"], ttl_seconds=60)
mixed_denied = hub.attach_host("run_mixed", lambda f: None, binding=mixed_payload)
ok(
    mixed_denied.get("ok") is not True
    and mixed_denied.get("error") == "browser_ticket_forbidden",
    "host_tunnel+watch mixed ticket cannot attach_host",
)

print(f"\nBUG-74 host tunnel ticket gate: {passed} passed, {failed} failed")
print(json.dumps({
    "host_url_sample": host_url.split("ticket=")[0] + "ticket=<redacted>",
}, sort_keys=True))
sys.exit(1 if failed else 0)
