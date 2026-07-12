#!/usr/bin/env python3
"""Self-contained tests for the closure-verification worker (DELIVERABLES-23).

Covers the safety filter (never auto-run a gate heavier than this host's
ceiling — mirrors the mcp-agent-path-performance dogfood, which ran the heavy
concurrent-load harness off-box on purpose) and the end-to-end CLI path
against a real seeded deliverable, so a closure_verification wake actually
produces and persists a graded report instead of the old ack-only stub.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="closure-verifier-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import store  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cv = _load("closure_verifier", REPO_ROOT / "adapters" / "closure_verifier.py")

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


store.init_project_registry()
store.init_db("switchboard")
store.create_project("Closure Verifier QA", project_id="qa-cv", actor="test")

# A cheap deliverable: scope only, no functional gates — the real shape of the
# stuck deliverable-event-driven-llm-narration wake this daemon was built for.
store.create_deliverable({
    "id": "qa-cv-cheap", "title": "cheap", "status": "in_progress",
    "acceptance_criteria": ["ships"],
    "proof_requirements": {"schema": "switchboard.deliverable_proof_requirements.v1",
                           "gates": [{"id": "scope", "required": True}]},
}, actor="test", project="qa-cv")

# A deliverable declaring one gate heavier than the auto-run ceiling.
store.create_deliverable({
    "id": "qa-cv-heavy", "title": "heavy", "status": "in_progress",
    "acceptance_criteria": ["ships"],
    "proof_requirements": {"schema": "switchboard.deliverable_proof_requirements.v1",
                           "gates": [
                               {"id": "scope", "required": True},
                               {"id": "harness:big", "kind": "script", "timeout_s": 600,
                                "command": ["python3", "-c", "import sys;sys.exit(0)"],
                                "required": True},
                           ]},
}, actor="test", project="qa-cv")

# --- _safe_to_auto_run ------------------------------------------------------

safe, heavy = cv._safe_to_auto_run("qa-cv-cheap", "qa-cv", 120)
ok(safe and heavy == [], "a scope-only deliverable is safe to auto-run")

safe, heavy = cv._safe_to_auto_run("qa-cv-heavy", "qa-cv", 120)
ok(not safe and heavy == ["harness:big"],
   "a deliverable declaring a gate above the ceiling is flagged, not auto-run")

safe, heavy = cv._safe_to_auto_run("qa-cv-heavy", "qa-cv", 900)
ok(safe, "raising the ceiling above the gate's own timeout clears it for auto-run")

ok(cv._auto_timeout_ceiling() == cv.DEFAULT_AUTO_TIMEOUT_CEILING_S,
   "auto-run ceiling defaults sanely with no env override")
os.environ["PM_CLOSURE_VERIFIER_AUTO_TIMEOUT_CEILING_S"] = "30"
ok(cv._auto_timeout_ceiling() == 30.0, "auto-run ceiling honors an env override")
os.environ.pop("PM_CLOSURE_VERIFIER_AUTO_TIMEOUT_CEILING_S", None)

# --- end-to-end CLI ----------------------------------------------------------

rc = cv.main(["--project", "qa-cv", "--deliverable-id", "qa-cv-cheap",
             "--host-id", "host/test"])
ok(rc == 0, "main() exits 0 for a real deliverable")
stored = store.get_deliverable_closure_report("qa-cv-cheap", project="qa-cv")
ok(stored.get("report", {}).get("grade") == "pass",
   "main() actually persisted a graded report — the whole point vs. the old ack-only stub")

rc_heavy = cv.main(["--project", "qa-cv", "--deliverable-id", "qa-cv-heavy",
                    "--host-id", "host/test"])
ok(rc_heavy == 0, "main() still exits 0 for a heavy-gate deliverable (a hold is a successful run)")
stored_heavy = store.get_deliverable_closure_report("qa-cv-heavy", project="qa-cv")
report_heavy = stored_heavy.get("report", {})
checks = {c["id"]: c for c in report_heavy.get("gates", {}).get("functional", {}).get("checks", [])}
ok(report_heavy.get("grade") == "hold", "heavy required gate left not_run holds the grade closed")
ok(checks.get("harness:big", {}).get("status") == "not_run",
   "the heavy gate itself is recorded not_run, never fabricated as a pass")

rc_missing = cv.main(["--project", "qa-cv", "--deliverable-id", "does-not-exist",
                      "--host-id", "host/test"])
ok(rc_missing == 1, "main() exits 1 for a deliverable that does not exist")

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
