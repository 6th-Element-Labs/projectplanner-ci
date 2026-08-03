#!/usr/bin/env python3
"""BUG-273/274/275: the UI validation gate must be project-scoped and falsifiable.

Three defects found completing project=simplemark task FOUNDATION-1:

BUG-273  ``ui_playwright_evidence_gate`` published
         ``classification.ui_validation.required = false`` and blocked anyway.
BUG-274  Every non-Switchboard project was handed Switchboard's own CI context
         and ``python3 scripts/run_ui_playwright.py``, which a TypeScript repo
         cannot produce. Impossible evidence, not merely absent evidence.
BUG-275  Prose in a task description set ``ui_impact=yes`` even when the diff
         was known and contained no UI file at all.
"""
from __future__ import annotations

import json
from pathlib import Path

from path_setup import ROOT  # noqa: E402

from switchboard.domain import validation_policy as vp
from switchboard.domain.validation_policy import (
    UI_CONTEXT,
    classify_task,
    project_validation_policy,
    ui_playwright_evidence_gate,
    ui_required_status_context,
    ui_validation_enforced,
)


failures: list[str] = []


def ok(condition: bool, message: str) -> None:
    print(("PASS" if condition else "FAIL") + " " + message)
    if not condition:
        failures.append(message)


UI_TASK = {
    "task_id": "FOUNDATION-1",
    "title": "Establish browser entrypoint locations",
    "description": "browser entrypoint locations and src/app/ui skeleton",
    "ui_impact": "yes",
    "agent_state": {},
}
SESSION = {"work_session_id": "worksession-x", "branch": "codex/x",
           "head_sha": "a" * 40, "hygiene": {}}


# --- BUG-273: the gate must honour the classification it publishes -----------

for project in ("simplemark", "helm", "atlas"):
    gate = ui_playwright_evidence_gate(
        UI_TASK, {}, SESSION, project=project, head_sha="a" * 40,
        changed_files=["src/app/browser.ts"])
    requirement = gate["classification"]["ui_validation"]
    ok(requirement.get("required") is False,
       f"{project}: classification still says UI validation does not apply")
    ok(gate.get("ok") is True and gate.get("required") is False,
       f"{project}: gate honours its own ui_validation.required=false")
    ok(bool(gate.get("reason")),
       f"{project}: the not-required verdict names why it was not required")

# The gate must not be disarmed for the project that does own the runner.
blocked = ui_playwright_evidence_gate(
    UI_TASK, {}, SESSION, project="switchboard", head_sha="a" * 40,
    changed_files=["static/app.js"])
ok(blocked.get("ok") is False
   and blocked.get("reason") == "missing_ui_playwright_evidence",
   "switchboard UI work with no receipt is still red")

# A project that opts in owns the demand too: enforcement is declared, not assumed.
ok(ui_validation_enforced("switchboard") is True,
   "switchboard still enforces UI validation")
ok(ui_validation_enforced("simplemark") is False,
   "simplemark does not enforce a runner it does not have")

# The merge gate must ask for a context the project's CI can actually publish.
ok(ui_required_status_context("switchboard") == UI_CONTEXT,
   "switchboard UI work still requires the Switchboard CI context")
for project in ("simplemark", "helm", "atlas"):
    ok(ui_required_status_context(project) == "",
       f"{project}: no Switchboard status context is demanded of it")


# --- BUG-274: validation_policy resolves per project -------------------------

switchboard_policy = project_validation_policy("switchboard")
ok(switchboard_policy["required_status_context"] == UI_CONTEXT,
   "switchboard policy is unchanged: Switchboard CI / VM gate")
ok(switchboard_policy["runner"]["command"] == "python3 scripts/run_ui_playwright.py",
   "switchboard policy is unchanged: Playwright runner")
ok(switchboard_policy.get("unconfigured") is not True,
   "switchboard is a configured project")

simplemark_policy = project_validation_policy("simplemark")
ok(simplemark_policy["project"] == "simplemark",
   "simplemark policy is scoped to simplemark")
ok(simplemark_policy["required_status_context"] == "gate",
   "simplemark gets its own CI context, not Switchboard's")
ok(simplemark_policy["runner"]["command"] == "bash scripts/simplemark_ci.sh",
   "simplemark gets its own gate command, not a Python script it does not have")

