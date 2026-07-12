#!/usr/bin/env python3
"""NARRATE-14: event-driven narration production cutover — drills + SLO proof.

Exercises the cutover integration (``narration_cutover``) end-to-end against a real SQLite store
with the provider injected, proving the exit criteria that can be shown without a live prod soak:

- **crash / restart** — a worker that dies mid-flight (expired lease) is recovered by the sweep with
  no lost and no duplicate published narrative;
- **crash mid-generate** — a generate that raises drops the row into bounded retry and is delivered
  on the next attempt, exactly once;
- **backlog** — a large burst drains fully across bounded sweeps and the next sweep is a clean no-op;
- **provider outage** — a down gateway yields a VISIBLE fallback narrative (never a crash, never a
  silent drop), and recovers to a real narrative when the provider returns;
- **compare-and-swap publish** — an older/reordered delivery never clobbers a newer published
  revision, and an error receipt never publishes;
- **rollback** — with ``PM_NARRATION_EVENT_PRIMARY`` off the recovery sweep and wake accelerator are
  inert (the instant rollback lever), and the legacy path stays primary;
- **wake accelerator** — a post-commit emit drives a bounded background drain that publishes;
- **SLO report** — request-to-delivery freshness p95, fallback rate, cost reconciliation, and
  dead-letter health compute correctly and gate on the <=60s freshness target.
"""
import os
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="narrate-rollout-test-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_NARRATION_EVENT_PRIMARY"] = "1"  # drills run as the cutover-enabled state
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import narration_cutover  # noqa: E402
import narration_generate  # noqa: E402
import narration_outbox  # noqa: E402
import narration_worker  # noqa: E402
import store  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import narration_slo  # noqa: E402

PROJECT = store.DEFAULT_PROJECT       # controlled-delivery drills
SLO_PROJECT = "helm"                  # isolated SLO measurement
SWEEP_PROJECT = "switchboard"         # sweep / rollback / wake drills

# Pin emit timestamps so requested_at is a known BASE and freshness math is deterministic.
BASE = 1_000_000.0
narration_outbox._now = lambda now=None: BASE if now is None else now

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def fake_llm(prompt, *, model="fake-model", prompt_version="v1", max_tokens=200):
    return {"text": "The board advanced.", "model": model,
            "tokens_in": 12, "tokens_out": 24, "cost_usd": 0.001}


def raising_llm(prompt, **kwargs):
    raise RuntimeError("gateway down")


def mk_task(project, title):
    t = store.create_task({"workstream_id": "NW", "title": title}, actor="user", project=project)
    return t["task_id"] if isinstance(t, dict) else t


def drain_controlled(project, *, deliver_at, claim_at, llm=fake_llm, max_items=100,
                     worker_id="test-w", generate=None):
    gen = generate or narration_cutover.make_production_generate(project, llm_fn=llm, now=deliver_at)
    return narration_worker.drain(project, worker_id=worker_id, generate=gen,
                                  now_fn=lambda: claim_at, max_items=max_items)


def outbox_states(project):
    with narration_outbox._conn(project) as c:
        rows = c.execute("SELECT attempt_state, COUNT(*) n FROM narration_outbox "
                         "GROUP BY attempt_state").fetchall()
    return {r["attempt_state"]: r["n"] for r in rows}


def delivered_count(project, entity_id):
    with narration_outbox._conn(project) as c:
        return c.execute("SELECT COUNT(*) FROM narration_receipts WHERE entity_id=? "
                         "AND outcome='delivered'", (entity_id,)).fetchone()[0]


