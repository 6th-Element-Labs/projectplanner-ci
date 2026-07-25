#!/usr/bin/env python3
"""COORD-49: the merge-authorization projection must not route completion.

`Switchboard / merge authorization` is a required status context that restates
Switchboard's own merge gate. Because a GitHub commit status carries no typed
code, every process-state problem (draft PR, missing review verdict, missing
executed_test_run) arrived at the classifier as one undifferentiated red
context, was attributed to `product`, and short-circuited `_required_ci_decision`
into `required_exact_head_ci_failed` -> remediation, before the typed branches
that already handle those cases could run. That is the DOGFOOD-19 stall: PRs
#863, #865, #867 all reached green product CI and then burned remediation
runners instead of finishing.

The fix defers that one context to the typed findings already on the snapshot,
and only when those findings are actually in hand.
"""
from __future__ import annotations

import importlib.util
from unittest import mock

from path_setup import ROOT, SRC  # noqa: F401

from switchboard.domain.completion import (
    MERGE_AUTHORIZATION_CONTEXT,
    build_completion_snapshot,
    classify_completion,
)
from switchboard.domain.completion.normalize import normalize_snapshot


HEAD = "c" * 40
REPO_PR = "https://github.com/6th-Element-Labs/projectplanner/pull/867"
VM_GATE = "Switchboard CI / VM gate"

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


def snapshot(
    *,
    draft: bool = False,
    vm_gate: str = "SUCCESS",
    merge_auth: str = "FAILURE",
    merge_auth_description: str = "Draft PRs cannot pass the merge gate.",
    review: str = "passed",
    review_head: str = HEAD,
    findings=(),
):
    """Reproduce the live shape: raw GitHub contexts, then production normalize.

    `merge_auth` defaults to FAILURE because that is the state every stalled
    DOGFOOD-19 PR was actually in. The contexts carry no `failure_attribution` —
    exactly like real `StatusContext` payloads — so `normalize_snapshot` has to
    infer it, which is where the type was being lost.
    """
    raw = build_completion_snapshot(
        task={
            "task_id": "COORD-49",
            "status": "In Review",
            "git_state": {"head_sha": HEAD, "pr_number": 867, "pr_url": REPO_PR},
        },
        github_pr={
            "number": 867,
            "html_url": REPO_PR,
            "state": "OPEN",
            "draft": draft,
            "mergeable": True,
            "mergeStateStatus": "BLOCKED",
            "head": {"sha": HEAD},
            "status_contexts": [
                {"context": VM_GATE, "state": vm_gate},
                {
                    "context": MERGE_AUTHORIZATION_CONTEXT,
                    "state": merge_auth,
                    "description": merge_auth_description,
                },
            ],
        },
        required_status_contexts=[VM_GATE, MERGE_AUTHORIZATION_CONTEXT],
        review={
            "status": review,
            "head_sha": review_head,
            "pr_url": REPO_PR,
        },
        merge_gate={"findings": list(findings)},
    )
    return normalize_snapshot(raw)


def finding(code: str, message: str = ""):
    return {
        "code": code,
        "message": message or code,
        "failure_class": "failed_gate",
        "blocking": True,
    }


# --- The projection is genuinely red and genuinely product-attributed --------
# Everything below rests on this: without the fix, normalize really does stamp
# the merge-auth context `product`, so the assertions are testing the live
# defect and not a strawman fixture.
_probe = snapshot(draft=True, findings=[finding("draft_pr")])
ok(
    _probe["status_contexts"][MERGE_AUTHORIZATION_CONTEXT]["failure_attribution"]
    == "product",
    "merge-auth prose still normalizes to product (the trap this fix routes around)",
)

# --- Acceptance 1: draft PR with green product CI reaches mark_ready ---------
draft_decision = classify_completion(
    None, snapshot(draft=True, findings=[finding("draft_pr")]))
ok(draft_decision["reason_code"] == "draft_ready_to_mark_ready",
   "draft PR + green VM gate classifies draft_ready_to_mark_ready")
ok(draft_decision["effect"] == "mark_ready_then_reread",
   "draft PR yields the mark_ready_then_reread effect")
ok(draft_decision["route"] == "review_merge",
   "draft PR routes review_merge, not remediation")
ok(draft_decision["state"] == "ready_to_queue",
   "draft PR is ready_to_queue, not blocked")

# --- Acceptance 2: missing exact-head verdict routes review, not remediation -
missing_verdict = classify_completion(None, snapshot(
    review="", findings=[finding("review_verdict_missing")]))
stale_verdict = classify_completion(None, snapshot(
    review_head="d" * 40, findings=[finding("review_verdict_stale")]))
ok(missing_verdict["route"] == "review_merge"
   and missing_verdict["desired_role"] == "review_merge",
   "non-draft PR with no exact-head verdict routes review_merge")
ok(stale_verdict["route"] == "review_merge",
   "non-draft PR with a stale verdict routes review_merge")

# --- Acceptance 3: missing executed_test_run routes to the evidence step -----
evidence = classify_completion(
    None, snapshot(findings=[finding("missing_executed_test_run")]))
ok(evidence["route"] == "coordination_retry",
   "missing executed_test_run routes coordination_retry (evidence), not remediation")
