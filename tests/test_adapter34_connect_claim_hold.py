#!/usr/bin/env python3
"""ADAPTER-34: claimed Connect wakes cannot sit unspawned until the 900s deadline.

Live COORD-63: host claimed wake-49a8279d444b41e4 then never completed_wake /
registered a runner for ~10 minutes. DHCP requires a terminal wake so Autopilot
can advance wake-generation and retry. Sweep fails claimed Connect wakes that
exceed a short claim-hold without a runner_session_id.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time

from path_setup import ROOT  # noqa: F401

_TMP = tempfile.mkdtemp(prefix="adapter34-claim-hold-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_CONNECT_CLAIM_HOLD_SECONDS"] = "90"

import store  # noqa: E402
from db.connection import _conn  # noqa: E402

P = "switchboard"
BASE = 1_700_000_000.0
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    store.init_db(P)
    wake = store.request_wake(
        {"runtime": "codex", "task_id": "COORD-63", "agent_id": "agent/codex/coord-63"},
        reason="Connect assignment COORD-63",
        source="connect",
        policy={"mode": "connect"},
        task_id="COORD-63",
        project=P,
        idem_key="adapter34-claim-hold-1",
    )
    ok(bool(wake.get("wake_id")), f"wake created ({wake})")
    wake_id = wake["wake_id"]
    claimed_at = BASE
    with _conn(P) as c:
        c.execute(
            "UPDATE wake_intents SET status='claimed', claimed_at=?, "
            "claimed_by_host='host/steve-mbp', runner_session_id=NULL, "
            "deadline=? WHERE wake_id=?",
            (claimed_at, claimed_at + 900, wake_id),
        )

    # Still inside the hold window — leave claimed alone (host may still be launching).
    early = store.sweep_wake_intents(project=P, now=claimed_at + 30)
    early_reasons = {e.get("reason") for e in (early.get("events") or [])
                     if e.get("wake_id") == wake_id}
    ok("connect_claim_hold_expired" not in early_reasons,
       "inside claim-hold window: Connect claimed wake is not failed yet")

    # Past hold, still before placement deadline, still no runner → fail closed.
    swept = store.sweep_wake_intents(project=P, now=claimed_at + 91)
    events = [e for e in (swept.get("events") or []) if e.get("wake_id") == wake_id]
    ok(any(e.get("reason") == "connect_claim_hold_expired" for e in events),
       f"past claim-hold: fail with connect_claim_hold_expired (events={events})")
    row = store.list_wake_intents(project=P, task_id="COORD-63", newest_first=True, limit=1)
    ok(row and row[0].get("status") == "failed",
       f"wake is terminal failed so DHCP generation can advance (got {row})")

    # Spawned runner must not be killed by claim-hold.
    wake2 = store.request_wake(
        {"runtime": "codex", "task_id": "COORD-64", "agent_id": "agent/codex/coord-64"},
        reason="Connect assignment COORD-64",
        source="connect",
        policy={"mode": "connect"},
        task_id="COORD-64",
        project=P,
        idem_key="adapter34-claim-hold-2",
    )
    with _conn(P) as c:
        c.execute(
            "UPDATE wake_intents SET status='claimed', claimed_at=?, "
            "claimed_by_host='host/ok', runner_session_id='run_live', deadline=? "
            "WHERE wake_id=?",
            (BASE, BASE + 900, wake2["wake_id"]),
        )
    keep = store.sweep_wake_intents(project=P, now=BASE + 500)
    keep_fail = [e for e in (keep.get("events") or [])
                 if e.get("wake_id") == wake2["wake_id"]
                 and e.get("reason") == "connect_claim_hold_expired"]
    ok(not keep_fail, "claimed Connect wake with runner_session_id survives claim-hold")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nADAPTER-34 Connect claim hold: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
