#!/usr/bin/env python3
"""NARRATE-9: wakeable narration worker — claim, lease, coalesce, retry, recover.

Proves the consumer-half invariants from ADR-0008 against a real SQLite store (no network,
provider injected):
- claim atomically moves the current revision to claimed with a bounded lease + attempt bump;
- a claimed row is not re-claimed while its lease is valid (single live lease per request);
- an expired lease is recoverable by another worker (crash recovery);
- coalescing: an older pending revision is superseded without a provider call when a newer
  revision exists for the same entity;
- stale suppression: a request older than the entity's current revision is superseded, not run;
- retry uses bounded backoff and escalates to dead_letter at the attempt ceiling, retaining the
  error; every transitioned row still satisfies the executable contract (lease cleared etc.);
- drain delivers on success and retries on provider failure;
- the post-commit wake fires through a registered sink.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="narrate-worker-test-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import narration_events  # noqa: E402
import narration_outbox  # noqa: E402
import narration_worker  # noqa: E402
import store  # noqa: E402

PROJECT = store.DEFAULT_PROJECT
passed = failed = 0

# Pin emit timestamps to a controllable timeline so claim/lease/retry math shares one clock
# with the outbox rows (otherwise emit uses real wall-clock and the fake claim times below make
# no sense relative to available_at). BASE is safely in the past so envelope validation accepts it.
BASE = 1_000_000.0
narration_outbox._now = lambda now=None: BASE if now is None else now


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def row(event_id):
    with narration_outbox._conn(PROJECT) as c:
        r = c.execute("SELECT * FROM narration_outbox WHERE event_id=?", (event_id,)).fetchone()
    return narration_outbox._row_to_event(r) if r else None


def rows_for(entity_id, state=None):
    out = [e for e in narration_outbox.list_narration_outbox(PROJECT, limit=500)
           if e["entity_id"] == entity_id]
    return [e for e in out if state is None or e["attempt"]["state"] == state]


def contract_valid(event):
    try:
        narration_events.validate_narration_requested(event, expected_project=PROJECT)
        return True
    except narration_events.NarrationEventValidationError as exc:
        print("    (contract violation:", exc.code, exc, ")")
        return False


try:
    store.init_db(PROJECT)

    # 1. claim moves the row to claimed with a lease + attempt bump; stays contract-valid.
    t = store.create_task({"workstream_id": "NW", "title": "Claimable"}, actor="user", project=PROJECT)
    tid = t["task_id"]
    claimed = narration_worker.claim_next_narration(PROJECT, worker_id="w1", now=BASE+10)
    ok(claimed is not None and claimed["entity_id"] == tid
       and claimed["attempt"]["state"] == "claimed"
       and claimed["attempt"]["claimed_by"] == "w1"
       and claimed["attempt"]["count"] == 1
       and claimed["attempt"]["lease_expires_at"] > claimed["attempt"]["available_at"],
       "claim assigns claimed state, worker, lease, and attempt count 1")
    ok(contract_valid(row(claimed["event_id"])), "a claimed row satisfies the contract")

    # 2. a valid lease blocks re-claiming (only one live lease per request).
    again = narration_worker.claim_next_narration(PROJECT, worker_id="w2", now=BASE+11)
    ok(again is None, "a live lease is not re-claimed by another worker")

    # 3. an expired lease is recoverable (crash recovery); attempt count increments.
    reclaimed = narration_worker.claim_next_narration(
        PROJECT, worker_id="w2", now=BASE + narration_worker.DEFAULT_LEASE_SECONDS + 20)
    ok(reclaimed is not None and reclaimed["event_id"] == claimed["event_id"]
       and reclaimed["attempt"]["claimed_by"] == "w2" and reclaimed["attempt"]["count"] == 2,
       "an expired lease is reclaimed by another worker with a bumped attempt count")
    narration_worker.mark_delivered(
        PROJECT, reclaimed["event_id"], worker_id=reclaimed["attempt"]["claimed_by"],
        lease=reclaimed["attempt"]["lease_expires_at"], now=BASE+200)
    ok(contract_valid(row(reclaimed["event_id"]))
       and row(reclaimed["event_id"])["attempt"]["state"] == "delivered"
       and row(reclaimed["event_id"])["attempt"]["lease_expires_at"] is None,
       "delivered clears the lease and stays contract-valid")

    # 4. coalescing: two pending revisions of one entity — claim runs the newest, supersedes the old.
    store.update_task(tid, {"status": "In Progress"}, actor="user", project=PROJECT)  # rev 2
    store.update_task(tid, {"status": "In Review"}, actor="user", project=PROJECT)    # rev 3
    pend = sorted(e["source_revision"] for e in rows_for(tid, "pending"))
    ok(pend == [2, 3], "two newer pending revisions exist before claim")
    claim3 = narration_worker.claim_next_narration(PROJECT, worker_id="w1", now=BASE+300)
    ok(claim3 is not None and claim3["source_revision"] == 3
       and claim3["attempt"]["state"] == "claimed",
       "claim runs the freshest revision (3)")
    superseded_revs = sorted(e["source_revision"] for e in rows_for(tid, "superseded"))
    ok(2 in superseded_revs, "the older pending revision (2) is superseded without a provider call")
    ok(not rows_for(tid, "pending"), "no pending revisions remain for the coalesced entity")
    narration_worker.mark_delivered(
        PROJECT, claim3["event_id"], worker_id=claim3["attempt"]["claimed_by"],
        lease=claim3["attempt"]["lease_expires_at"], now=BASE+301)

    # 4b. lease fencing: a zombie worker whose lease expired and was reclaimed cannot settle.
    tz = store.create_task({"workstream_id": "NW", "title": "Zombie"}, actor="user", project=PROJECT)
    tzid = tz["task_id"]
    w1_claim = narration_worker.claim_next_narration(PROJECT, worker_id="zw1", now=BASE + 4000)
    # lease expires; a second worker reclaims the same request.
    w2_claim = narration_worker.claim_next_narration(
        PROJECT, worker_id="zw2",
        now=BASE + 4000 + narration_worker.DEFAULT_LEASE_SECONDS + 5)
    ok(w2_claim is not None and w2_claim["event_id"] == w1_claim["event_id"]
       and w2_claim["attempt"]["claimed_by"] == "zw2",
       "an expired-lease request is reclaimed by a second worker")
    # the zombie w1 returns late and tries to settle with its stale lease → no-op.
    zombie_ok = narration_worker.mark_delivered(
        PROJECT, w1_claim["event_id"], worker_id="zw1",
        lease=w1_claim["attempt"]["lease_expires_at"], now=BASE + 4200)
    after = row(w1_claim["event_id"])
    ok(zombie_ok is False and after["attempt"]["state"] == "claimed"
       and after["attempt"]["claimed_by"] == "zw2",
       "a zombie worker's stale-lease settle is dropped and does not clobber the new owner")
    narration_worker.mark_delivered(
        PROJECT, w2_claim["event_id"], worker_id="zw2",
        lease=w2_claim["attempt"]["lease_expires_at"], now=BASE + 4300)

    # 5. stale suppression: a request older than the entity's current revision is superseded.
    t2 = store.create_task({"workstream_id": "NW", "title": "Advancer"}, actor="user", project=PROJECT)
    t2id = t2["task_id"]
    ev_old = rows_for(t2id, "pending")[0]["event_id"]
    with narration_outbox._conn(PROJECT) as c:  # entity jumps ahead of the queued revision
        c.execute("UPDATE tasks SET narration_source_revision=9, narration_source_hash='sha256:"
                  + ("a" * 64) + "' WHERE task_id=?", (t2id,))
    res = narration_worker.claim_next_narration(PROJECT, worker_id="w1", now=BASE+400)
    ok((res is None or res["entity_id"] != t2id)
       and row(ev_old)["attempt"]["state"] == "superseded",
       "a request behind the current entity revision is stale-suppressed, not claimed")
    ok(contract_valid(row(ev_old)), "a superseded row satisfies the contract")

    # 6. retry backoff → dead_letter at the attempt ceiling; error retained; row stays valid.
    t3 = store.create_task({"workstream_id": "NW", "title": "Flaky"}, actor="user", project=PROJECT)
    t3id = t3["task_id"]
    clk = [BASE]
    def now_fn():
        clk[0] += 100000.0  # always past any backoff window so the retry re-claims immediately
        return clk[0]
    def boom(_event):
        raise RuntimeError("provider down")
    outcomes = narration_worker.drain(PROJECT, worker_id="w1", generate=boom, now_fn=now_fn,
                                      max_items=20, max_attempts=3, jitter=0.0)
    ev3 = rows_for(t3id)[0]["event_id"]
    final = row(ev3)
    seq = [o for (eid, o) in outcomes if eid == ev3]
    ok(seq[-1] == "dead_letter" and seq.count("retry_wait") >= 1,
       "provider failures back off then escalate to dead_letter at the ceiling")
    ok(final["attempt"]["state"] == "dead_letter" and (final["attempt"]["last_error"] or "")
       and final["attempt"]["lease_expires_at"] is None,
       "dead_letter retains the error and clears the lease")
    ok(contract_valid(final), "a dead_letter row satisfies the contract")

    # 7. drain success path delivers.
    t4 = store.create_task({"workstream_id": "NW", "title": "Happy"}, actor="user", project=PROJECT)
    t4id = t4["task_id"]
    seen = []
    ok_outcomes = narration_worker.drain(PROJECT, worker_id="w1",
                                         generate=lambda e: seen.append(e["event_id"]),
                                         now_fn=lambda: 2_000_000.0, max_items=10)
    ev4 = rows_for(t4id)[0]["event_id"]
    ok(("delivered" in [o for (eid, o) in ok_outcomes if eid == ev4])
       and row(ev4)["attempt"]["state"] == "delivered" and ev4 in seen,
       "drain generates then marks the request delivered")

    # 8. the post-commit wake fires through a registered sink.
    wakes = []
    narration_outbox.register_wake_sink(lambda project, **ctx: wakes.append((project, ctx)))
    try:
        t5 = store.create_task({"workstream_id": "NW", "title": "Wakes"}, actor="user", project=PROJECT)
        ok(any(p == PROJECT and c.get("entity_id") == t5["task_id"] for (p, c) in wakes),
           "creating a task fires a post-commit wake with project + entity context")
    finally:
        narration_outbox.register_wake_sink(None)

    # 9. count_actionable / list_actionable reflect only actionable rows.
    ok(isinstance(narration_worker.count_actionable(PROJECT, now=9e9), int),
       "count_actionable returns an int over the recovery-sweep predicate")

    # 10. concurrency: many drainers racing one pending request yield exactly one winner
    #     (ADR: recovery timer overlapping an active worker must not double-claim).
    import threading
    t6 = store.create_task({"workstream_id": "NW", "title": "Contended"}, actor="user", project=PROJECT)
    t6id = t6["task_id"]
    ev6 = rows_for(t6id, "pending")[0]["event_id"]
    winners = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def race(worker):
        barrier.wait()  # maximize overlap on the claim
        got = narration_worker.claim_next_narration(PROJECT, worker_id=worker, now=BASE + 5000)
        if got is not None and got["event_id"] == ev6:
            with lock:
                winners.append(worker)

    threads = [threading.Thread(target=race, args=(f"w{i}",)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    ok(len(winners) == 1, f"exactly one of 8 racing drainers claims the request (got {len(winners)})")
    ok(row(ev6)["attempt"]["state"] == "claimed" and row(ev6)["attempt"]["count"] == 1,
       "the contended request ends up claimed exactly once (attempt count 1)")

except Exception as exc:  # pragma: no cover - surfaces setup/import failures as a hard fail
    import traceback
    traceback.print_exc()
    ok(False, f"unexpected exception: {exc}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