helm_policy = project_validation_policy("helm")
ok(helm_policy["required_status_context"] == "helm-ci/full-suite",
   "helm gets helm-ci/full-suite, not Switchboard CI / VM gate")

# An unconfigured project must inherit nothing impossible, and must say so.
atlas_policy = project_validation_policy("atlas")
ok(atlas_policy["required_status_context"] == "",
   "an unconfigured project claims no status context")
ok(atlas_policy["runner"]["command"] == "",
   "an unconfigured project claims no runner command")
ok(atlas_policy.get("unconfigured") is True,
   "the missing-policy fallback is named, not silently green")

# Project names are file lookups; they must not be able to escape deploy/.
for hostile in ("../../etc/passwd", "switchboard/../helm", "..", "/etc/passwd"):
    resolved = project_validation_policy(hostile)
    ok(resolved.get("unconfigured") is True
       and resolved["required_status_context"] == "",
       f"path-traversal project id {hostile!r} resolves to the neutral policy")

# A corrupt or missing shared policy file must not quietly disarm Switchboard.
original_path = vp._POLICY_PATH
try:
    vp._POLICY_PATH = ROOT / "deploy" / "validation-policy.does-not-exist.json"
    fallback = project_validation_policy("switchboard")
    ok(fallback["required_status_context"] == UI_CONTEXT
       and ui_validation_enforced("switchboard") is True,
       "an unreadable policy file fails closed for switchboard, not open")
finally:
    vp._POLICY_PATH = original_path

# The shipped files are real and self-describing.
for name, project in (("validation-policy.json", "switchboard"),
                      ("validation-policy.simplemark.json", "simplemark"),
                      ("validation-policy.helm.json", "helm")):
    path = Path(ROOT / "deploy" / name)
    ok(path.exists(), f"deploy/{name} exists")
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        ok(payload.get("schema") == "switchboard.validation_policy.v1"
           and payload.get("project") == project,
           f"deploy/{name} declares its own project and schema")


# --- BUG-275: a prose signal must be falsifiable by the diff ------------------

PROSE = {"task_id": "FOUNDATION-1",
         "title": "Establish browser entrypoint locations",
         "description": "browser entrypoint locations and src/app/ui skeleton"}

known_diff_no_ui = classify_task(
    PROSE, project="simplemark", existing={},
    changed_files=["src/app/browser.ts", "src/app/ui/README.md", "package.json"])
ok(known_diff_no_ui["ui_impact"] == "no",
   "prose alone cannot set ui_impact=yes against a diff that renders nothing")
ok("prose_signal_ignored_without_file_evidence" in known_diff_no_ui["reasons"],
   "the classification says why the prose signal lost")

# The same protection an explicit "no" already had (FIDELITY-1's path).
explicit_no = classify_task(
    dict(PROSE, ui_impact="no"), project="simplemark", existing={},
    changed_files=["src/app/browser.ts"])
ok(explicit_no["ui_impact"] == "no",
   "an explicit ui_impact=no is still protected from prose")

# Real UI file evidence still wins, with or without a declaration.
with_ui_file = classify_task(
    PROSE, project="switchboard", existing={}, changed_files=["static/app.js"])
ok(with_ui_file["ui_impact"] == "yes"
   and any(r.startswith("ui_path:") for r in with_ui_file["reasons"]),
   "a real UI file in the diff still classifies as UI-impacting")

upgraded = classify_task(
    dict(PROSE, ui_impact="no"), project="switchboard", existing={},
    changed_files=["static/app.js"])
ok(upgraded["ui_impact"] == "yes"
   and upgraded["classification_source"] == "upgraded_from_false_no",
   "a false ui_impact=no is still upgraded from the changed path")

# Before any diff exists, prose must still fail closed. Requiring file evidence
# unconditionally would classify every not-yet-written UI task as "no" and
# disarm the gate for Switchboard's own work.
no_diff_yet = classify_task(PROSE, project="switchboard", existing={})
ok(no_diff_yet["ui_impact"] == "yes",
   "with no diff available, a prose UI signal still fails closed")
empty_diff = classify_task(PROSE, project="switchboard", existing={}, changed_files=[])
ok(empty_diff["ui_impact"] == "yes",
   "an empty changed-file list is unknown, not proof of a UI-free diff")


if failures:
    raise SystemExit(f"{len(failures)} BUG-273/274/275 assertion(s) failed")
print("PASS BUG-273/274/275 project-scoped, falsifiable UI validation policy")
