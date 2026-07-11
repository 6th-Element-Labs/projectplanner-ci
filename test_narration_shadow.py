#!/usr/bin/env python3
"""NARRATE-10: shadow-mode comparison of the legacy queue vs the event-driven outbox.

Proves (no network, no visible publish):
- the read-only event impact set coalesces a burst into one freshest request per entity;
- it stale-suppresses a request the entity has advanced past;
- the legacy impact set applies the trigger-status filter;
- compare_narration_paths reports only_event surplus (broader event triggering) with an empty
  only_legacy in the healthy case, and detects only_legacy drift when a legacy marker has no
  matching fresh outbox request;
- compare and the impact sets are side-effect free (they neither drain the outbox nor clear
  the legacy queue);
- run_shadow_drain exercises the worker end-to-end and writes NO visible narration.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="narrate-shadow-test-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_NARRATE_TRIGGERS"] = "create,In Review,Done,Blocked"  # pin the legacy filter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import narration_outbox  # noqa: E402
import narration_shadow  # noqa: E402
import narration_worker  # noqa: E402
import store  # noqa: E402

PROJECT = store.DEFAULT_PROJECT
passed = failed = 0

BASE = 1_000_000.0
narration_outbox._now = lambda now=None: BASE if now is None else now


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_db(PROJECT)

    # 1. coalescing: a create + two status changes collapse to one impact at the freshest rev.
    t = store.create_task({"workstream_id": "SH", "title": "Coalesce"}, actor="user", project=PROJECT)
    tid = t["task_id"]
    store.update_task(tid, {"status": "In Review"}, actor="user", project=PROJECT)  # rev 2
    store.update_task(tid, {"status": "Blocked"}, actor="user", project=PROJECT)     # rev 3 (applies)
    ev = narration_shadow.event_impact_set(PROJECT)
    ok(ev["impacts"] == [tid] and ev["coalesced"] >= 2,
       "event impact set coalesces the burst into one freshest request")

    # 2. side-effect-free: the preview neither claimed nor consumed the queued revisions.
    ok(narration_worker.count_actionable(PROJECT, now=BASE + 1) >= 3,
       "computing the impact set does not consume outbox rows")

    # 3. healthy compare on clean state: the one trigger task is in BOTH, no legacy-only drift.
    healthy = narration_shadow.compare_narration_paths(PROJECT)
    ok(healthy["both"] == [tid] and healthy["only_legacy"] == [] and healthy["in_sync"],
       "compare reports in-sync when every legacy task has a fresh outbox request")

    # 4. only_event surplus: a non-trigger status change narrates on the event path but not legacy.
    t3 = store.create_task({"workstream_id": "SH", "title": "NonTrigger"}, actor="user", project=PROJECT)
    t3id = t3["task_id"]
    store.clear_pending_narration(t3id, project=PROJECT)  # drop the create marker
    store.update_task(t3id, {"status": "In Progress"}, actor="user", project=PROJECT)  # not a trigger
    legacy = narration_shadow.legacy_impact_set(PROJECT)
    ok(t3id not in legacy["impacts"] and legacy["filtered_out"] >= 1,
       "legacy impact set filters out a non-trigger status change")
    ok(tid in legacy["impacts"], "legacy impact set includes a trigger-status task")
    surplus = narration_shadow.compare_narration_paths(PROJECT)
    ok(t3id in surplus["only_event"] and surplus["in_sync"],
       "compare surfaces the event-only surplus (broader triggering) with no legacy drift")

    # 5. stale suppression: advance the entity past every queued revision → dropped, no impact.
    t2 = store.create_task({"workstream_id": "SH", "title": "Advancer"}, actor="user", project=PROJECT)
    t2id = t2["task_id"]
    with narration_outbox._conn(PROJECT) as c:
        c.execute("UPDATE tasks SET narration_source_revision=9, narration_source_hash='sha256:"
                  + ("b" * 64) + "' WHERE task_id=?", (t2id,))
    ev2 = narration_shadow.event_impact_set(PROJECT)
    ok(t2id not in ev2["impacts"] and ev2["stale_suppressed"] >= 1,
       "a request the entity advanced past is stale-suppressed with no impact")

    # 6. drift detection: a queued trigger marker whose outbox rows are all superseded → only_legacy.
    with narration_outbox._conn(PROJECT) as c:
        c.execute("UPDATE narration_outbox SET attempt_state='superseded', claimed_by=NULL, "
                  "lease_expires_at=NULL WHERE entity_id=?", (tid,))
    store.enqueue_narration(tid, status="Blocked", reason="status_change", project=PROJECT)
    drift = narration_shadow.compare_narration_paths(PROJECT)
    ok(tid in drift["only_legacy"] and not drift["in_sync"],
       "compare detects legacy-only drift when the outbox has no fresh request for a queued task")

    # 7. compare is side-effect free: the legacy queue and outbox are unchanged by comparing.
    q_before = len(store.list_pending_narrations(project=PROJECT))
    a_before = narration_worker.count_actionable(PROJECT, now=BASE + 1)
    narration_shadow.compare_narration_paths(PROJECT)
    ok(len(store.list_pending_narrations(project=PROJECT)) == q_before
       and narration_worker.count_actionable(PROJECT, now=BASE + 1) == a_before,
       "compare_narration_paths mutates neither the legacy queue nor the outbox")

    # 8. run_shadow_drain exercises the worker and writes NO visible narration.
    t4 = store.create_task({"workstream_id": "SH", "title": "ShadowDrain"}, actor="user", project=PROJECT)
    t4id = t4["task_id"]
    result = narration_shadow.run_shadow_drain(PROJECT, now_fn=lambda: BASE + 10_000)
    drained_ids = {r["entity_id"] for r in result["records"]}
    ok(t4id in drained_ids and all(r["shadow"] for r in result["records"]),
       "run_shadow_drain records the request it would have narrated")
    ok(store.get_task_narration(t4id, project=PROJECT) is None,
       "run_shadow_drain publishes no visible task narration")
    ok(all(o == "delivered" for (_id, o) in result["outcomes"]),
       "shadow drain settles every claimed request cleanly")

except Exception as exc:  # pragma: no cover
    import traceback
    traceback.print_exc()
    ok(False, f"unexpected exception: {exc}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
