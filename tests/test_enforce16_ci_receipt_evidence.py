#!/usr/bin/env python3
"""ENFORCE-16: the merge gate consumes its own CI receipt as executed-test evidence.

On 2026-07-26 the completion autopilot re-dispatched an evidence-repair runner
50 times for COORD-57 / PR #936 with `missing_executed_test_run` — while
`Switchboard CI / VM gate` was green on the exact head SHA. The gate refused a
fact the server had already recorded in `external_ci_runs`: a passing full-suite
run pinned to that commit. Dispatching an agent to re-type a fact the gate can
read is a deterministic plumbing job done stochastically; it fails identically
on every attempt and the loop never converges.

complete_claim already adopted this doctrine (ADR-0008): "CI on the exact SHA is
the executor of record for 'the tests ran'; a locally attested hash is
bookkeeping." This test pins the same rule onto the merge-authorization path:

  * a green external CI run on the EXACT gated head satisfies the
    executed-test-run requirement, derived server-side, no dispatch;
  * the derivation is visible (a non-blocking finding), never silent;
  * a run on another head, a failed run, or no run at all keeps the refusal —
    the #859 rule stands: evidence is derived from a real run or not at all;
  * agent-recorded evidence still wins when present and valid.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

from path_setup import ROOT, SRC  # noqa: F401

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="enforce16-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402

store.init_db(project="switchboard")

from switchboard.application.commands import merge_gate as merge_gate_mod  # noqa: E402
from switchboard.storage.repositories.external_ci import (  # noqa: E402
    _external_ci_summary,
)

HEAD = "5cb76fab" + "a" * 32
OTHER_HEAD = "d" * 40
VM_GATE = "Switchboard CI / VM gate"
RUN_URL = (
    "https://github.com/6th-Element-Labs/projectplanner-ci/actions/runs/1"
)
PR_URL = "https://github.com/6th-Element-Labs/projectplanner/pull/936"
CONTRACT = {
    "source_repo": "6th-Element-Labs/projectplanner",
    "ci_repo": "6th-Element-Labs/projectplanner-ci",
    "status_context": VM_GATE,
}

passed = 0
failed = 0


def ok(condition: bool, label: str) -> None:
    global passed, failed
    if condition:
        passed += 1
        print("PASS  %s" % label)
    else:
        failed += 1
        print("FAIL  %s" % label)


def ci_row(sha: str, status: str = "success", conclusion: str = "success"):
    return {
        "run_id": f"ecir-{sha[:8]}-{status}",
        "status": status,
        "conclusion": conclusion,
        "source_sha": sha,
        "source_repo": CONTRACT["source_repo"],
        "ci_repo": CONTRACT["ci_repo"],
        "status_context": VM_GATE,
        "run_url": RUN_URL,
        "result": {},
    }


def review_gate_summary(rows, source_sha: str = ""):
    """Build the summary through the REAL production aggregator, then add the
    two keys `_external_ci_review_gate` layers on top."""
    summary = _external_ci_summary(
        list(rows), source_sha=source_sha, contract=dict(CONTRACT))
    summary["required"] = False
    summary["gate"] = {"name": "external_ci_passed", "required": False,
                       "passed": summary["passed"]}
    return summary


def run_gate(summary, evidence_extra=None):
    payload = {
        "task_id": "COORD-57",
        "head_sha": HEAD,
        "pr_url": PR_URL,
        "pr_number": 936,
        "session_policy_profile": "code_strict",
        "github_pr": {
            "number": 936,
            "state": "OPEN",
            "draft": False,
            "mergeable": True,
            "head": {"sha": HEAD},
            "status_contexts": [{"context": VM_GATE, "state": "SUCCESS"}],
        },
        **(evidence_extra or {}),
    }
    with mock.patch.object(
            merge_gate_mod, "_external_ci_review_gate",
            return_value=summary):
        return merge_gate_mod.merge_gate(
            payload, actor="enforce16-test", project="switchboard",
            record=False)


def finding_codes(result):
    return [f.get("code") for f in result.get("findings") or []]


def find(result, code):
    return next(
        (f for f in result.get("findings") or [] if f.get("code") == code),
        None)


# 1. Green CI run on the exact gated head satisfies the executed-test gate.
result = run_gate(review_gate_summary([ci_row(HEAD)], source_sha=HEAD))
ok("missing_executed_test_run" not in finding_codes(result),
   "green exact-head CI receipt clears missing_executed_test_run")
derived = find(result, "executed_test_run_derived_from_external_ci")
ok(derived is not None,
   "derivation is recorded as a visible finding, not a silent pass")
ok(bool(derived) and derived.get("blocking") is False,
   "derivation finding is non-blocking")
gate_report = (derived or {}).get("executed_test_gate") or {}
ok(gate_report.get("source") == "external_ci",
   "derived gate names external_ci as its source")
ok(RUN_URL in str(gate_report),
   "derived gate carries the CI run receipt (run_url)")

# 2. A green run on a DIFFERENT head must not authorize this head.
result = run_gate(review_gate_summary([ci_row(OTHER_HEAD)], source_sha=""))
ok("missing_executed_test_run" in finding_codes(result),
   "green CI on another head keeps the refusal")
ok(find(result, "executed_test_run_derived_from_external_ci") is None,
   "no derivation finding for a head-mismatched run")

# 3. A failed run derives nothing.
result = run_gate(review_gate_summary(
    [ci_row(HEAD, status="failure", conclusion="failure")], source_sha=HEAD))
ok("missing_executed_test_run" in finding_codes(result),
   "failed CI run keeps the refusal")

# 4. No recorded run at all derives nothing (the #859 fabrication rule).
result = run_gate(review_gate_summary([], source_sha=HEAD))
ok("missing_executed_test_run" in finding_codes(result),
   "no CI run recorded keeps the refusal")

# 5. Valid agent-recorded evidence still wins; no derivation finding appears.
agent_evidence = {
    "executed_test_run": {
        "commands": ["bash scripts/switchboard_ci.sh"],
        "passed": True,
        "output_sha256": "ab" * 32,
        "completed_at": "2026-07-26T22:00:00Z",
    },
}
result = run_gate(review_gate_summary([ci_row(HEAD)], source_sha=HEAD),
                  evidence_extra=agent_evidence)
ok("missing_executed_test_run" not in finding_codes(result),
   "agent-recorded evidence passes the gate")
ok(find(result, "executed_test_run_derived_from_external_ci") is None,
   "agent-recorded evidence needs no derivation")

# 6. Pending CI derives nothing.
result = run_gate(review_gate_summary(
    [ci_row(HEAD, status="running", conclusion="")], source_sha=HEAD))
ok("missing_executed_test_run" in finding_codes(result),
   "pending CI run keeps the refusal")

# 7. Loop death, end to end: feed the classifier the REAL finding set the fixed
# gate produces (only the non-blocking derivation finding) and assert the
# completion state machine no longer plans an evidence-repair dispatch — the
# exact effect that ran 50 times on COORD-57.
from switchboard.domain.completion import (  # noqa: E402
    build_completion_snapshot, classify_completion,
)
from switchboard.domain.completion.normalize import normalize_snapshot  # noqa: E402

derived_result = run_gate(review_gate_summary([ci_row(HEAD)], source_sha=HEAD))
derived_findings = list(derived_result.get("findings") or [])
snapshot = normalize_snapshot(build_completion_snapshot(
    task={
        "task_id": "COORD-57",
        "status": "In Review",
        "git_state": {"head_sha": HEAD, "pr_number": 936, "pr_url": PR_URL},
    },
    github_pr={
        "number": 936,
        "html_url": PR_URL,
        "state": "OPEN",
        "draft": False,
        "mergeable": True,
        "mergeStateStatus": "BLOCKED",
        "head": {"sha": HEAD},
        "status_contexts": [{"context": VM_GATE, "state": "SUCCESS"}],
    },
    required_status_contexts=[VM_GATE],
    review={"status": "passed", "head_sha": HEAD, "pr_url": PR_URL},
    merge_gate={"findings": [
        f for f in derived_findings
        if f.get("code") == "executed_test_run_derived_from_external_ci"
    ]},
))
decision = classify_completion(None, snapshot)
ok(decision.get("reason_code") != "missing_executed_test_run",
   "classifier no longer sees missing_executed_test_run")
ok(decision.get("route") != "coordination_retry",
   "classifier no longer routes the repair loop (coordination_retry)")
ok(decision.get("route") == "review_merge",
   "green PR with derived evidence routes review_merge (toward enqueue)")

print(f"\nenforce16 CI-receipt evidence: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
