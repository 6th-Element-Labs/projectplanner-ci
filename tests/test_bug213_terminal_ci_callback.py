"""BUG-213: terminal public-CI evidence survives a callback outage."""

from path_setup import ROOT

from switchboard.application.commands.merge_gate import (
    _apply_terminal_external_ci_receipt,
)


WORKFLOW = ROOT / ".github" / "workflows" / "verify.yml"
SHA = "d1e25eff0580ebdacbbba0906f5e80265403716b"
CONTEXT = "Switchboard CI / VM gate"
RUN_URL = "https://github.com/6th-Element-Labs/projectplanner-ci/actions/runs/30391843470"


def test_terminal_callback_retries_identical_status_and_verifies_readback():
    source = WORKFLOW.read_text()
    assert "Post and verify terminal required status" in source
    assert "for attempt in 1 2 3 4 5" in source
    assert 'commits/$SOURCE_SHA/status' in source
    assert "select(.context == $context)][0]" in source
    assert ".state == $state" in source
    assert ".target_url == $target_url" in source
    assert "Terminal status callback did not verify after 5 bounded attempts" in source


def test_exact_head_failed_public_receipt_replaces_stale_pending_context():
    contexts = {CONTEXT: "pending"}
    receipt = _apply_terminal_external_ci_receipt(
        contexts,
        {
            "source_sha": SHA,
            "status_context": CONTEXT,
            "run_url": RUN_URL,
            "latest": {
                "source_sha": SHA,
                "status_context": CONTEXT,
                "status": "failure",
                "conclusion": "failure",
                "run_url": RUN_URL,
                "failure_class": "tests",
                "failure_reason": "test_bug91_out_of_order_attempts.py failed",
            },
        },
        head_sha=SHA,
        required_contexts=[CONTEXT],
    )
    assert contexts[CONTEXT] == "failure"
    assert receipt == {
        "schema": "switchboard.external_ci_terminal_receipt.v1",
        "source_sha": SHA,
        "context": CONTEXT,
        "state": "failure",
        "run_url": RUN_URL,
        "failure_class": "tests",
        "failure_reason": "test_bug91_out_of_order_attempts.py failed",
    }


def test_receipt_fallback_refuses_other_heads_and_nonterminal_runs():
    for source_sha, status in (("a" * 40, "failure"), (SHA, "running")):
        contexts = {CONTEXT: "pending"}
        receipt = _apply_terminal_external_ci_receipt(
            contexts,
            {
                "latest": {
                    "source_sha": source_sha,
                    "status_context": CONTEXT,
                    "status": status,
                    "conclusion": "failure" if status == "failure" else "",
                },
            },
            head_sha=SHA,
            required_contexts=[CONTEXT],
        )
        assert receipt is None
        assert contexts[CONTEXT] == "pending"


if __name__ == "__main__":
    test_terminal_callback_retries_identical_status_and_verifies_readback()
    test_exact_head_failed_public_receipt_replaces_stale_pending_context()
    test_receipt_fallback_refuses_other_heads_and_nonterminal_runs()
    print("BUG-213 terminal CI callback: 3 passed")
