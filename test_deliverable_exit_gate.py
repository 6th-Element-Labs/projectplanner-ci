#!/usr/bin/env python3
"""DELIVERABLES-22: deployment defaults, audit classifications, and runbook proof."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp(prefix="deliverable-exit-gate-")
os.environ["PM_DB_PATH"] = os.path.join(TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP

import store  # noqa: E402
from scripts import audit_deliverable_intake as audit  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


for unit in ("deploy/projectplanner.service", "deploy/projectplanner-mcp.service"):
    text = (ROOT / unit).read_text()
    ok("Environment=PM_ENFORCE_DELIVERABLE_INTAKE=1" in text,
       f"{unit} pins forward intake enforcement")

env_example = (ROOT / ".env.example").read_text()
ok("\nPM_ENFORCE_DELIVERABLE_INTAKE=1\n" in env_example,
   ".env.example enables intake enforcement for non-systemd installs")

runbook = (ROOT / "docs/DELIVERABLE-CLOSURE-GATE.md").read_text()
for phrase in ("Operator rollout and closeout runbook", "pending_contract", "grandfathered",
               "--require-clean", "systemctl restart projectplanner projectplanner-mcp",
               "DELIVERABLES-12 through DELIVERABLES-21", "after DELIVERABLES-22 reaches Done"):
    ok(phrase in runbook, f"runbook documents {phrase!r}")

project = "qa-exit-gate"
store.init_project_registry()
store.create_project("Exit gate QA", project_id=project, actor="test")
store.create_deliverable({"id": "draft", "title": "Draft", "status": "proposed"},
                         actor="test", project=project)
store.create_deliverable({"id": "legacy", "title": "Legacy", "status": "in_progress"},
                         actor="test", project=project)
store.create_deliverable({
    "id": "ready", "title": "Ready", "status": "in_progress",
    "end_state": "The outcome is proven.", "acceptance_criteria": ["scope is terminal"],
    "proof_requirements": {
        "schema": "switchboard.deliverable_proof_requirements.v1",
        "gates": [{"id": "scope", "required": True}],
    },
}, actor="test", project=project)

report = audit.classify(
    store.list_deliverables(project=project, include_task_snapshots=False), project)
by_id = {row["deliverable_id"]: row for row in report["deliverables"]}
ok(by_id["draft"]["classification"] == "pending_contract",
   "audit identifies a draft that needs a contract before transition")
ok(by_id["legacy"]["classification"] == "grandfathered",
   "audit identifies already-active legacy debt without retroactive mutation")
ok(by_id["ready"]["classification"] == "compliant",
   "audit recognizes a complete proof contract")
ok(report["counts"] == {"compliant": 1, "pending_contract": 1,
                         "grandfathered": 1, "ignored": 0} and report["ok"] is False,
   "audit emits deterministic counts and a fail-closed clean verdict")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
