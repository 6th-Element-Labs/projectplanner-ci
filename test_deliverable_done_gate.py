#!/usr/bin/env python3
"""DELIVERABLES-19: fail-closed Done gate at the deliverable store boundary."""
from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="deliverable-done-gate-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

import store  # noqa: E402

passed = failed = 0
PROJECT = "qa-done-gate"


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def upsert(deliverable_id, status, metadata=None):
    data = {"id": deliverable_id, "title": deliverable_id, "status": status}
    if metadata is not None:
        data["metadata"] = metadata
    return store.create_deliverable(data, actor="test", project=PROJECT)


def closure(deliverable_id, grade, report_id):
    return store.record_deliverable_closure(deliverable_id, {
        "schema": "switchboard.deliverable_closure_report.v1",
        "report_id": report_id,
        "grade": grade,
        "recommendation": "safe_to_mark_done" if grade in ("pass", "waive") else "hold",
        "generated_at": 1234.0,
        "generated_by": "test/verifier",
        "evidence_hash": "sha256:test",
        "gates": {},
    }, actor="test/verifier", project=PROJECT)


store.init_project_registry()
store.create_project("Done gate QA", project_id=PROJECT, actor="test")

# New records cannot arrive Done, even if their own untrusted metadata claims pass.
res = upsert("new-done", "done")
ok(res.get("error") == "deliverable closure grade required"
   and res.get("last_closure_grade") is None,
   "a new status=done deliverable without persisted closure proof is rejected")
ok(store.get_deliverable("new-done", project=PROJECT) is None,
   "the rejected create did not write a deliverable")

res = upsert("spoof", "done", {"last_closure_grade": "pass",
                                "last_closure_report": {"grade": "pass"}})
ok(res.get("error") == "deliverable closure grade required",
   "caller-supplied closure metadata cannot bypass the Done gate")
ok(store.get_deliverable("spoof", project=PROJECT) is None,
   "the spoofed create did not write a deliverable")

# Missing/hold fail closed and do not change the current status.
upsert("held", "in_review")
res = upsert("held", "done")
ok(res.get("error") == "deliverable closure grade required",
   "an in-review deliverable with no grade cannot enter Done")
ok(store.get_deliverable("held", project=PROJECT).get("status") == "in_review",
   "missing-grade rejection leaves the previous status unchanged")

closure("held", "hold", "report-hold")
res = upsert("held", "done")
ok(res.get("error") == "deliverable closure grade required"
   and res.get("last_closure_grade") == "hold",
   "the latest hold grade cannot enter Done")
ok(store.get_deliverable("held", project=PROJECT).get("status") == "in_review",
   "hold-grade rejection leaves the previous status unchanged")

# Pass and operator waiver are the only accepted grades.
closure("held", "pass", "report-pass")
res = upsert("held", "done")
ok(res.get("status") == "done", "a persisted pass grade allows Done")
meta = res.get("metadata") or {}
ok(meta.get("last_closure_grade") == "pass"
   and (meta.get("last_closure_report") or {}).get("report_id") == "report-pass",
   "the Done upsert preserves verifier-owned closure metadata")

upsert("waived", "in_review")
closure("waived", "waive", "report-waive")
res = upsert("waived", "done", {"last_closure_grade": "hold", "operator_note": "kept"})
meta = res.get("metadata") or {}
ok(res.get("status") == "done" and meta.get("last_closure_grade") == "waive",
   "a persisted waiver allows Done and overrides spoofed closure metadata")
ok(meta.get("operator_note") == "kept",
   "ordinary caller-owned metadata still updates normally")

# A caller cannot overwrite or erase verifier-owned fields after persistence.
res = upsert("waived", "in_review", {"last_closure_grade": "pass"})
meta = res.get("metadata") or {}
ok(meta.get("last_closure_grade") == "waive"
   and (meta.get("last_closure_report") or {}).get("report_id") == "report-waive",
   "general upserts cannot overwrite persisted closure grade/report fields")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
