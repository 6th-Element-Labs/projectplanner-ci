#!/usr/bin/env python3
"""Self-contained tests for the deliverable closure engine (DELIVERABLES-15).

Pure unit tests drive scope_gate / run_gate with synthetic inputs; an
integration section seeds a real deliverable (with genuine merge provenance via
store.mark_task_merged) and exercises verify_deliverable_closure end-to-end:
scope hold/pass, waivers, functional script execution, the not_run fail-closed
path, agent-submitted results, and deterministic grading + evidence hashing.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="closure-engine-")
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


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def raises(exc_type, fn, message):
    try:
        fn()
    except exc_type:
        ok(True, message)
    except Exception as other:  # noqa: BLE001
        ok(False, f"{message} (raised {type(other).__name__}: {other})")
    else:
        ok(False, f"{message} (no exception raised)")


# --- pure scope_gate unit tests --------------------------------------------

def _link(task_id, status, terminal=False, ptype=None):
    return {"project_id": "qa", "task_id": task_id,
            "task_detail": {"task_id": task_id, "status": status,
                            "provenance": {"type": ptype, "terminal": terminal}}}


def _ms(blockers=(), in_review=0, linked=(), done=0, total=None, ratio=0.0):
    linked = list(linked)
    return {
        "blockers": list(blockers),
        "progress": {
            "in_review_count": in_review,
            "done_with_proof_count": done,
            "linked_task_count": total if total is not None else len(linked),
            "done_with_proof_ratio": ratio,
        },
        "linked_tasks": linked,
    }


clean = _ms(linked=[_link("A", "Done", True, "github_pr_merged")], done=1, total=1)
ok(dc.scope_gate(clean)["pass"] is True,
   "scope passes: no blockers, none in review, all terminal, ratio 1.0")

ok(dc.scope_gate(_ms(blockers=[{"kind": "task_blocked"}],
                     linked=[_link("A", "Done", True)], done=1))["pass"] is False,
   "a blocker fails scope")

ok(dc.scope_gate(_ms(in_review=1, linked=[_link("A", "In Review")], total=1))["pass"] is False,
   "an In Review task fails scope (no_in_review)")

inprog = dc.scope_gate(_ms(linked=[_link("A", "In Progress")], total=1))
ok(inprog["pass"] is False
   and any(c["id"] == "no_in_progress" and not c["pass"] for c in inprog["checks"]),
   "an In Progress task fails no_in_progress")

nonterm = dc.scope_gate(_ms(linked=[_link("A", "Done", False)], done=0, total=1))
ok(any(c["id"] == "terminal_or_waived" and not c["pass"] for c in nonterm["checks"]),
   "Done without terminal provenance is not terminal")

waived = dc.scope_gate(_ms(linked=[_link("A", "Not Started")], total=1),
                       waivers=[{"task_id": "A", "reason": "cut from scope", "approved_by": "op"}])
ok(waived["pass"] is True, "an operator waiver excludes a non-terminal task from scope")

ok(dc.scope_gate(_ms(linked=[_link("A", "Cancelled")], total=1))["pass"] is True,
   "a Cancelled task is terminal for closure")

# Cancelled is excluded from the proof-ratio denominator, so it does not fail the ratio.
mixed = dc.scope_gate(_ms(linked=[_link("A", "Done", True, "github_pr_merged"),
                                  _link("B", "Cancelled")], done=1, total=2))
ok(mixed["pass"] is True, "a Cancelled sibling does not drag down done_with_proof_ratio")

# A real non-shipped task without proof does fail the default 1.0 ratio.
ratio_fail = dc.scope_gate(_ms(linked=[_link("A", "Done", True, "github_pr_merged"),
                                       _link("B", "Done", False)], done=1, total=2))
ok(any(c["id"] == "done_with_proof_ratio" and not c["pass"] for c in ratio_fail["checks"]),
   "ratio floor fails when a linked task lacks proof")

raises(dc.ClosureError, lambda: dc.scope_gate(_ms(), waivers=[{"reason": "x"}]),
       "a waiver without task_id fails closed")
raises(dc.ClosureError, lambda: dc.scope_gate(_ms(), waivers=[{"task_id": "A"}]),
       "a waiver without a reason fails closed")


# --- run_gate unit tests ----------------------------------------------------

sc_ok = dc.run_gate({"id": "store:r", "kind": "store_check", "check": "min_done_with_proof_ratio",
                     "params": {"min": 0.5}}, project="qa",
                    mission_status={"progress": {"done_with_proof_ratio": 0.75}})
ok(sc_ok["pass"] is True and sc_ok["status"] == "checked", "store_check min ratio passes at 0.75>=0.5")

sc_unknown = dc.run_gate({"id": "store:u", "kind": "store_check", "check": "nope"},
                         project="qa", mission_status={"progress": {}})
ok(sc_unknown["pass"] is False and sc_unknown["status"] == "error",
   "unknown store_check predicate fails closed")

not_run = dc.run_gate({"id": "harness:x", "kind": "script", "command": ["python3", "-c", "pass"],
                       "required": True}, project="qa", mission_status={}, run_scripts=False)
ok(not_run["pass"] is None and not_run["status"] == "not_run",
   "a command gate is not_run (never optimistically passed) without run_scripts or a submitted result")

ran_ok = dc.run_gate({"id": "harness:ok", "kind": "script", "command": ["python3", "-c", "import sys;sys.exit(0)"]},
                     project="qa", mission_status={}, run_scripts=True)
ok(ran_ok["pass"] is True and ran_ok["status"] == "ran" and ran_ok["artifact_hash"].startswith("sha256:"),
   "run_scripts executes a passing script gate and hashes its output")

ran_bad = dc.run_gate({"id": "harness:bad", "kind": "script", "command": ["python3", "-c", "import sys;sys.exit(1)"]},
                      project="qa", mission_status={}, run_scripts=True)
ok(ran_bad["pass"] is False and ran_bad["exit_code"] == 1, "a failing script gate reports pass=False")

submitted = dc.run_gate({"id": "harness:ok", "kind": "script", "command": ["false"], "required": True},
                        project="qa", mission_status={},
                        submitted_functional={"harness:ok": {"pass": True, "duration_s": 2.0}})
ok(submitted["pass"] is True and submitted["status"] == "submitted",
   "an agent-submitted result is used instead of running the command")


# --- integration: real deliverable end-to-end ------------------------------

store.init_project_registry()
store.init_db("switchboard")
store.create_project("Closure QA", project_id="qa-closure", actor="test")
for title in ("shipped A", "shipped B", "open C"):
    store.create_task({"workstream_id": "CL", "title": title}, actor="test", project="qa-closure")
store.mark_task_merged("CL-1", "sha-aaaaaa", pr_number=1, project="qa-closure")
store.mark_task_merged("CL-2", "sha-bbbbbb", pr_number=2, project="qa-closure")

store.create_deliverable({
    "id": "qa-closure-deliv",
    "title": "QA closure",
    "status": "in_progress",
    "acceptance_criteria": ["A ships", "B ships"],
    "proof_requirements": {
        "schema": "switchboard.deliverable_proof_requirements.v1",
        "gates": [
            {"id": "scope", "required": True},
            {"id": "store:a_terminal", "kind": "store_check", "check": "task_terminal",
             "params": {"task_id": "CL-1"}, "required": True},
            {"id": "harness:ok", "kind": "script",
             "command": ["python3", "-c", "import sys;sys.exit(0)"], "required": True},
        ],
    },
}, actor="test", project="qa-closure")
for tid in ("CL-1", "CL-2", "CL-3"):
    store.link_task_to_deliverable("qa-closure-deliv", "qa-closure", tid,
                                   actor="test", project="qa-closure")

# Scenario 1 — CL-3 is Not Started: scope holds, grade hold, but the functional gates run.
rep = dc.verify_deliverable_closure("qa-closure-deliv", "qa-closure",
                                    run_scripts=True, generated_by="test", now=1000.0)
ok(rep["schema"] == dc.CLOSURE_REPORT_SCHEMA, "report carries the closure_report schema")
ok(rep["gates"]["scope"]["pass"] is False, "scope holds while CL-3 is non-terminal")
ok(rep["grade"] == "hold" and rep["recommendation"] == "hold", "grade hold when scope fails")
fn = {c["id"]: c for c in rep["gates"]["functional"]["checks"]}
ok(fn["store:a_terminal"]["pass"] is True, "store_check task_terminal(CL-1) passes on a merged task")
ok(fn["harness:ok"]["pass"] is True and fn["harness:ok"]["status"] == "ran", "script gate ran and passed")
ok(rep["evidence_hash"].startswith("sha256:"), "report carries an evidence hash")
ok(len(rep["acceptance_criteria_results"]) == 2
   and all(a["pass"] is None for a in rep["acceptance_criteria_results"]),
   "free-text acceptance criteria are listed unassessed, not optimistically passed")

# Scenario 2 — waive CL-3: scope clears, grade waive.
rep2 = dc.verify_deliverable_closure("qa-closure-deliv", "qa-closure", run_scripts=True,
                                     waivers=[{"task_id": "CL-3", "reason": "cut", "approved_by": "op"}],
                                     now=1000.0)
ok(rep2["gates"]["scope"]["pass"] is True, "waiving CL-3 clears scope")
ok(rep2["grade"] == "waive", "grade waive when the pass is achieved via a waiver")

# Scenario 3 — CL-3 also merged, no waivers: clean pass.
store.mark_task_merged("CL-3", "sha-cccccc", pr_number=3, project="qa-closure")
rep3 = dc.verify_deliverable_closure("qa-closure-deliv", "qa-closure", run_scripts=True, now=1000.0)
ok(rep3["gates"]["scope"]["pass"] is True and rep3["grade"] == "pass",
   "all terminal + functional pass -> grade pass")
ok(rep3["recommendation"] == "safe_to_mark_done", "clean pass recommends safe_to_mark_done")

# Scenario 4 — no run_scripts, no submitted result: required command gate holds closed.
rep4 = dc.verify_deliverable_closure("qa-closure-deliv", "qa-closure", now=1000.0)
fn4 = {c["id"]: c for c in rep4["gates"]["functional"]["checks"]}
ok(fn4["harness:ok"]["status"] == "not_run" and rep4["grade"] == "hold",
   "a required not_run command gate holds the grade even when scope is clean")

# Scenario 5 — agent submits the harness result: pass without running anything.
rep5 = dc.verify_deliverable_closure("qa-closure-deliv", "qa-closure", now=1000.0,
                                     submitted_functional={"harness:ok": {"pass": True, "duration_s": 1.2}})
ok(rep5["grade"] == "pass" and fn4 and rep5["gates"]["functional"]["pass"] is True,
   "agent-submitted functional result yields a pass without in-process execution")

# offline_evidence gate specifically requires offline provenance (a PR-merge is not enough).
off = dc.run_gate({"id": "offline:x", "kind": "offline_evidence", "task_id": "CL-1",
                   "task_project": "qa-closure"}, project="qa-closure", mission_status={})
ok(off["pass"] is False, "offline_evidence gate fails for a PR-merged (non-offline) task")

# Determinism + missing deliverable.
ok(rep3["evidence_hash"] == dc.verify_deliverable_closure(
    "qa-closure-deliv", "qa-closure", run_scripts=True, now=1000.0)["evidence_hash"],
   "evidence hash is deterministic for identical inputs")
raises(dc.ClosureError, lambda: dc.verify_deliverable_closure("nope", "qa-closure"),
       "a missing deliverable raises ClosureError")

# A dangling gate reference in proof_requirements surfaces as the engine's own error.
store.create_deliverable({"id": "qa-bad", "title": "bad", "status": "in_progress",
                          "proof_requirements": {"gates": [{"id": "harness:nope", "required": True}]}},
                         actor="test", project="qa-closure")
raises(dc.ClosureError, lambda: dc.verify_deliverable_closure("qa-bad", "qa-closure"),
       "a dangling gate reference in proof_requirements raises ClosureError, not GateResolutionError")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