ok(evidence["reason_code"] == "missing_executed_test_run",
   "missing executed_test_run keeps its typed reason code")
ok(evidence["retry_policy"] == "bounded",
   "the evidence step retries under a bounded budget")

# --- Acceptance 4: ordinary red product CI still routes to remediation -------
red_product_ci = classify_completion(
    None, snapshot(vm_gate="FAILURE", findings=[finding("draft_pr")], draft=True))
ok(red_product_ci["route"] == "remediation",
   "a red VM gate still routes remediation even while merge-auth is deferred")
ok(red_product_ci["reason_code"] == "required_exact_head_ci_failed",
   "a red VM gate keeps required_exact_head_ci_failed")
ok(red_product_ci["board_projection"] == "Blocked",
   "a red VM gate still projects Blocked")

# --- Acceptance 5: fail closed when there are no typed findings --------------
no_findings = classify_completion(None, snapshot(findings=[]))
ok(no_findings["state"] != "ready_to_queue",
   "red merge-auth with zero findings never becomes ready_to_queue")
ok(no_findings["route"] == "remediation"
   and no_findings["reason_code"] == "required_exact_head_ci_failed",
   "red merge-auth with zero findings retains today's behaviour")

untyped_only = classify_completion(
    None, snapshot(findings=[{"message": "something went wrong", "blocking": True}]))
ok(untyped_only["state"] != "ready_to_queue",
   "a finding with no route-bearing code does not license deferral")

non_blocking_only = classify_completion(
    None, snapshot(findings=[{"code": "draft_pr", "blocking": False}]))
ok(non_blocking_only["state"] != "ready_to_queue",
   "a non-blocking finding does not license deferral")

# --- Acceptance 6: no process-state failure yields required_exact_head_ci_failed
for code in ("draft_pr", "review_verdict_missing", "review_verdict_stale",
             "missing_executed_test_run", "missing_ui_playwright_evidence",
             "pr_not_mergeable", "work_session_required"):
    decision = classify_completion(None, snapshot(
        draft=(code == "draft_pr"),
        review="" if code.startswith("review_verdict") else "passed",
        findings=[finding(code)],
    ))
    ok(decision["reason_code"] != "required_exact_head_ci_failed",
       "process-state finding %s never becomes required_exact_head_ci_failed" % code)
    ok(decision["desired_role"] != "remediation",
       "process-state finding %s never dispatches a remediation runner" % code)

# --- The deferral is scoped to exactly one context ---------------------------
other_red_context = classify_completion(None, snapshot(
    vm_gate="FAILURE", merge_auth="SUCCESS", findings=[finding("draft_pr")],
    draft=True))
ok(other_red_context["reason_code"] == "required_exact_head_ci_failed",
   "deferral does not leak to any other required context")


# --- The discarded reason code is kept as diagnostics, never as transport -----
# `run_merge_authorization_for_pr` computes the blocking code and then drops it
# at the GitHub API boundary, which carries no structured attribution. Persist
# it in Switchboard's own ledger so an operator reading a red context can get
# back to the finding — explicitly flagged as non-routing.
_gate_spec = importlib.util.spec_from_file_location(
    "switchboard_pr_gate_coord49", ROOT / "scripts" / "switchboard_pr_gate.py")
_gate = importlib.util.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(_gate)

_ledger = []
with mock.patch.object(_gate.store, "append_activity",
                       side_effect=lambda *a, **k: _ledger.append((a, k))):
    _gate._record_merge_authorization(
        [{"task_id": "COORD-49", "project": "switchboard"}],
        repo="6th-Element-Labs/projectplanner", number=867, sha=HEAD,
        context=MERGE_AUTHORIZATION_CONTEXT, state="failure", reason="draft_pr",
        description="Draft PRs cannot pass the merge gate.",
        blocked=[finding("draft_pr")],
    )
ok(len(_ledger) == 1, "a published merge authorization writes one ledger row")
_payload = _ledger[0][0][2]
ok(_ledger[0][0][0] == "merge.authorization.published",
   "the ledger row is a merge.authorization.published activity")
ok(_payload["reason_code"] == "draft_pr",
   "the code GitHub could not carry is preserved in the ledger")
ok(_payload["blocking_codes"] == ["draft_pr"],
   "every blocking code is preserved, not just the first")
ok(_payload["routing_authority"] is False,
   "the ledger row is explicitly marked as non-routing diagnostics")
ok(_ledger[0][1]["task_id"] == "COORD-49"
   and _ledger[0][1]["project"] == "switchboard",
   "the row is filed against the resolved task and project")

_boom = []
with mock.patch.object(_gate.store, "append_activity",
                       side_effect=RuntimeError("ledger down")):
    try:
        _gate._record_merge_authorization(
            [{"task_id": "COORD-49", "project": "switchboard"}],
            repo="r", number=1, sha=HEAD, context=MERGE_AUTHORIZATION_CONTEXT,
            state="failure", reason="draft_pr", description="d", blocked=[],
        )
        _boom.append("survived")
    except Exception:  # noqa: BLE001 - the point is that this must not happen
        pass
ok(_boom == ["survived"],
   "a failed ledger write never takes down the CI gate")

print("\nCOORD-49 merge-authorization routing: %d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
