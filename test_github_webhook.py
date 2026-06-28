#!/usr/bin/env python3
"""Smoke test for default-branch webhook task provenance handling."""
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="switchboard-github-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")

import store  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_db(P)
    ready = store.create_task({"workstream_id": "TEST", "title": "ready direct"}, actor="seed", project=P)
    store.update_task(ready["task_id"], {"status": "In Review"}, actor="seed", project=P)
    not_ready = store.create_task({"workstream_id": "TEST", "title": "not ready direct"},
                                  actor="seed", project=P)
    result = store.backfill_default_branch_commits([
        {"id": "abc123", "message": f"fix({ready['task_id']}): direct default proof"},
        {"id": "def456", "message": f"fix({not_ready['task_id']}): should skip"},
    ], branch="master", actor="github-webhook", project=P)
    ok(ready["task_id"] in result["direct_backfilled_tasks"],
       "push webhook backfills eligible In Review task")
    ok(any(s["task_id"] == not_ready["task_id"] and s["reason"] == "status_not_in_review"
           for s in result["direct_backfill_skipped"]),
       "push webhook reports skipped non-review task")
    ready_after = store.get_task(ready["task_id"], project=P)
    not_ready_after = store.get_task(not_ready["task_id"], project=P)
    ok(ready_after["status"] == "Done" and ready_after["git_state"]["merged_sha"] == "abc123",
       "backfilled task is Done with commit provenance")
    ok(not_ready_after["status"] == "Not Started",
       "non-review task is not promoted by default-branch push")

    print("\n%d passed, %d failed" % (passed, failed))
    raise SystemExit(1 if failed else 0)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)
