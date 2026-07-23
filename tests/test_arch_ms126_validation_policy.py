#!/usr/bin/env python3
"""ARCH-MS-126 policy, evidence, readiness, and declarative-boundary proof."""
from __future__ import annotations

import json
import time
from pathlib import Path

from path_setup import ROOT  # noqa: E402

from fastapi import FastAPI
from fastapi.testclient import TestClient

from switchboard.domain.validation_policy import (
    infer_ui_impact,
    UI_CONTEXT,
    classify_task,
    project_validation_policy,
    ui_playwright_evidence_gate,
)
from switchboard.services.health import create_router


failures: list[str] = []


def ok(condition: bool, message: str) -> None:
    print(("PASS" if condition else "FAIL") + " " + message)
    if not condition:
        failures.append(message)


policy = project_validation_policy("switchboard")
ok(policy["schema"] == "switchboard.validation_policy.v1", "policy is project-wide v1")
ok(policy["required_status_context"] == UI_CONTEXT, "dedicated UI context is declared")
runner_source = (ROOT / "scripts" / "run_ui_playwright.py").read_text(encoding="utf-8")
ok('"completed_at": finished' in runner_source,
   "Playwright receipt includes the code-strict completion timestamp")

missing = classify_task(
    {"title": "Refactor repository adapter", "phase": "Build"},
    project="switchboard", material_rescope=True,
)
ok(not missing["ok"] and missing["error"] == "ui_impact_required",
   "ambiguous code task fails closed")

declared_no = classify_task(
    {"title": "Refactor repository adapter", "phase": "Build", "ui_impact": "no"},
    project="switchboard",
)
ok(declared_no["ok"] and declared_no["ui_impact"] == "no",
   "explicit non-UI code classification is accepted")

upgraded = classify_task(
    {"task_id": "ARCH-MS-X", "title": "Internal refactor", "ui_impact": "no"},
    project="switchboard", changed_files=["static/app.js"],
)
ok(upgraded["ui_impact"] == "yes" and upgraded["classification_source"] == "upgraded_from_false_no",
   "false no is automatically upgraded from the changed path")

# BUG-1: a word in a task description is not evidence of a UI change. CORE-1 on
# the atlas board ("router/tool/migration/worker/health registration hooks",
# four markdown files) was classified UI-impacting because "route" matched
# inside "router", and an explicit ui_impact=no could not overrule it.
prose_only = classify_task(
    {"task_id": "CORE-1", "ui_impact": "no",
     "title": "Define the core capability contract and package architecture ADR",
     "description": "router/tool/migration/worker/health registration hooks"},
    project="atlas",
)
ok(prose_only["ui_impact"] == "no",
   "a docs task whose description says 'router' is not UI-impacting")

# A description that does carry a real UI word, with no UI file behind it.
prose_token_only = classify_task(
    {"task_id": "CORE-9", "ui_impact": "no", "title": "Document the deploy profile",
     "description": "deploy notes only"},
    project="switchboard", changed_files=["docs/profiles.md"],
)
ok(prose_token_only["ui_impact"] == "no"
   and "prose_signal_ignored_without_file_evidence" in prose_token_only["reasons"],
   "prose alone cannot overrule an explicit ui_impact=no")

ok(not infer_ui_impact({"title": "router registration hooks"})["ui"],
   "'router' no longer trips the 'route' token")
ok(infer_ui_impact({"title": "fix caddy routing"})["ui"],
   "genuine routing work is still detected")

off_project = classify_task(
    {"title": "Browser session cookie work", "ui_impact": "yes"}, project="atlas")
ok(off_project["ui_validation"].get("required") is False,
   "Playwright evidence is not demanded from projects without the runner")
on_project = classify_task(
    {"title": "Browser session cookie work", "ui_impact": "yes"}, project="switchboard")
ok(on_project["ui_validation"]["required"] is True,
   "Switchboard UI work is still gated on exact-head Playwright evidence")

task = {
    "task_id": "ARCH-MS-X", "title": "Browser authentication change",
    "phase": "Build", "ui_impact": "yes", "agent_state": {},
}
session = {
    "work_session_id": "worksession-x", "branch": "codex/x", "head_sha": "a" * 40,
    "hygiene": {},
}
run = {
    "schema": "switchboard.executed_test_run.v1", "test_kind": "ui_playwright",
    "executed": True, "executed_count": 3, "skipped": False, "skipped_count": 0,
    "browser": "chromium", "chromium_version": "123.0", "headless": True,
    "tier": "hermetic", "base_url": "http://127.0.0.1:8120",
    "console_errors": [], "failed_requests": [], "artifact_hash": "sha256:abc",
    "task_id": "ARCH-MS-X", "work_session_id": "worksession-x",
    "branch": "codex/x", "head_sha": "a" * 40,
}
gate = ui_playwright_evidence_gate(
    task, {"executed_test_run": run}, session,
    project="switchboard", head_sha="a" * 40,
)
ok(gate["ok"] and gate["required"], "valid exact-head Chromium receipt passes")

for field, bad in (("executed_count", 0), ("skipped_count", 1),
                   ("console_errors", ["boom"]), ("head_sha", "b" * 40)):
    invalid = dict(run, **{field: bad})
    rejected = ui_playwright_evidence_gate(
        task, {"executed_test_run": invalid}, session,
        project="switchboard", head_sha="a" * 40,
    )
    ok(not rejected["ok"], f"invalid UI receipt field {field} is red")

waiver = {"approved_by": "verifier", "approved_at": time.time(), "reason": "fixture",
          "alternative_evidence": "artifact", "task_id": "WRONG", "expires_at": time.time() + 60}
ok(not ui_playwright_evidence_gate(
    task, {"ui_validation_waiver": waiver}, session, project="switchboard")["ok"],
   "waiver must be task-scoped")

app = FastAPI()
app.include_router(create_router(
    service_name="ready-test", readiness_probe=lambda: {
        "ok": True, "checks": {"database_schema": "ok",
                                 "browser_session_auth": "ok",
                                 "repository_read": "ok"}}))
client = TestClient(app)
ok(client.get("/health").status_code == 200, "shared liveness is cheap and open")
ok(client.get("/ready").status_code == 200, "shared readiness passes all dependencies")

inventory = json.loads((ROOT / "deploy/service-cut-inventory.json").read_text())
cuts = {item["port"]: item for item in inventory["services"]}
ok(all(cuts[port].get("ready") == "/ready" for port in range(8121, 8125)),
   "all four service cuts declare fail-closed readiness")

boundary = json.loads((ROOT / "deploy/service-boundary-contract.json").read_text())
ok(boundary["schema"] == "switchboard.service_boundary_contract.v1",
   "service boundary contract is machine-readable")
ok(len(boundary["identity_matrix"]) >= 6, "identity matrix covers least-privilege cases")
ok(boundary["sqlite"]["multiprocess_required"] is True,
   "SQLite proof requires multiple processes")
ok(boundary["extension_template"]["services"] == ["tally", "ingest"],
   "boundary harness is reusable for future cuts")

if failures:
    raise SystemExit(f"{len(failures)} ARCH-MS-126 assertion(s) failed")
print("PASS ARCH-MS-126 validation policy and boundary contract")
