#!/usr/bin/env python3
"""Regression tests for HARDEN-23 lifecycle cleanup candidates and apply path.

Run:
    python3 test_cleanup_lifecycle.py
"""
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="cleanup-lifecycle-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "required"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_project_registry()
    store.init_db(P)
    now = time.time()
    old = now - 7200

    stale_task = store.create_task({"workstream_id": "HARDEN", "title": "stale claim target"},
                                   actor="test", project=P)
    claim = store.claim_task(stale_task["task_id"], "codex/stale", ttl_seconds=60,
                             actor="test", project=P)
    store.register_agent("codex/stale", "codex", lane="HARDEN",
                         task_id=stale_task["task_id"], ttl_s=60,
                         actor="test", project=P)
    runner = store.upsert_runner_session({
        "runner_session_id": "run-stale",
        "host_id": "host/test",
        "agent_id": "codex/stale",
        "runtime": "codex",
        "task_id": stale_task["task_id"],
        "claim_id": claim["claim_id"],
        "status": "running",
        "heartbeat_at": old,
        "heartbeat_ttl_s": 60,
        "control": {"managed_process": True},
    }, actor="test", project=P)
    wake = store.request_wake(
        selector={"runtime": "codex", "agent_id": "codex/missing"},
        reason="test stale wake",
        source="test",
        policy={"deadline_seconds": 60},
        task_id=stale_task["task_id"],
        actor="test",
        project=P,
    )
    msg = store.send_agent_message("codex/source", "codex/missing", "ack please",
                                   task_id=stale_task["task_id"],
                                   requires_ack=True, ack_timeout_seconds=60,
                                   project=P)
    proof = store.create_task({"workstream_id": "PROOF", "title": "old sentinel proof",
                               "status": "Done"},
                              actor="test", project=P)
    file_lease = store.claim_files("codex/file", ["store.py"], task_id=stale_task["task_id"],
                                   ttl_minutes=1, project=P)

    with store._conn(P) as c:
        c.execute("UPDATE task_claims SET expires_at=? WHERE id=?", (old, claim["claim_id"]))
        c.execute("UPDATE resource_leases SET claimed_at=?, ttl_seconds=60 WHERE agent_id=?",
                  (old, "codex/stale"))
        c.execute("UPDATE file_leases SET claimed_at=?, ttl_minutes=1 WHERE id=?",
                  (old, file_lease["lease_id"]))
        c.execute("UPDATE agent_presence SET heartbeat_at=? WHERE agent_id=?",
                  (old, "codex/stale"))
        c.execute("UPDATE wake_intents SET deadline=? WHERE wake_id=?",
                  (old, wake["wake_id"]))
        c.execute("UPDATE coordination_monitors SET status='fired', fired_at=?, updated_at=? "
                  "WHERE id=?", (old, old, msg["monitor_id"]))
        c.execute("UPDATE tasks SET updated_at=? WHERE task_id=?", (old, proof["task_id"]))

    plan = store.cleanup_candidates(project=P, now=now, proof_task_age_days=0)
    ids = {c["id"] for c in plan["candidates"]}
    expected = {
        "agent_presence:codex/stale",
        "runner_session:" + runner["runner_session_id"],
        "task_claim:" + claim["claim_id"],
        "wake_intent:" + wake["wake_id"],
        "monitor:" + msg["monitor_id"],
        "proof_task:" + proof["task_id"],
        "file_lease:" + str(file_lease["lease_id"]),
    }
    ok(expected.issubset(ids), "cleanup_candidates returns each stale lifecycle object")
    ok(plan["summary"]["by_kind"]["task_claim"] == 1,
       "cleanup candidate summary counts by kind")

    dry = store.apply_cleanup(project=P, dry_run=True, now=now, proof_task_age_days=0)
    ok(dry["dry_run"] is True and dry["summary"]["total"] >= len(expected),
       "apply_cleanup dry-run reports candidates without applying")
    ok(store.get_task(proof["task_id"], project=P) is not None,
       "dry-run leaves proof task active")

    applied = store.apply_cleanup(project=P, dry_run=False, now=now,
                                  proof_task_age_days=0,
                                  reason="test lifecycle cleanup")
    ok(applied["applied_count"] >= len(expected),
       "apply_cleanup applies stale lifecycle candidates")
    remaining = store.cleanup_candidates(project=P, now=now + 1,
                                         proof_task_age_days=0)
    remaining_ids = {c["id"] for c in remaining["candidates"]}
    ok(not (expected & remaining_ids), "applied cleanup candidates disappear from the read model")

    with store._conn(P) as c:
        claim_row = c.execute("SELECT status, abandon_reason FROM task_claims WHERE id=?",
                              (claim["claim_id"],)).fetchone()
        runner_row = c.execute("SELECT status FROM runner_sessions WHERE runner_session_id=?",
                               (runner["runner_session_id"],)).fetchone()
        wake_row = c.execute("SELECT status FROM wake_intents WHERE wake_id=?",
                             (wake["wake_id"],)).fetchone()
        monitor_row = c.execute("SELECT status FROM coordination_monitors WHERE id=?",
                                (msg["monitor_id"],)).fetchone()
        presence_row = c.execute("SELECT 1 FROM agent_presence WHERE agent_id=?",
                                 ("codex/stale",)).fetchone()
        archive_row = c.execute("SELECT * FROM archived_tasks WHERE task_id=?",
                                (proof["task_id"],)).fetchone()
        cleanup_events = [r["kind"] for r in c.execute(
            "SELECT kind FROM activity WHERE kind LIKE 'cleanup.%' ORDER BY id"
        ).fetchall()]
    ok(claim_row["status"] == "abandoned" and "cleanup" in claim_row["abandon_reason"],
       "expired task claim is abandoned with cleanup reason")
    ok(runner_row["status"] == "expired", "expired runner session is retained as expired")
    ok(wake_row["status"] == "cancelled", "old wake intent is cancelled")
    ok(monitor_row["status"] == "resolved", "fired monitor is resolved")
    ok(presence_row is None, "stale agent presence is removed from live registry")
    ok(store.get_task(proof["task_id"], project=P) is None and archive_row is not None,
       "old proof task is archived with snapshot provenance")
    for kind in ("cleanup.task_claim_abandoned", "cleanup.runner_session_expired",
                 "cleanup.wake_cancelled", "cleanup.monitor_resolved",
                 "cleanup.agent_presence_resolved", "cleanup.task_archived"):
        ok(kind in cleanup_events, f"{kind} audit activity is recorded")

    try:
        from fastapi.testclient import TestClient  # noqa: E402
        from app import app  # noqa: E402
        token = "cleanup-admin-token"
        store.create_principal("agent", "cleanup admin", token, ["read", "write:system"],
                               project=P)
        client = TestClient(app)
        denied = client.get(f"/api/cleanup/candidates?project={P}")
        denied_apply = client.post("/api/cleanup/apply",
                                   json={"project": P, "dry_run": True})
        allowed = client.get(f"/api/cleanup/candidates?project={P}",
                             headers={"Authorization": f"Bearer {token}"})
        dry_rest = client.post("/api/cleanup/apply",
                               json={"project": P, "dry_run": True},
                               headers={"Authorization": f"Bearer {token}"})
        ok(denied.status_code == 401, "REST cleanup candidates require auth")
        ok(denied_apply.status_code == 401, "REST cleanup apply still requires auth")
        ok(allowed.status_code == 200 and "candidates" in allowed.json(),
           "REST cleanup candidates returns a plan")
        ok(dry_rest.status_code == 200 and dry_rest.json()["dry_run"] is True,
           "REST cleanup apply accepts body-scoped project and defaults to dry-run")
    except ModuleNotFoundError as exc:
        print(f"  SKIP  FastAPI cleanup smoke requires optional dependency: {exc.name}")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