def run():
    for p in (PROJECT, SLO_PROJECT, SWEEP_PROJECT):
        store.init_db(p)

    # ---- Drill 1: crash / restart — no lost, no duplicate ----------------------------------
    ids = [mk_task(PROJECT, f"crash-{i}") for i in range(4)]
    # Two workers claim, then "die" (never settle) — their leases will expire.
    for eid in ids[:2]:
        with narration_outbox._conn(PROJECT) as c:
            row = c.execute("SELECT event_id FROM narration_outbox WHERE entity_id=?",
                            (eid,)).fetchone()
        narration_worker.claim_next_narration(PROJECT, worker_id="dead", now=BASE)
    # Restart: recovery sweep after the leases expire reclaims everything and delivers it.
    drain_controlled(PROJECT, deliver_at=BASE + 12, claim_at=BASE + 200)
    states = outbox_states(PROJECT)
    ok(states.get("delivered", 0) == 4 and states.get("claimed", 0) == 0,
       "crash/restart: every request is recovered and delivered, none left claimed")
    ok(all(delivered_count(PROJECT, eid) == 1 for eid in ids),
       "crash/restart: each entity delivered exactly once (no duplicate published narrative)")
    ok(all(store.get_task_narration(eid, project=PROJECT) is not None for eid in ids),
       "crash/restart: the visible narration is published for every recovered entity")

    # ---- Drill 2: crash mid-generate — bounded retry, delivered exactly once ----------------
    flaky_id = mk_task(PROJECT, "flaky")
    attempts = {"n": 0}
    real_gen = narration_cutover.make_production_generate(PROJECT, llm_fn=fake_llm, now=BASE + 12)

    def flaky_generate(event):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("crash during generate")
        return real_gen(event)

    r1 = drain_controlled(PROJECT, deliver_at=BASE + 12, claim_at=BASE, generate=flaky_generate,
                          max_items=1)
    r2 = drain_controlled(PROJECT, deliver_at=BASE + 12, claim_at=BASE + 5000,
                          generate=flaky_generate, max_items=1)
    ok(r1 and r1[0][1] == "retry_wait", "crash mid-generate: first attempt drops into bounded retry")
    ok(r2 and r2[0][1] == "delivered" and delivered_count(PROJECT, flaky_id) == 1,
       "crash mid-generate: recovered and delivered exactly once")

    # ---- Drill 3: backlog — drains fully across bounded sweeps, then clean no-op ------------
    for i in range(25):
        mk_task(SWEEP_PROJECT, f"backlog-{i}")
    swept = 0
    for _ in range(3):
        res = narration_cutover.run_recovery_sweep(projects=[SWEEP_PROJECT], max_items=10)
        swept += (res["projects"][SWEEP_PROJECT] or {}).get("delivered", 0)
    tail = narration_cutover.run_recovery_sweep(projects=[SWEEP_PROJECT], max_items=10)
    ok(swept == 25, f"backlog: bounded sweeps drain the full 25-item backlog (got {swept})")
    ok((tail["projects"][SWEEP_PROJECT] or {}).get("total", 0) == 0,
       "backlog: the sweep after drain is a clean no-op (idempotent)")

    # ---- Drill 4: provider outage — visible fallback, then recovery -------------------------
    out_id = mk_task(PROJECT, "outage")
    drain_controlled(PROJECT, deliver_at=BASE + 12, claim_at=BASE, llm=raising_llm)
    narr = store.get_task_narration(out_id, project=PROJECT)
    ok(narr is not None and outbox_states(PROJECT).get("dead_letter", 0) == 0,
       "provider outage: a visible fallback narrative is published, nothing dead-lettered")
    store.update_task(out_id, {"status": "In Review"}, actor="user", project=PROJECT)
    drain_controlled(PROJECT, deliver_at=BASE + 15, claim_at=BASE + 20, llm=fake_llm)
    ok(delivered_count(PROJECT, out_id) >= 1,
       "provider outage: recovers to a real delivered narrative once the provider returns")

    # ---- Drill 5: compare-and-swap publish -------------------------------------------------
    cas_id = mk_task(PROJECT, "cas")
    drain_controlled(PROJECT, deliver_at=BASE + 12, claim_at=BASE)
    before = store.get_task_narration(cas_id, project=PROJECT)["narration"]
    stale = {"entity_type": "task", "entity_id": cas_id, "source_revision": 1,
             "outcome": "delivered", "narration": "STALE-OLD", "source_hash": "h", "mode": "llm"}
    published_stale = narration_cutover._publish(PROJECT, stale, prev_revision=9)
    ok(published_stale is False
       and store.get_task_narration(cas_id, project=PROJECT)["narration"] == before,
       "CAS: an older revision never clobbers a newer published narrative")
    newer = {"entity_type": "task", "entity_id": cas_id, "source_revision": 12,
             "outcome": "delivered", "narration": "NEWEST", "source_hash": "h", "mode": "llm"}
    published_new = narration_cutover._publish(PROJECT, newer, prev_revision=9)
    ok(published_new is True
       and store.get_task_narration(cas_id, project=PROJECT)["narration"] == "NEWEST",
       "CAS: a newer revision publishes")
    err = {"entity_type": "task", "entity_id": cas_id, "source_revision": 20,
           "outcome": "error", "narration": None, "source_hash": "h", "mode": "llm"}
    ok(narration_cutover._publish(PROJECT, err, prev_revision=0) is False,
       "CAS: an error receipt carries no text and never publishes")

    # ---- Drill 6: rollback lever — flag off makes the event path inert ----------------------
    os.environ["PM_NARRATION_EVENT_PRIMARY"] = "0"
    ok(narration_cutover.event_primary_enabled() is False, "rollback: cutover gate reads off")
    off = narration_cutover.run_recovery_sweep(projects=[SWEEP_PROJECT])
    ok(off.get("enabled") is False and off.get("skipped") == "event_primary_disabled",
       "rollback: the recovery sweep is a no-op while disabled (legacy stays primary)")
    narration_cutover._wake_sink(SWEEP_PROJECT)
    ok(SWEEP_PROJECT not in narration_cutover._WAKE_INFLIGHT,
       "rollback: the wake accelerator is inert while disabled")
    os.environ["PM_NARRATION_EVENT_PRIMARY"] = "1"
    ok(narration_cutover.event_primary_enabled() is True, "rollback: re-enabling the cutover works")

    # ---- Drill 7: wake accelerator — a post-commit emit drives a bounded drain --------------
    narration_cutover.register_production_wake_sink()
    wake_id = mk_task(SWEEP_PROJECT, "wake")  # create_task -> request_wake -> background drain
    deadline = time.time() + 10.0
    while SWEEP_PROJECT in narration_cutover._WAKE_INFLIGHT and time.time() < deadline:
        time.sleep(0.02)
    # Give the daemon thread a beat to finish its settle after leaving the in-flight set.
    time.sleep(0.1)
    ok(store.get_task_narration(wake_id, project=SWEEP_PROJECT) is not None,
       "wake accelerator: an emit drives a background drain that publishes the narration")
    narration_outbox.register_wake_sink(None)  # unregister so later work is deterministic

    # ---- Drill 8: SLO / reconciliation report ----------------------------------------------
    slo_ids = [mk_task(SLO_PROJECT, f"slo-{i}") for i in range(5)]
    drain_controlled(SLO_PROJECT, deliver_at=BASE + 5, claim_at=BASE + 5)  # freshness = 5s
    report = narration_slo.slo_report(SLO_PROJECT, window_seconds=1000.0, now=BASE + 100)
    ok(report["freshness"]["delivered_samples"] == 5
       and report["freshness"]["p95_seconds"] <= 60.0,
       f"SLO: request-to-delivery p95 freshness is within the 60s target "
       f"({report['freshness']['p95_seconds']}s)")
    ok(report["slo"]["all_ok"] is True and report["cost"]["total_cost_usd"] > 0,
       "SLO: a healthy window passes all SLO gates and reconciles non-zero cost")
    # Inject a dead letter and confirm the report gates on it (breach is visible, not hidden).
    dead_id = mk_task(SLO_PROJECT, "poison")
    with narration_outbox._conn(SLO_PROJECT) as c:
        c.execute("UPDATE narration_outbox SET attempt_state='dead_letter' WHERE entity_id=?",
                  (dead_id,))
    breached = narration_slo.slo_report(SLO_PROJECT, window_seconds=1000.0, now=BASE + 100)
    ok(breached["slo"]["no_dead_letters"] is False and breached["slo"]["all_ok"] is False,
       "SLO: a dead letter breaches the SLO gate (surfaced, not hidden)")

    print(f"\n{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
