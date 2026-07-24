"""BUG-172: generated proofs for completion decision authority.

These are exhaustive small models, not seeded examples.  They prove that
presentation ordering cannot choose the route and that a pass verdict is not
merge authority unless it is bound to the current PR head.
"""
from __future__ import annotations

from itertools import permutations, product

from path_setup import ROOT, SRC  # noqa: F401

from switchboard.domain.completion.state_machine import classify_completion


HEAD = "a" * 40
OTHER_HEAD = "b" * 40
PR_URL = "https://github.com/example/projectplanner/pull/825"
REPLACEMENT_PR_URL = "https://github.com/example/projectplanner/pull/826"

FINDINGS = (
    {"code": "semantic_completion_failed", "blocking": True,
     "finding_class": "automatic"},
    {"code": "canonical_repo_missing", "blocking": True,
     "finding_class": "human"},
    {"code": "review_required", "blocking": True},
    {"code": "missing_required_status_contexts", "blocking": True},
    {"code": "draft_pr", "blocking": True},
    {"code": "noise", "blocking": False},
)

CI_ROWS = {
    "pass": {"state": "success"},
    "pending": {"state": "pending"},
    "cancelled": {"state": "cancelled", "failure_attribution": "infrastructure"},
    "infrastructure": {"state": "failure",
                       "failure_attribution": "infrastructure"},
    "authority": {"state": "action_required",
                  "failure_attribution": "authority"},
    "product": {"state": "failure", "failure_attribution": "product"},
}


def _snapshot() -> dict:
    return {
        "task_id": "COORD-46",
        "board_status": "In Review",
        "board_head_sha": HEAD,
        "pr_number": 825,
        "pr_url": PR_URL,
        "pr_identity": "github.com/example/projectplanner/825",
        "head_sha": HEAD,
        "github_pr": {
            "number": 825,
            "url": PR_URL,
            "state": "open",
            "head_sha": HEAD,
            "draft": False,
            "mergeable": True,
            "mergeStateStatus": "clean",
        },
        "required_status_contexts": [],
        "status_contexts": {},
        "review": {
            "status": "pass",
            "head_sha": HEAD,
            "pr_url": PR_URL,
            "findings": [],
        },
        "findings": [],
        "merge_queue": {},
        "runner": {},
        "merge_provenance": {},
    }


def _finding_oracle(rows: tuple[dict, ...]) -> tuple[str, str]:
    codes = {row["code"] for row in rows}
    if "semantic_completion_failed" in codes:
        return "remediation", "semantic_completion_failed"
    if "canonical_repo_missing" in codes:
        return "human", "canonical_repo_missing"
    if "review_required" in codes:
        return "review_merge", "review_required"
    if "missing_required_status_contexts" in codes:
        return "coordination_retry", "missing_required_status_contexts"
    return "review_merge", "exact_head_gates_passed"


def _ci_oracle(tokens: tuple[str, ...]) -> str:
    if "product" in tokens:
        return "remediation"
    if "authority" in tokens:
        return "human"
    if {"cancelled", "infrastructure", "missing"} & set(tokens):
        return "coordination_retry"
    if "pending" in tokens:
        return "wait"
    return "review_merge"


def test_all_1957_ordered_finding_sets_follow_one_explicit_precedence():
    checked = 0
    for length in range(len(FINDINGS) + 1):
        for ordered_rows in permutations(FINDINGS, length):
            snapshot = _snapshot()
            snapshot["findings"] = list(ordered_rows)
            decision = classify_completion(None, snapshot)
            expected_route, expected_reason = _finding_oracle(ordered_rows)
            assert (decision["route"], decision["reason_code"]) == (
                expected_route,
                expected_reason,
            )
            checked += 1
    assert checked == 1_957


def test_all_57624_required_ci_histories_ignore_context_presentation_order():
    names = ("a", "b", "c", "d")
    checked = 0
    for tokens in product((*CI_ROWS, "missing"), repeat=len(names)):
        snapshot = _snapshot()
        snapshot["status_contexts"] = {
            name: CI_ROWS[token]
            for name, token in zip(names, tokens)
            if token != "missing"
        }
        expected_route = _ci_oracle(tokens)
        baseline = None
        for required_order in permutations(names):
            snapshot["required_status_contexts"] = list(required_order)
            decision = classify_completion(None, snapshot)
            assert decision["route"] == expected_route
            if baseline is None:
                baseline = decision
            else:
                assert decision == baseline
            checked += 1
    assert checked == 57_624


def test_pass_verdict_requires_an_explicit_exact_current_head_binding():
    for status in ("pass", "passed", "approved", "success"):
        for invalid_head in (None, "", "   ", OTHER_HEAD):
            snapshot = _snapshot()
            snapshot["review"] = {
                "status": status,
                "head_sha": invalid_head,
                "pr_url": PR_URL,
            }
            decision = classify_completion(None, snapshot)
            assert decision["route"] == "review_merge"
            assert decision["reason_code"] == "review_verdict_stale"
            assert decision["effect"] != "enqueue"

        snapshot = _snapshot()
        snapshot["review"] = {
            "status": status,
            "head_sha": HEAD,
            "pr_url": PR_URL,
        }
        decision = classify_completion(None, snapshot)
        assert decision["reason_code"] == "exact_head_gates_passed"
        assert decision["effect"] == "enqueue"


def test_same_head_replacement_pr_never_inherits_old_review_authority():
    for status, expected_route in (
        ("pass", "review_merge"),
        ("changes_requested", "review_merge"),
    ):
        snapshot = _snapshot()
        snapshot["pr_number"] = 826
        snapshot["pr_url"] = REPLACEMENT_PR_URL
        snapshot["pr_identity"] = "github.com/example/projectplanner/826"
        snapshot["github_pr"] = {
            **snapshot["github_pr"],
            "number": 826,
            "url": REPLACEMENT_PR_URL,
        }
        snapshot["review"] = {
            "status": status,
            "head_sha": HEAD,
            "pr_url": PR_URL,
            "findings": [{
                "id": "old-pr-finding",
                "finding_class": "automatic",
            }],
        }
        decision = classify_completion(None, snapshot)
        assert decision["route"] == expected_route
        assert decision["effect"] != "enqueue"
        assert decision["reason_code"] in {
            "review_verdict_stale", "review_required",
        }


if __name__ == "__main__":
    # Switchboard CI executes each test file directly, not through pytest.
    # Keep the generated proof on that real path so a green file means the
    # 59,581 modeled classifier states were actually evaluated.
    test_all_1957_ordered_finding_sets_follow_one_explicit_precedence()
    test_all_57624_required_ci_histories_ignore_context_presentation_order()
    test_pass_verdict_requires_an_explicit_exact_current_head_binding()
    test_same_head_replacement_pr_never_inherits_old_review_authority()
    print("BUG-172 completion classifier model: 59,581 generated states passed")
