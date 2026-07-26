#!/usr/bin/env python3
"""ADAPTER-34: Autopilot tick idem must ignore lease clock churn on scope_authority.

BUG-140 stripped coordinator_agent_id from the hash. Lease renew still mutates
expires_at / heartbeat_at / lease_id inside scope_authority, which poisoned the
same s15:...:wake-generation-N key into 'idempotency conflict' and blocked DHCP
restarts (DOGFOOD-25 after kill/re-arm).
"""
from __future__ import annotations

import os
import shutil
import tempfile

from path_setup import ROOT  # noqa: F401

_TMP = tempfile.mkdtemp(prefix="adapter34-tick-idem-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    store.init_db("switchboard")
    KEY = "s15:autopilot-test:1:1:DOGFOOD-25:rev1:wake-generation-1:policy-abc"
    authority_a = {
        "schema": "switchboard.autopilot_scope_authority.v1",
        "scope_id": "autopilot-test",
        "deliverable_id": "project-independent-execution-plane",
        "fence_epoch": 1,
        "generation": 1,
        "holder_agent_id": "switchboard/coordinator-autopilot/aaa",
        "lease_id": "scopelease-aaa",
        "expires_at": 1000.0,
        "heartbeat_at": 900.0,
        "renewed": True,
    }
    first = store.run_mission_coordinator_tick(
        project="switchboard",
        deliverable_id="deliverable-missing",
        coordinator_agent_id="switchboard/coordinator-autopilot/aaa",
        actor="autopilot",
        idem_key=KEY,
        policy={"auto_start": True, "target_task_id": "DOGFOOD-25"},
        scope_authority=authority_a,
    )
    ok(isinstance(first, dict) and first.get("error") != "idempotency conflict",
       f"first tick stores a receipt (got {str(first.get('error'))[:80]})")

    authority_b = dict(authority_a)
    authority_b.update({
        "lease_id": "scopelease-bbb",
        "expires_at": 2000.0,
        "heartbeat_at": 1900.0,
        "holder_agent_id": "switchboard/coordinator-autopilot/bbb",
        "renewed": True,
    })
    second = store.run_mission_coordinator_tick(
        project="switchboard",
        deliverable_id="deliverable-missing",
        coordinator_agent_id="switchboard/coordinator-autopilot/bbb",
        actor="autopilot",
        idem_key=KEY,
        policy={"auto_start": True, "target_task_id": "DOGFOOD-25"},
        scope_authority=authority_b,
    )
    ok(second.get("error") != "idempotency conflict",
       "lease renew / holder churn must NOT conflict the durable s15 key")
    ok(second == first,
       "replay returns the stored receipt (crash-replay + lease-renew contract)")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nADAPTER-34 tick idem authority: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
