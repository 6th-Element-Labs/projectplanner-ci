#!/usr/bin/env python3
"""BUG-75: PTY ticket revoke drops live sessions and persists until JWT expiry."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = Path(tempfile.mkdtemp(prefix="bug75-pty-revoke-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_RUNNER_PTY_RELAY_SECRET"] = "bug75-relay-secret"

import store  # noqa: E402
from switchboard.application import runner_pty_relay as relay  # noqa: E402
from switchboard.domain import runner_pty as domain  # noqa: E402
from switchboard.storage.repositories import runner_pty_revocations as rev_store  # noqa: E402

PROJECT = "switchboard"
store.init_db(PROJECT)
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
        "project_id": PROJECT,
        "task_id": "BUG-75",
        "claim_id": "claim-bug75",
        "work_session_id": "ws-bug75",
        "runner_session_id": session_id,
        "host_id": "host/bug75",
        "wake_id": "wake-bug75",
        "execution_connection_id": "execconn/bug75",
        "source_sha": "cafebabe",
        "permission_profile": "operator_watch",
    }
    base.update(overrides)
    return base


relay.clear_revoked_jtis_for_tests(PROJECT)
hub = relay.reset_default_hub_for_tests()

ticket, payload = relay.mint_capability_ticket(
    _binding("run_bug75"), ["watch", "input"], ttl_seconds=300)
jti = str(payload["jti"])
expires_at = float(payload["exp"])

frames: list[bytes] = []
closed = {"n": 0}
hub.attach_browser(
    "run_bug75",
    payload,
    frames.append,
    client_id="b1",
    close_fn=lambda: closed.__setitem__("n", closed["n"] + 1),
)
ok(hub.session_info("run_bug75").get("browser_count") == 1, "browser attached before revoke")

ok(relay.revoke_ticket_jti(
    jti, project=PROJECT, expires_at=expires_at, hub=hub),
   "revoke_ticket_jti succeeds with project+expiry")
ok(closed["n"] == 1 and any(
    domain.decode_frame(frame).get("reason") == "ticket_revoked"
    for frame in frames),
   "revoke closes the live browser client immediately")
ok(hub.session_info("run_bug75").get("browser_count") == 0,
   "hub browser count drops after revoke")
ok(rev_store.is_jti_revoked_persisted(jti, project=PROJECT) is not None,
   "revocation is persisted in the board DB")

# Simulate another instance: clear in-process cache, trust DB.
with relay._REVOKE_LOCK:
    relay._REVOKED_JTIS.clear()
ok(relay.is_jti_revoked(jti, project=PROJECT),
   "other instance sees persisted revocation via DB")
denied, reason = relay.verify_capability_ticket(ticket, required_scope="watch")
ok(denied is None and reason == "revoked",
   "verify fails closed after cross-instance revoke")

# Expiry: once past JWT exp, denial lifts.
with relay._REVOKE_LOCK:
    relay._REVOKED_JTIS.clear()
ok(not relay.is_jti_revoked(jti, project=PROJECT, now=expires_at + 1),
   "revocation expires with the JWT")
ok(rev_store.is_jti_revoked_persisted(jti, project=PROJECT, now=expires_at + 1) is None,
   "expired revocation rows are purged")

# Revoke-by-ticket must decode with a far-past clock (now=0), not far-future.
ticket2, payload2 = relay.mint_capability_ticket(
    _binding("run_bug75_ticket"), ["watch"], ttl_seconds=120)
ok(relay.revoke_capability_ticket(ticket2, project=PROJECT)[0] is True,
   "revoke_capability_ticket succeeds for a live ticket")
denied2, reason2 = relay.verify_capability_ticket(ticket2, required_scope="watch")
ok(denied2 is None and reason2 == "revoked",
   "revoke_capability_ticket marks the ticket revoked")

relay.clear_revoked_jtis_for_tests(PROJECT)
print(f"\nBUG-75 PTY revoke live sessions: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
