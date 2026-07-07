#!/usr/bin/env python3
"""Tests for background job catalog and checkpoint runner (RECON-10)."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="background-jobs-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")

import background_jobs  # noqa: E402
import store  # noqa: E402

P = "bgjob-test"
store.create_project("Background Job Test", project_id=P, actor="test")
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


store.init_db(P)
alpha = store.create_task({"workstream_id": "RECON", "title": "BG alpha",
                           "sort_order": 10}, actor="test", project=P)
store.claim_task(alpha["task_id"], "agent/bg", actor="test", project=P)

catalog = background_jobs.list_background_jobs()
ok(catalog["schema"] == background_jobs.CATALOG_SCHEMA, "catalog schema")
ok(any(j["job_name"] == "replay_verify_batch" for j in catalog["jobs"]),
   "catalog includes replay_verify_batch")
ok("claim_next" in catalog["forbidden_hot_path_operations"],
   "claim_next is forbidden on hot path")

eval_report = background_jobs.evaluate_dbos_runtime()
ok(eval_report["schema"] == background_jobs.EVAL_SCHEMA, "dbos evaluation schema")
ok(eval_report["hot_path_independent"], "hot path stays independent of DBOS")
ok(eval_report["recommendation"] in ("local_checkpoint", "dbos"),
   "runtime recommendation present")

try:
    background_jobs.assert_job_boundary("claim_next")
    ok(False, "forbidden job should raise JobBoundaryError")
except background_jobs.JobBoundaryError:
    ok(True, "forbidden job raises JobBoundaryError")

run = background_jobs.run_background_job(
    P, "replay_verify_batch", params={"projects": P}, resume=False,
)
ok(run["status"] == "completed", "replay_verify_batch completes")
ok(run["summary"]["ok"], "replay_verify_batch summary ok")
run_id = run["run_id"]

loaded = background_jobs.load_run(P, run_id)
ok(loaded["run_id"] == run_id, "load_run returns persisted manifest")

try:
    background_jobs.run_background_job(
        P, "replay_verify_batch",
        params={"projects": P},
        resume=True,
        crash_after_step=0,
    )
    ok(False, "crash_after_step should raise")
except RuntimeError as exc:
    ok("simulated crash" in str(exc), "simulated crash after first step")

resumed = background_jobs.run_background_job(
    P, "replay_verify_batch", params={"projects": P}, resume=True,
)
ok(resumed["status"] == "completed", "resume completes after simulated crash")
ok(resumed["steps"][0]["status"] == "completed", "resumed run skips completed step")

audit = background_jobs.run_background_job(
    P, "audit_export_batch", params={"projects": P}, resume=False,
)
ok(audit["status"] == "completed", "audit_export_batch completes")
ok(audit["steps"][0]["result"]["task_count"] >= 1, "audit export sees tasks")

listed = background_jobs.list_job_runs(P, limit=5)
ok(listed["count"] >= 1, "list_job_runs returns recent runs")

print(f"\n{background_jobs.__name__}: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
