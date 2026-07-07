#!/usr/bin/env python3
"""Tests for coordination receipt projection (RECON-9)."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="coordination-receipts-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")

import coordination_receipts  # noqa: E402
import store  # noqa: E402

P = "receipt-test"
store.create_project("Receipt Test", project_id=P, actor="test")
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


store.init_db(P)
task = store.create_task({"workstream_id": "RECON", "title": "Receipt lifecycle"},
                         actor="test", project=P)
task_id = task["task_id"]

claim = store.claim_task(task_id, "agent/receipt", actor="test", project=P)
ok(claim.get("claimed"), "claim seeds receipt lifecycle")
claim_id = claim["claim_id"]

mid = coordination_receipts.project_task_receipts(P, task_id, claim_id=claim_id)
ok(len(mid) == 1 and mid[0]["status"] == "running",
   "receipt projects running state after claim")
ok(mid[0]["policy_refs"], "receipt captures dispatch policy refs")
ok(mid[0]["receipt_id"] == f"cr:{P}:{task_id}:{claim_id}",
   "receipt id is stable")

complete = store.complete_claim(
    claim_id,
    evidence='{"branch":"codex/RECON-9","head_sha":"deadbeef","pr_url":"https://example/pr/9"}',
    actor="test",
    project=P,
)
ok(complete.get("status") == "In Review", "complete_claim moves task to In Review")

review = coordination_receipts.get_coordination_receipt(
    P, f"cr:{P}:{task_id}:{claim_id}")
ok(review.get("status") == "in_review", "receipt status becomes in_review")
ok(any(ref.get("pr_url") for ref in review.get("evidence_refs") or []),
   "receipt captures PR evidence refs")
ok(review.get("source_events"), "receipt lists source activity events")

store.mark_task_merged(
    task_id, "cafebabe", pr_number=9, pr_url="https://example/pr/9",
    branch="codex/RECON-9", head_sha="deadbeef", actor="test", project=P)

done = coordination_receipts.get_coordination_receipt(
    P, f"cr:{P}:{task_id}:{claim_id}")
ok(done.get("status") == "done", "receipt status becomes done after merge")
ok(done.get("terminal_at"), "done receipt has terminal_at")
ok(any(ref.get("kind") == "merge_provenance" for ref in done.get("outcome_refs") or []),
   "receipt records merge outcome ref")

listed = coordination_receipts.list_coordination_receipts(P, task_id=task_id)
ok(listed["count"] >= 1, "list_coordination_receipts returns task receipts")

task2 = store.create_task({"workstream_id": "RECON", "title": "Supersede path"},
                          actor="test", project=P)
tid2 = task2["task_id"]
first = store.claim_task(tid2, "agent/one", actor="test", project=P)
store.complete_claim(
    first["claim_id"],
    evidence='{"branch":"codex/a","head_sha":"111"}',
    actor="test",
    project=P,
)
store.update_task(tid2, {"status": "Not Started"}, actor="test", project=P)
second = store.claim_task(tid2, "agent/two", actor="test", project=P)
ok(second.get("claimed"), "second claim starts after first cycle returned to ready")
receipts = coordination_receipts.project_task_receipts(P, tid2)
ok(len(receipts) == 2, "two claim cycles produce two receipts")
ok(receipts[0]["status"] == "superseded",
   "earlier in_review receipt is superseded by later claim")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
