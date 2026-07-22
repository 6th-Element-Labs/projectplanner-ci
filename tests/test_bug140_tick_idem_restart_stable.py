#!/usr/bin/env python3
"""BUG-140: coordinator tick idempotency must survive daemon restarts.

Every Autopilot daemon restart mints a new per-instance coordinator_agent_id.
That id was hashed into run_mission_coordinator_tick's idempotency payload, so
a restarted daemon replaying its own durable key (ui30:...:wake-generation-N)
got "idempotency conflict" instead of the stored receipt -- and because a
conflicted tick dispatches nothing, no terminal wake was created and the
generation could never advance the key: the scope dead-looped until an
unrelated task edit changed the revision. Observed live on the
deliverable-watch-chat-truth scopes after the BUG-138 deploy restart.

Contract pinned here: the idem payload hash contains only restart-stable
request semantics. Replaying the same key from a different daemon instance
returns the stored receipt (the crash-replay contract), never a conflict.
"""
import os
import shutil
import tempfile

from path_setup import ROOT  # noqa: F401

_TMP = tempfile.mkdtemp(prefix="bug140-tick-idem-")
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
    KEY = "ui30:autopilot-test:TASK-X:rev1:wake-generation-1:policy-abc"

    # Instance A ticks and stores a receipt under the durable key. (An unknown
    # deliverable keeps the fixture minimal; the error result is stored under
    # the key exactly like a dispatch receipt would be.)
    first = store.run_mission_coordinator_tick(
        project="switchboard", deliverable_id="deliverable-missing",
        coordinator_agent_id="switchboard/coordinator-autopilot/instance-a",
        actor="autopilot", idem_key=KEY, policy={"auto_start": True})
    ok(isinstance(first, dict) and first.get("error") != "idempotency conflict",
       f"instance A's tick stores a receipt (got {str(first.get('error'))[:60]})")

    # Instance B (post-restart) replays the SAME durable key. It must receive
    # the stored receipt -- never an idempotency conflict.
    second = store.run_mission_coordinator_tick(
        project="switchboard", deliverable_id="deliverable-missing",
        coordinator_agent_id="switchboard/coordinator-autopilot/instance-b",
        actor="autopilot", idem_key=KEY, policy={"auto_start": True})
    ok(second.get("error") != "idempotency conflict",
       "a restarted daemon replaying its durable key is NOT told 'idempotency conflict'")
    ok(second == first,
       "the replay returns the stored receipt byte-for-byte (crash-replay contract)")

    # A genuinely different request under the same key must still conflict:
    # the guard exists to catch semantic drift, not instance identity.
    drifted = store.run_mission_coordinator_tick(
        project="switchboard", deliverable_id="deliverable-DIFFERENT",
        coordinator_agent_id="switchboard/coordinator-autopilot/instance-b",
        actor="autopilot", idem_key=KEY, policy={"auto_start": True})
    ok(drifted.get("error") == "idempotency conflict",
       "a semantically different request on the same key still fails closed")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nBUG-140 restart-stable tick idempotency: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
