#!/usr/bin/env python3
"""End-to-end closure-gate acceptance test (DELIVERABLES-21).

This file *is* the ``harness:test_deliverable_closure_gate`` registry gate. It
stands up a fixture deliverable with a fake harness (inline script gates) and
drives the whole operator flow through
``deliverable_closure.verify_and_record_closure`` (the DELIVERABLES-16
orchestrator over the DELIVERABLES-15 engine): scope pass/fail, functional
pass/fail via the fake harness, grades pass/hold/waive, report persistence + the
``deliverable.closure_verified`` audit stamp, and finally confirms the gate
registry now resolves this gate (no longer pending).

Auto-discovered by scripts/switchboard_ci.sh (recursive test_*.py find).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="closure-gate-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import store  # noqa: E402
import deliverable_closure as dc  # noqa: E402
import deliverable_gates  # noqa: E402

passed = failed = 0
PROJ = "qa-cl21"
D1 = "qa-cl21-primary"
D2 = "qa-cl21-failharness"

# Fake harness gates: deterministic inline scripts the closure engine executes.
PASS_HARNESS = {"id": "harness:fake_ok", "kind": "script",
                "command": ["python3", "-c", "import sys; sys.exit(0)"], "required": True}
FAIL_HARNESS = {"id": "harness:fake_bad", "kind": "script",
                "command": ["python3", "-c", "import sys; sys.exit(1)"], "required": True}


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def make_deliverable(did, gates):
    store.create_deliverable({
        "id": did, "title": did, "status": "in_progress",
        "acceptance_criteria": ["fixture ships"],
        "proof_requirements": {"schema": "switchboard.deliverable_proof_requirements.v1",
                               "gates": [{"id": "scope", "required": True}] + gates},
    }, actor="test", project=PROJ)


def closure_stamp_count():
    with store._conn(PROJ) as c:
        return c.execute(
            "SELECT COUNT(*) FROM activity WHERE kind='deliverable.closure_verified'").fetchone()[0]


store.init_project_registry()
store.init_db("switchboard")
store.create_project("Closure21 QA", project_id=PROJ, actor="test")
store.create_task({"workstream_id": "CL", "title": "shipped"}, actor="test", project=PROJ)   # CL-1
store.create_task({"workstream_id": "CL", "title": "open"}, actor="test", project=PROJ)       # CL-2
store.mark_task_merged("CL-1", "sha-1", pr_number=1, project=PROJ)  # terminal; CL-2 stays open

# Primary fixture: passing fake harness, one terminal + one non-terminal linked task.
make_deliverable(D1, [PASS_HARNESS])
for tid in ("CL-1", "CL-2"):
    store.link_task_to_deliverable(D1, PROJ, tid, actor="test", project=PROJ)

# --- scope FAIL: a non-terminal task holds closure even though the harness passes ---
rep_a = dc.verify_and_record_closure(D1, PROJ, actor="verifier", run_scripts=True)
ok(rep_a["report"]["gates"]["scope"]["pass"] is False,
   "scope fails while a linked task (CL-2) is non-terminal")
ok(rep_a["grade"] == "hold", "grade holds when scope fails despite a passing harness")

# --- waiver: waiving CL-2 clears scope; passing harness -> grade waive ---
rep_b = dc.verify_and_record_closure(
    D1, PROJ, actor="op", run_scripts=True,
    waivers=[{"task_id": "CL-2", "reason": "cut from scope", "approved_by": "op"}])
ok(rep_b["report"]["gates"]["scope"]["pass"] is True and rep_b["grade"] == "waive",
   "an operator waiver clears scope and grades waive")

# --- scope PASS + passing harness -> grade pass ---
store.mark_task_merged("CL-2", "sha-2", pr_number=2, project=PROJ)  # now all terminal
rep_c = dc.verify_and_record_closure(D1, PROJ, actor="verifier", run_scripts=True)
ok(rep_c["report"]["gates"]["scope"]["pass"] is True and rep_c["grade"] == "pass",
   "all terminal + passing harness grades pass")
ok(rep_c["report"]["recommendation"] == "safe_to_mark_done",
   "a pass grade recommends safe_to_mark_done")

# --- functional FAIL: a failing fake harness holds the grade closed (scope is clean) ---
make_deliverable(D2, [FAIL_HARNESS])
for tid in ("CL-1", "CL-2"):
    store.link_task_to_deliverable(D2, PROJ, tid, actor="test", project=PROJ)
rep_d = dc.verify_and_record_closure(D2, PROJ, actor="verifier", run_scripts=True)
ok(rep_d["report"]["gates"]["functional"]["pass"] is False and rep_d["grade"] == "hold",
   "a failing fake harness fails the functional gate -> hold")
fn = {c["id"]: c for c in rep_d["report"]["gates"]["functional"]["checks"]}
ok(fn["harness:fake_bad"]["status"] == "ran" and fn["harness:fake_bad"]["pass"] is False,
   "the fake harness actually executed and reported its failure")

# --- grade persistence: latest report retrievable, schema + evidence hash intact ---
latest = store.get_deliverable_closure_report(D1, project=PROJ)
ok(latest["grade"] == "pass"
   and latest["report"]["schema"] == "switchboard.deliverable_closure_report.v1",
   "the graded closure report persisted and is retrievable for the primary fixture")
ok(latest["report"]["evidence_hash"].startswith("sha256:"),
   "the persisted report carries an evidence hash")
ok(closure_stamp_count() >= 4,
   "every verification stamped a deliverable.closure_verified activity")

# --- self-check: this file is the (now non-pending) registered gate ---
gate = deliverable_gates.registry_gates()["harness:test_deliverable_closure_gate"]
ok(not gate.get("pending"),
   "harness:test_deliverable_closure_gate is no longer pending in the registry")
ok((REPO_ROOT / gate["command"][1]).exists(),
   "the registered gate command points at an existing target (this test)")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
