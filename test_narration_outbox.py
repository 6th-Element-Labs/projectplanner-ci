#!/usr/bin/env python3
"""NARRATE-8: transactional narration outbox — atomic emit, dedupe, materiality, backfill.

Proves the producer-half invariants from ADR-0008 against a real SQLite store (no network):
- a committed create/update mutation carries exactly one durable request for its revision;
- a rolled-back mutation leaves neither the mutation nor the outbox row (atomicity);
- cosmetic edits bump no revision and emit nothing (cost guarantee);
- a material edit bumps the revision monotonically and emits the next revision;
- a retried identical emit is idempotent on the unique dedupe_key;
- every emitted row satisfies the executable narration_events contract;
- backfill sets revision 1 without emitting historical work;
- the legacy pending_narrations marker still shadows the outbox;
- the PM_NARRATION_OUTBOX kill switch disables emit.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="narrate-outbox-test-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
# PERF-2 single-writer proxy commits mutating statements on a worker thread, which
# breaks this test's direct-transaction atomicity assertions — disable for hermetic proof.
os.environ["PM_SQLITE_SINGLE_WRITER"] = "0"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import narration_events  # noqa: E402
import narration_outbox  # noqa: E402
import store  # noqa: E402

PROJECT = store.DEFAULT_PROJECT
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def outbox(states=None):
    return narration_outbox.list_narration_outbox(PROJECT, states=states, limit=500)


def task_rev(task_id):
    with narration_outbox._conn(PROJECT) as c:
        r = c.execute("SELECT narration_source_revision AS rev, narration_source_hash AS h "
                      "FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return (r["rev"], r["h"])


try:
    store.init_db(PROJECT)

    # 1. create_task emits exactly one revision-1 request, atomically with the insert.
    t = store.create_task({"workstream_id": "NAR", "title": "Ship the widget"},
                          actor="test", project=PROJECT)
    tid = t["task_id"]
    rows = [e for e in outbox() if e["entity_id"] == tid]
    ok(len(rows) == 1 and rows[0]["source_revision"] == 1 and rows[0]["entity_type"] == "task",
       "create_task emits one revision-1 task request")
    ok(task_rev(tid)[0] == 1, "create bumps the entity narration_source_revision to 1")

    # 2. every emitted row satisfies the executable contract.
    contract_ok = True
    for e in outbox():
        try:
            narration_events.validate_narration_requested(e, expected_project=PROJECT)
        except narration_events.NarrationEventValidationError:
            contract_ok = False
    ok(contract_ok, "every outbox row validates against narration_events contract")

    # 3. the legacy pending_narrations marker still shadows the outbox.
    ok(any(p["task_id"] == tid for p in store.list_pending_narrations(project=PROJECT)),
       "legacy pending_narrations marker is still written alongside the outbox")

    # 4. a material status change bumps to revision 2 and emits it.
    store.update_task(tid, {"status": "In Progress"}, actor="test", project=PROJECT)
    revs = sorted(e["source_revision"] for e in outbox() if e["entity_id"] == tid)
    ok(revs == [1, 2] and task_rev(tid)[0] == 2,
       "material status change emits revision 2 (monotonic bump)")

    # 5. a cosmetic edit (non-projected field) bumps nothing and emits nothing.
    before = len([e for e in outbox() if e["entity_id"] == tid])
    store.update_task(tid, {"assignee": "someone-else"}, actor="test", project=PROJECT)
    after = len([e for e in outbox() if e["entity_id"] == tid])
    ok(after == before and task_rev(tid)[0] == 2,
       "cosmetic edit emits nothing and does not bump the revision")

    # 6. an unchanged status re-write (same projection) is a no-op emit.
    store.update_task(tid, {"status": "In Progress"}, actor="test", project=PROJECT)
    ok(len([e for e in outbox() if e["entity_id"] == tid]) == after,
       "re-writing the same status emits no new revision")

    # 7. dedupe_key uniqueness: a manual re-emit of the current revision is idempotent.
    with narration_outbox._conn(PROJECT) as c:
        e2 = [e for e in outbox() if e["entity_id"] == tid and e["source_revision"] == 2][0]
        # Force the entity back so _emit recomputes revision 2 with the same dedupe material.
        c.execute("UPDATE tasks SET narration_source_revision=1 WHERE task_id=?", (tid,))
        narration_outbox.emit_task_narration_request(
            c, tid, project=PROJECT, cause_kind="task.updated", actor="test")
    dupes = [e for e in outbox() if e["entity_id"] == tid and e["source_revision"] == 2]
    ok(len(dupes) == 1, "re-emitting the same revision is idempotent on dedupe_key")

    # 8. atomicity: if the mutation transaction raises, neither mutation nor outbox row commit.
    t3 = store.create_task({"workstream_id": "NAR", "title": "Rollback probe"},
                           actor="test", project=PROJECT)
    rb_id = t3["task_id"]
    base_rev = task_rev(rb_id)[0]
    base_rows = len([e for e in outbox() if e["entity_id"] == rb_id])
    raised = False
    try:
        with narration_outbox._conn(PROJECT) as c:
            c.execute("UPDATE tasks SET status='Done' WHERE task_id=?", (rb_id,))
            narration_outbox.emit_task_narration_request(
                c, rb_id, project=PROJECT, cause_kind="task.updated", actor="test")
            raise RuntimeError("boom after emit, before commit")
    except RuntimeError:
        raised = True
    with narration_outbox._conn(PROJECT) as c:
        status_now = c.execute("SELECT status FROM tasks WHERE task_id=?", (rb_id,)).fetchone()[0]
    ok(raised and status_now != "Done" and task_rev(rb_id)[0] == base_rev
       and len([e for e in outbox() if e["entity_id"] == rb_id]) == base_rows,
       "a raised transaction rolls back the mutation AND the outbox emit together")

    # 9. backfill sets revision 1 for a never-projected task without emitting.
    with narration_outbox._conn(PROJECT) as c:
        c.execute("INSERT INTO tasks (task_id, workstream_id, workstream_name, title, status, "
                  "sort_order, created_at, updated_at, narration_source_revision) "
                  "VALUES ('NAR-legacy','NAR','NAR','Legacy task','Not Started',99,0,0,0)")
    pre_rows = len(outbox())
    result = narration_outbox.backfill_narration_source_revisions(PROJECT)
    ok(result["tasks"] >= 1 and task_rev("NAR-legacy")[0] == 1 and len(outbox()) == pre_rows,
       "backfill sets revision 1 without emitting a provider request")
    ok(narration_outbox.backfill_narration_source_revisions(PROJECT)["tasks"] == 0,
       "backfill is idempotent (re-run touches no rows)")

    # 9b. a task id that cannot form a valid envelope (workstream_id is not slugified, so a
    #     space yields "My Feature-1") must still create — emit skips, never rolls back.
    unsafe = store.create_task({"workstream_id": "My Feature", "title": "Unsafe id"},
                               actor="user@example.com", project=PROJECT)
    ok(isinstance(unsafe, dict) and unsafe.get("task_id")
       and not [e for e in outbox() if e["entity_id"] == unsafe.get("task_id")],
       "task with an unsafe id still creates; emit is skipped, not rolled back")

    # 10. kill switch disables emit.
    os.environ["PM_NARRATION_OUTBOX"] = "0"
    try:
        t4 = store.create_task({"workstream_id": "NAR", "title": "Flag off"},
                               actor="test", project=PROJECT)
        ok(not [e for e in outbox() if e["entity_id"] == t4["task_id"]],
           "PM_NARRATION_OUTBOX=0 disables the atomic emit")
    finally:
        os.environ.pop("PM_NARRATION_OUTBOX", None)

except Exception as exc:  # pragma: no cover - surfaces setup/import failures as a hard fail
    import traceback
    traceback.print_exc()
    ok(False, f"unexpected exception: {exc}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
