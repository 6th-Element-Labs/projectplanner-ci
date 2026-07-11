#!/usr/bin/env python3
"""NARRATE-13: operator surfaces — queue health, provenance, and authorized controls.

Proves the M4 exit criteria (no network):
- narration_health distinguishes queued / running / retrying / dead-lettered / delivered /
  fallback, reports freshness age, success/failure/fallback rates + model-token-cost totals, and
  precomputes alert flags for queue age, failure rate, and dead letters;
- narrate_now re-queues the CURRENT revision (deduped — no new revision, no second outbox row),
  is audited, and does not itself call an LLM / bypass a budget;
- reactivate_request retry/dead-letter transitions are authorized-actor audited and operate on the
  existing row.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="narrate-ops-test-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import narration_ops  # noqa: E402
import narration_outbox  # noqa: E402
import store  # noqa: E402

PROJECT = store.DEFAULT_PROJECT
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def outbox_rows(entity_id):
    return [e for e in narration_outbox.list_narration_outbox(PROJECT, limit=1000)
            if e["entity_id"] == entity_id]


def activity_kinds():
    with narration_ops._conn(PROJECT) as c:
        return [r[0] for r in c.execute("SELECT kind FROM activity ORDER BY id DESC LIMIT 20").fetchall()]


def insert_receipt(mode, outcome, cost, tokens, latency, when):
    with narration_ops._conn(PROJECT) as c:
        c.execute(
            "INSERT INTO narration_receipts (event_id, project, entity_type, entity_id, "
            "source_revision, source_hash, mode, outcome, model, prompt_version, latency_ms, "
            "tokens_in, tokens_out, cost_usd, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("e", PROJECT, "task", "T", 1, "sha256:x", mode, outcome, "taikun-summarize",
             "narrate.v1", latency, tokens, 0, cost, when),
        )


try:
    store.init_db(PROJECT)
    now = 2_000_000.0
    narration_outbox._now = lambda n=None: now if n is None else n

    # Build a queue: 2 tasks (pending), and receipts across outcomes.
    ta = store.create_task({"workstream_id": "OPS", "title": "Task A", "status": "In Progress"},
                           actor="u", project=PROJECT)
    taid = ta["task_id"]
    tb = store.create_task({"workstream_id": "OPS", "title": "Task B"}, actor="u", project=PROJECT)
    tbid = tb["task_id"]
    insert_receipt("llm", "delivered", 0.02, 180, 900, now - 100)
    insert_receipt("llm", "error", 0.01, 120, 1200, now - 90)
    insert_receipt("deterministic", "delivered", 0.0, 0, 1, now - 80)
    insert_receipt("fallback", "fallback", 0.0, 0, None, now - 70)
    # two more llm-delivered rows with NO latency (fallback-that-still-recorded style): these must
    # NOT skew avg_latency_ms — it should average only the 3 non-null latencies (900, 1200, 1).
    insert_receipt("llm", "delivered", 0.0, 0, None, now - 60)
    insert_receipt("llm", "delivered", 0.0, 0, None, now - 55)

    # 1. health distinguishes states + reports rates/cost + freshness.
    h = narration_ops.narration_health(PROJECT, now=now)
    ok(h["queue"]["pending"] >= 2 and h["queue"]["actionable"] >= 2,
       "health reports the pending/actionable queue depth")
    ok(h["receipts"]["delivered"] == 4 and h["receipts"]["failed"] == 1
       and h["receipts"]["fallback"] == 1 and h["receipts"]["deterministic"] == 1,
       "health separates delivered / failed / fallback / deterministic receipts")
    ok(abs(h["cost"]["total_cost_usd"] - 0.03) < 1e-9 and h["cost"]["total_tokens"] == 300,
       "health totals model token + cost spend over the window")
    ok(h["receipts"]["failure_rate"] == round(2 / 6, 4),
       "health computes a failure+fallback rate for alerting")
    ok(h["receipts"]["avg_latency_ms"] == round((900 + 1200 + 1) / 3, 2),
       "avg_latency_ms averages only non-null latencies (null-latency rows don't skew it)")
    ok(h["freshness"]["oldest_pending_age_seconds"] >= 0,
       "health reports oldest-pending freshness age")

    # 2. alerting flags fire on old queue + failures.
    old = store.create_task({"workstream_id": "OPS", "title": "Stale"}, actor="u", project=PROJECT)
    with narration_ops._conn(PROJECT) as c:  # force an old requested_at so the age alert trips
        c.execute("UPDATE narration_outbox SET requested_at=? WHERE entity_id=?",
                  (now - 3600, old["task_id"]))
    h2 = narration_ops.narration_health(PROJECT, now=now)
    ok(h2["alerts"]["queue_age_over_threshold"] is True and h2["alerting"] is True,
       "an old actionable request raises the queue-age alert")

    # 3. narrate_now re-queues the CURRENT revision, deduped (no new outbox row), audited.
    before = outbox_rows(taid)
    res = narration_ops.narrate_now(PROJECT, "task", taid, actor="operator@x.com", reason="manual")
    after = outbox_rows(taid)
    ok(res.get("ok") and len(after) == len(before),
       "narrate_now re-queues the current revision without creating a duplicate outbox row")
    ok(any(e["source_revision"] == res["source_revision"]
           and e["attempt"]["state"] == "pending" for e in after),
       "narrate_now leaves the current-revision request in a pending (re-runnable) state")
    ok("narration.narrate_now" in activity_kinds(),
       "narrate_now records an audit activity under the operator actor")

    # 4. reactivate retry: dead_letter -> pending, audited.
    dead_ev = after[0]["event_id"]
    with narration_ops._conn(PROJECT) as c:
        c.execute("UPDATE narration_outbox SET attempt_state='dead_letter', last_error='boom' "
                  "WHERE event_id=?", (dead_ev,))
    rr = narration_ops.reactivate_request(PROJECT, dead_ev, actor="operator@x.com", action="retry")
    st = [e["attempt"]["state"] for e in outbox_rows(taid) if e["event_id"] == dead_ev][0]
    ok(rr.get("ok") and rr["previous_state"] == "dead_letter" and st == "pending"
       and "narration.retry" in activity_kinds(),
       "reactivate_request(retry) moves a dead letter back to pending and audits it")

    # 5. reactivate dead_letter: pending -> dead_letter, audited.
    dl = narration_ops.reactivate_request(PROJECT, dead_ev, actor="operator@x.com",
                                          action="dead_letter", reason="poison")
    st2 = [e["attempt"]["state"] for e in outbox_rows(taid) if e["event_id"] == dead_ev][0]
    ok(dl.get("ok") and st2 == "dead_letter" and "narration.dead_letter" in activity_kinds(),
       "reactivate_request(dead_letter) parks a request and audits it")
    ok(any(d["event_id"] == dead_ev for d in narration_ops.list_dead_letters(PROJECT)),
       "list_dead_letters surfaces the parked request for the operator")

    # 6. narrate_now on a missing entity is a clean audited no-op error (not a crash).
    miss = narration_ops.narrate_now(PROJECT, "task", "NOPE-1", actor="operator@x.com")
    ok(miss.get("error") and "narration.narrate_now_missing" in activity_kinds(),
       "narrate_now on an entity with no request returns an audited error, not a crash")

except Exception as exc:  # pragma: no cover
    import traceback
    traceback.print_exc()
    ok(False, f"unexpected exception: {exc}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
