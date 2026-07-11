#!/usr/bin/env python3
"""Persistence + orchestrator tests for the closure surface (DELIVERABLES-16).

Covers store.record_deliverable_closure / get_deliverable_closure_report (metadata
persistence, the deliverable.closure_verified audit stamp, bounded history) and
deliverable_closure.verify_and_record_closure (the engine->persist path the MCP
tool and REST route both call, plus the agent-submitted-report path and errors).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="closure-persist-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import store  # noqa: E402
import deliverable_closure as dc  # noqa: E402

passed = failed = 0
PROJ = "qa-cl16"
DELIV = "qa-cl16-deliv"


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def closure_stamps():
    with store._conn(PROJ) as c:
        return [r[0] for r in c.execute(
            "SELECT kind FROM activity WHERE kind='deliverable.closure_verified'").fetchall()]


store.init_project_registry()
store.init_db("switchboard")
store.create_project("Closure16 QA", project_id=PROJ, actor="test")
for title in ("A", "B", "C"):
    store.create_task({"workstream_id": "CL", "title": title}, actor="test", project=PROJ)
for tid, sha, pr in (("CL-1", "sha-a", 1), ("CL-2", "sha-b", 2), ("CL-3", "sha-c", 3)):
    store.mark_task_merged(tid, sha, pr_number=pr, project=PROJ)  # all terminal
store.create_deliverable({
    "id": DELIV, "title": "QA16", "status": "in_progress",
    "acceptance_criteria": ["ships"],
    "proof_requirements": {
        "schema": "switchboard.deliverable_proof_requirements.v1",
        "gates": [
            {"id": "scope", "required": True},
            {"id": "store:a", "kind": "store_check", "check": "task_terminal",
             "params": {"task_id": "CL-1"}, "required": True},
            {"id": "harness:ok", "kind": "script",
             "command": ["python3", "-c", "import sys;sys.exit(0)"], "required": True},
        ],
    },
}, actor="test", project=PROJ)
for tid in ("CL-1", "CL-2", "CL-3"):
    store.link_task_to_deliverable(DELIV, PROJ, tid, actor="test", project=PROJ)

# --- 1. orchestrator: engine -> grade pass -> persist + stamp ---------------
res = dc.verify_and_record_closure(DELIV, PROJ, actor="verifier/test", run_scripts=True,
                                   generated_by="verifier/test")
ok(res.get("ok") is True and res.get("grade") == "pass",
   "verify_and_record_closure runs the engine, grades pass, and persists")
rid = res.get("report_id")
ok(bool(rid) and rid.startswith("closure-"), "a report_id is assigned on persist")

deliv = store.get_deliverable(DELIV, project=PROJ)
meta = deliv.get("metadata") or {}
ok(meta.get("last_closure_grade") == "pass"
   and (meta.get("last_closure_report") or {}).get("report_id") == rid,
   "last_closure_report + grade persisted in deliverable metadata")
ok(len(closure_stamps()) == 1, "deliverable.closure_verified activity stamped once")

# --- 2. get_deliverable_closure_report: latest / by id / not found ----------
latest = store.get_deliverable_closure_report(DELIV, project=PROJ)
ok((latest.get("report") or {}).get("report_id") == rid and latest.get("grade") == "pass",
   "get_deliverable_closure_report returns the latest report")
byid = store.get_deliverable_closure_report(DELIV, project=PROJ, report_id=rid)
ok((byid.get("report") or {}).get("report_id") == rid, "fetch a closure report by report_id")
nf = store.get_deliverable_closure_report(DELIV, project=PROJ, report_id="closure-nope")
ok("error" in nf and "history" in nf, "unknown report_id returns an error (with history)")

# --- 3. hold grade still persists (required command gate not_run) ------------
res2 = dc.verify_and_record_closure(DELIV, PROJ, actor="verifier/test")  # run_scripts=False
ok(res2.get("grade") == "hold", "a required not_run gate grades hold and still persists")
ok(res2.get("report_id") != rid, "a different verdict yields a distinct report_id")

# --- 4. history retained + bounded (direct record with distinct ids) --------
for i in range(12):
    store.record_deliverable_closure(DELIV, {
        "schema": dc.CLOSURE_REPORT_SCHEMA, "report_id": f"hist-{i}", "grade": "pass",
        "recommendation": "safe_to_mark_done", "generated_at": 2000.0 + i,
        "generated_by": f"run-{i}", "evidence_hash": f"sha256:{i:064d}", "gates": {},
    }, actor="verifier/test", project=PROJ)
hist = store.get_deliverable_closure_report(DELIV, project=PROJ)
ok(hist.get("count") == store.CLOSURE_REPORT_HISTORY_LIMIT,
   f"closure_reports bounded to {store.CLOSURE_REPORT_HISTORY_LIMIT}")
ok((hist.get("report") or {}).get("report_id") == "hist-11", "newest report is retained as latest")
ok(all(h["report_id"] != "hist-0" for h in hist.get("history") or []),
   "oldest report evicted past the history limit")

# --- 5. agent-submitted full report persisted as-is -------------------------
submitted = {
    "schema": dc.CLOSURE_REPORT_SCHEMA, "deliverable_id": DELIV, "project_id": PROJ,
    "grade": "waive", "recommendation": "safe_to_mark_done", "gates": {},
    "generated_at": 3000.0, "generated_by": "agent/x", "evidence_hash": "sha256:beef",
    "report_id": "closure-custom-1",
}
res3 = dc.verify_and_record_closure(DELIV, PROJ, actor="op", report=submitted)
ok(res3.get("grade") == "waive" and res3.get("report_id") == "closure-custom-1",
   "an agent-submitted report is persisted as-is with its own report_id")

# --- 6. error paths (fail closed, nothing persisted) ------------------------
ok("error" in dc.verify_and_record_closure(DELIV, PROJ, actor="op", report={"grade": "pass"}),
   "a submitted report lacking the closure schema is rejected")
ok("error" in dc.verify_and_record_closure("nope", PROJ),
   "verify_and_record_closure on a missing deliverable returns an error")
ok("error" in store.get_deliverable_closure_report("nope", project=PROJ),
   "get_deliverable_closure_report on a missing deliverable returns an error")
ok("error" in store.record_deliverable_closure(DELIV, {"no": "grade"}, project=PROJ),
   "record_deliverable_closure rejects an object without a grade")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
