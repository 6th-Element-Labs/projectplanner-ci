"""HARDEN-74 follow-up — a bad event-driven request marker must not crash the drain.

A request marker for a PR that 404s (stale/deleted PR, or a transient GitHub error) used to
propagate out of the gate's --pr path and fail the whole drain run, starving every other
queued PR. This verifies the lookup is now skipped per-PR. Script-style test."""
import importlib.util
import os
import tempfile
from pathlib import Path

import ci_gate_requests as cr

ROOT = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location(
    "switchboard_pr_gate", ROOT / "scripts" / "switchboard_pr_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def ok(condition, message):
    if not condition:
        raise AssertionError(message)
    print("  PASS ", message)


_orig_request = gate._github_request


# ----- end-to-end: drain a marker for a PR that 404s -> exit 0, no crash ------------
def _boom_lookup(method, path, *, token, body=None):
    if method == "GET" and "/pulls/" in path:
        raise gate.GateError("GitHub API GET .../pulls/424242 failed: HTTP 404 Not Found")
    if method == "GET" and "/statuses" in path:
        return []
    return {"ok": True}


with tempfile.TemporaryDirectory(prefix="drain-res-") as d:
    os.environ["SWITCHBOARD_CI_REQUEST_DIR"] = d
    os.environ["SWITCHBOARD_CI_GITHUB_TOKEN"] = "tkn"
    cr.request_ci_gate(424242, repo="6th-Element-Labs/projectplanner", dir_override=d)
    gate._github_request = _boom_lookup
    try:
        rc = gate.main(["--drain-requests"])
        ok(rc == 0, "drain of a PR that 404s returns 0 (skips it; the batch does not crash)")
        ok(cr.list_requests(d) == [],
           "the bad marker was claimed/drained, not left to re-fire forever")
    finally:
        gate._github_request = _orig_request
        os.environ.pop("SWITCHBOARD_CI_REQUEST_DIR", None)
        os.environ.pop("SWITCHBOARD_CI_GITHUB_TOKEN", None)


# ----- unit: a mixed good/bad --pr set skips only the bad one -----------------------
class _Args:
    pr = [111, 999]


def _mixed(method, path, *, token, body=None):
    if method == "GET" and "/pulls/999" in path:
        raise gate.GateError("HTTP 404")
    if method == "GET" and "/pulls/111" in path:
        return {"number": 111, "head": {"sha": "s111"}}
    return []


gate._github_request = _mixed
try:
    got = list(gate._select_prs(_Args(), "o/r", "t"))
    ok([p["number"] for p in got] == [111],
       "_select_prs yields the good PR and skips the one that 404s")
    claim_targets = list(gate._claim_gate_targets(_Args(), "o/r", "t"))
    ok([pr["number"] for _repo, pr, _mode in claim_targets] == [111],
       "_claim_gate_targets skips the 404 PR too (no aborted claim pass)")
finally:
    gate._github_request = _orig_request


print("\nAll drain-resilience tests passed.")
