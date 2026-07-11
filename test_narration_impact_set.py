#!/usr/bin/env python3
"""NARRATE-11: dependency-aware task->deliverable narration invalidation (minimum impact set).

Proves (no network) that a task change enqueues that task and ONLY the linked deliverables whose
narrative inputs actually moved, with a bounded, observable fan-out and no full-project scan:
- linking a task enqueues only that deliverable (an unrelated deliverable's revision never moves);
- a task status change invalidates its linked deliverable, examined==direct-link-count;
- a cosmetic task edit (description) invalidates no deliverable;
- a linked task's terminal-provenance change invalidates the deliverable;
- unlinking enqueues the deliverable, and afterwards that task no longer invalidates it;
- a milestone upsert enqueues only its deliverable;
- every deliverable outbox row is a valid narration_requested.v1 envelope.

`deliv-beta` is an unrelated deliverable whose revision is the witness that the impact set stayed
minimal — operations on alpha must never advance beta.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="narrate-impact-test-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
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


def drev(did):
    with narration_outbox._conn(PROJECT) as c:
        r = c.execute("SELECT narration_source_revision FROM deliverables WHERE id=?", (did,)).fetchone()
    return (r[0] or 0) if r else None


def deliverable_outbox(did):
    return [e for e in narration_outbox.list_narration_outbox(PROJECT, limit=1000)
            if e["entity_type"] == "deliverable" and e["entity_id"] == did]


try:
    store.init_db(PROJECT)
    store.create_deliverable({"id": "deliv-alpha", "title": "Alpha", "status": "in_progress"},
                             actor="test", project=PROJECT)
    store.create_deliverable({"id": "deliv-beta", "title": "Beta"}, actor="test", project=PROJECT)
    ta = store.create_task({"workstream_id": "IMP", "title": "Task A"}, actor="test", project=PROJECT)
    taid = ta["task_id"]
    tb = store.create_task({"workstream_id": "IMP", "title": "Task B"}, actor="test", project=PROJECT)
    tbid = tb["task_id"]
    beta_witness = drev("deliv-beta")  # unrelated deliverable; must never move on alpha ops

    # 1. linking a task enqueues only that deliverable.
    rev0 = drev("deliv-alpha")
    store.link_task_to_deliverable("deliv-alpha", PROJECT, taid, actor="test", project=PROJECT)
    store.link_task_to_deliverable("deliv-alpha", PROJECT, tbid, actor="test", project=PROJECT)
    ok(drev("deliv-alpha") > rev0 and drev("deliv-beta") == beta_witness,
       "linking a task enqueues only the linked deliverable (unrelated one untouched)")

    # 2. a task STATUS change invalidates its linked deliverable; fan-out is bounded + observable.
    rev1 = drev("deliv-alpha")
    summary = narration_outbox.invalidate_linked_deliverables(taid, PROJECT, actor="test")
    ok(any(iv["deliverable_id"] == "deliv-alpha" for iv in summary["invalidated"]) is False,
       "no-op fan-out when nothing changed since last emit (idempotent)")
    store.update_task(taid, {"status": "In Review"}, actor="test", project=PROJECT)
    ok(drev("deliv-alpha") > rev1, "a task status change invalidates its linked deliverable")
    after_status = narration_outbox.invalidate_linked_deliverables(taid, PROJECT, actor="test")
    ok(after_status["scanned_full_project"] is False and after_status["examined"] == 1,
       "fan-out is bounded to the task's direct links and never scans the full project")
    ok(drev("deliv-beta") == beta_witness, "unrelated deliverable still untouched (minimum impact set)")

    # 3. a cosmetic task edit (description only) invalidates no deliverable.
    rev2 = drev("deliv-alpha")
    store.update_task(taid, {"description": "wording tweak"}, actor="test", project=PROJECT)
    ok(drev("deliv-alpha") == rev2,
       "a cosmetic task edit invalidates no deliverable (projection unchanged)")

    # 4. a linked task's terminal-provenance change invalidates the deliverable.
    rev3 = drev("deliv-alpha")
    with narration_outbox._conn(PROJECT) as c:
        c.execute("INSERT INTO task_git_state (task_id, merged_sha, updated_at) VALUES (?,?,?) "
                  "ON CONFLICT(task_id) DO UPDATE SET merged_sha=excluded.merged_sha",
                  (taid, "a" * 40, 1.0))
    prov = narration_outbox.invalidate_linked_deliverables(taid, PROJECT, actor="test")
    ok(any(iv["deliverable_id"] == "deliv-alpha" for iv in prov["invalidated"])
       and drev("deliv-alpha") > rev3,
       "a linked task's provenance change invalidates the deliverable")

    # 5. unlink enqueues the deliverable, and afterwards that task no longer invalidates it.
    rev4 = drev("deliv-alpha")
    store.unlink_task_from_deliverable("deliv-alpha", PROJECT, taid, actor="test", project=PROJECT)
    ok(drev("deliv-alpha") > rev4, "unlinking enqueues the deliverable itself")
    post = narration_outbox.invalidate_linked_deliverables(taid, PROJECT, actor="test")
    ok(post["examined"] == 0 and not post["invalidated"],
       "after unlinking, the task no longer invalidates the deliverable (dependency-aware)")

    # 6. milestone upsert enqueues only its deliverable.
    rev5 = drev("deliv-alpha")
    store.add_deliverable_milestone("deliv-alpha", {"title": "M-one", "status": "in_progress"},
                                    actor="test", project=PROJECT)
    ok(drev("deliv-alpha") > rev5 and drev("deliv-beta") == beta_witness,
       "a milestone upsert enqueues only its deliverable")

    # 7. every deliverable outbox row is a valid v1 envelope for entity_type=deliverable.
    good = bool(deliverable_outbox("deliv-alpha"))
    for e in deliverable_outbox("deliv-alpha") + deliverable_outbox("deliv-beta"):
        try:
            narration_events.validate_narration_requested(e, expected_project=PROJECT)
            good = good and e["entity_type"] == "deliverable"
        except narration_events.NarrationEventValidationError:
            good = False
    ok(good, "deliverable outbox rows are valid narration_requested.v1 envelopes")

except Exception as exc:  # pragma: no cover
    import traceback
    traceback.print_exc()
    ok(False, f"unexpected exception: {exc}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
