"""Legacy classify_completion projection now mirrors the Mission Bot."""
from __future__ import annotations

from copy import deepcopy

from path_setup import ROOT, SRC  # noqa: F401

from switchboard.domain.completion import (
    COMPLETION_DECISION_SCHEMA,
    COMPLETION_SNAPSHOT_SCHEMA,
    build_completion_snapshot,
    classify_completion,
)


HEAD = "a" * 40


def snapshot(
    *,
    draft: bool = False,
    merge_state: str = "CLEAN",
    ci: str = "SUCCESS",
    attribution: str = "product",
    review: str = "passed",
    review_head: str = HEAD,
    findings=(),
    runner=None,
):
    return build_completion_snapshot(
        task={"task_id": "SIMPLIFY-23", "status": "In Review",
              "git_state": {"head_sha": HEAD}},
        github_pr={
            "number": 810, "state": "OPEN", "draft": draft, "mergeable": True,
            "mergeStateStatus": merge_state, "head": {"sha": HEAD},
            "status_contexts": [{
                "context": "Switchboard CI / VM gate", "state": ci,
                "failure_attribution": attribution,
                "target_url": "https://github.com/example/x/actions/runs/1",
                "description": "ci",
                "run_attempt": 1,
            }],
        },
        required_status_contexts=["Switchboard CI / VM gate"],
        review={"status": review, "head_sha": review_head, "number": 810},
        merge_gate={"findings": list(findings)},
        runner=runner or {},
    )


def test_snapshot_is_shared_exact_head_contract_and_does_not_mutate_sources():
    pr = {"number": 1, "state": "OPEN", "head": {"sha": HEAD},
          "checks": {"required": "SUCCESS"}}
    original = deepcopy(pr)
    result = build_completion_snapshot(
        task={"task_id": "x", "git_state": {"head_sha": HEAD}},
        github_pr=pr,
        required_status_contexts=["required"],
    )
    assert result["schema"] == COMPLETION_SNAPSHOT_SCHEMA
    assert result["head_sha"] == HEAD
    assert result["status_contexts"]["required"]["state"] == "SUCCESS"
    assert pr == original


def test_same_snapshot_always_produces_same_decision():
    value = snapshot()
    first = classify_completion({"attempt": 2}, value)
    second = classify_completion({"attempt": 2}, deepcopy(value))
    assert first == second
    assert first["schema"] == COMPLETION_DECISION_SCHEMA


def test_live_runner_waits_even_when_ci_red():
    result = classify_completion(None, snapshot(
        draft=True, ci="FAILURE", attribution="product",
        runner={"live": True, "role": "review_merge", "head_sha": HEAD},
    ))
    assert result["route"] == "wait"
    assert result["mission_output"] == "WAIT"


def test_authority_ci_failure_boots_remediation():
    result = classify_completion(
        None,
        snapshot(draft=True, ci="ACTION_REQUIRED", attribution="authority"),
    )
    assert result["route"] == "remediation"
    assert result["desired_role"] == "remediation"
    assert result["failing_check_url"]


def test_green_draft_marks_ready():
    value = snapshot(
        draft=True,
        findings=[{"code": "draft_pr", "failure_class": "failed_gate",
                   "blocking": True}],
    )
    result = classify_completion(None, value)
    assert result["route"] == "review_merge"
    assert result["reason_code"] == "draft_ready_to_mark_ready"
    assert result["mission_output"] == "MARK_READY"


def test_draft_outranks_pending_ci():
    result = classify_completion(None, snapshot(draft=True, ci="IN_PROGRESS"))
    assert result["reason_code"] == "draft_ready_to_mark_ready"
    assert result["mission_output"] == "MARK_READY"


def test_all_red_ci_boots_remediation():
    product = classify_completion(None, snapshot(ci="ERROR", attribution="product"))
    infra = classify_completion(None, snapshot(ci="ERROR", attribution="infrastructure"))
    unknown = classify_completion(None, snapshot(ci="ERROR", attribution="unknown"))
    assert product["route"] == infra["route"] == unknown["route"] == "remediation"
    assert product["failing_check_url"]


def test_pending_waits_cancelled_remediates():
    pending = classify_completion(None, snapshot(ci="IN_PROGRESS"))
    cancelled = classify_completion(None, snapshot(ci="CANCELLED"))
    assert pending["route"] == "wait"
    assert cancelled["route"] == "remediation"


def test_missing_and_stale_review_route_review_merge():
    missing = classify_completion(None, snapshot(review=""))
    stale = classify_completion(None, snapshot(review_head="b" * 40))
    assert missing["reason_code"] == "review_required"
    assert stale["reason_code"] == "review_verdict_stale"
    assert missing["desired_role"] == stale["desired_role"] == "review_merge"


def test_review_findings_always_remediate():
    automatic = snapshot(review="changes_requested")
    automatic["review"]["findings"] = [{"finding_class": "automatic"}]
    judgment = snapshot(review="changes_requested")
    judgment["review"]["findings"] = [{"finding_class": "judgment"}]
    assert classify_completion(None, automatic)["route"] == "remediation"
    assert classify_completion(None, judgment)["route"] == "remediation"
    assert classify_completion(None, judgment)["mission_output"] == "START_REMEDIATION"


def test_merge_states():
    conflict = classify_completion(None, snapshot(merge_state="DIRTY"))
    behind = classify_completion(None, snapshot(merge_state="BEHIND"))
    unknown = classify_completion(None, snapshot(merge_state="UNKNOWN"))
    blocked_green = classify_completion(None, snapshot(merge_state="BLOCKED"))
    unstable_red = classify_completion(
        None, snapshot(merge_state="UNSTABLE", ci="FAILURE"))
    assert conflict["route"] == "remediation"
    assert behind["route"] == "remediation"
    assert unknown["mission_output"] in {"WAIT", "ARM_MERGE", "START_REVIEW"}
    assert blocked_green["reason_code"] == "exact_head_gates_passed"
    assert unstable_red["route"] == "remediation"


def test_merge_gate_coded_findings_remediate_or_review():
    review = classify_completion(None, snapshot(
        findings=[{"code": "review_required", "failure_class": "failed_gate",
                   "blocking": True}]))
    policy = classify_completion(None, snapshot(
        findings=[{"code": "wrong_target_branch", "failure_class": "failed_gate",
                   "blocking": True}]))
    # review_required finding is still a factory finding → remediation unless
    # review_needed path wins; Mission Bot treats coded findings as remediation.
    assert policy["route"] == "remediation"
    assert review["route"] in {"remediation", "review_merge"}


def test_merge_queue_and_provenance_precedence():
    queued = snapshot()
    queued["merge_queue"] = {"state": "AWAITING_CHECKS"}
    merged = snapshot()
    merged["github_pr"]["state"] = "MERGED"
    assert classify_completion(None, queued)["route"] == "wait"
    assert classify_completion(None, merged)["route"] == "reconcile"


def test_board_projection_blocked_for_remediation():
    remediation = classify_completion(
        None, snapshot(ci="FAILURE", attribution="product"))
    infra = classify_completion(
        None, snapshot(ci="FAILURE", attribution="infrastructure"))
    assert remediation["board_projection"] == "Blocked"
    assert infra["board_projection"] == "Blocked"


if __name__ == "__main__":
    _passed = _failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            _passed += 1
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            _failed += 1
            print(f"FAIL  {name}: {exc}")
    print(f"\nSIMPLIFY-23 Mission Bot projection: {_passed} passed, {_failed} failed")
    raise SystemExit(1 if _failed else 0)
