#!/usr/bin/env python3
"""A transport placeholder must never read as ownership evidence.

SIMPLIFY-18 follow-up (ADR-0008 C1: claims and Work Sessions may not be
impersonated). Relay tickets for native/relay-attached runners substitute a
`direct/<runner_session_id>` placeholder where no real claim, Work Session,
execution connection, or source SHA exists -- otherwise the ticket bind shape
cannot be satisfied and Watch breaks for those runners.

That substitution is legitimate transport plumbing, but it previously looked
identical to a real record, so anything auditing "which claim was this Watch
session under?" received a fiction it could not detect. This pins the
placeholder as explicitly labelled and separable, and pins that a genuinely
bound runner is never mislabelled.
"""
from __future__ import annotations

import os
import sys

from path_setup import ROOT  # noqa: F401

os.environ.setdefault("PM_RUNNER_PTY_RELAY_PUBLIC_BASE", "https://plan.example")
os.environ.setdefault("PM_RUNNER_PTY_RELAY_SECRET", "synthetic-bind-secret")

from switchboard.domain import execution_liveness as live  # noqa: E402
from switchboard.storage.repositories import runner as runner_repo  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


print("synthetic bind honesty")

# --- the predicate itself ---------------------------------------------------
ok(live.SYNTHETIC_BIND_PREFIX == "direct/",
   "the historical placeholder prefix is stable (already serialized in tickets)")
ok(live.is_synthetic_bind_ref("direct/run_abc") is True,
   "a placeholder ref is recognised as synthetic")
ok(live.is_synthetic_bind_ref("taskclaim-1234") is False
   and live.is_synthetic_bind_ref("") is False
   and live.is_synthetic_bind_ref(None) is False,
   "real claim ids, empty and None are not synthetic")
ok(live.synthetic_bind_fields({"claim_id": "direct/x", "task_id": "T-1"})
   == ["claim_id"],
   "exactly the substituted fields are named, not the whole binding")

# --- behaviour: an UNBOUND native runner is labelled ------------------------
# Same WATCH-7 shape as tests/test_connect_unclaimed_relay_mint.py: a native run
# that launched before it claimed, so claim/work_session/exec-conn/sha are absent.
unbound = {
    "runner_session_id": "run_synthetic_unbound",
    "task_id": "WATCH-7",
    "claim_id": "",
    "host_id": "host/steve-mbp-co16",
    "runtime": "codex",
    "status": "running",
    "metadata": {
        "wake_id": "wake-synthetic-unbound",
        "connect_assignment": True,
        "assignment_schema": "switchboard.connect.assignment.v1",
        "native_host_execution": True,
    },
}
relay = runner_repo._server_relay_options(
    unbound, user_id="user-x", project="switchboard")
ok(not relay.get("error"),
   f"Watch still mints for an unbound native runner (got {relay.get('error') or 'ok'})")
ok(relay.get("synthetic_bind") is True,
   "the response declares that its bind was synthetic")
ok("claim_id" in (relay.get("synthetic_bind_fields") or []),
   "the substituted claim_id is named in the response")
ok(live.is_synthetic_bind_ref((relay.get("binding") or {}).get("claim_id")),
   "the binding's claim_id is detectable as fiction, not a claim")

# --- behaviour: a genuinely bound runner is NOT labelled --------------------
bound = {
    "runner_session_id": "run_synthetic_bound",
    "task_id": "WATCH-7",
    "claim_id": "taskclaim-real0001",
    "host_id": "host/steve-mbp-co16",
    "runtime": "codex",
    "status": "running",
    "metadata": {
        "wake_id": "wake-synthetic-bound",
        "connect_assignment": True,
        "assignment_schema": "switchboard.connect.assignment.v1",
        "native_host_execution": True,
        "work_session_id": "worksession-real0001",
        "execution_connection_id": "execconn-real0001",
        "source_sha": "a" * 40,
    },
}
bound_relay = runner_repo._server_relay_options(
    bound, user_id="user-x", project="switchboard")
ok(not bound_relay.get("error"),
   f"a fully bound runner still mints (got {bound_relay.get('error') or 'ok'})")
ok(bound_relay.get("synthetic_bind") is False
   and bound_relay.get("synthetic_bind_fields") == [],
   "a real binding is never mislabelled as synthetic")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
