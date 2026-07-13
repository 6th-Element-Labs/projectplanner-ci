"""Claim-gate resilience — a stale/deleted PR must not crash the claim pass."""
import importlib.util
import os
from pathlib import Path

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


def _boom_lookup(method, path, *, token, body=None):
    if method == "GET" and "/pulls/" in path:
        raise gate.GateError("GitHub API GET .../pulls/424242 failed: HTTP 404 Not Found")
    if method == "GET" and "/statuses" in path:
        return []
    return {"ok": True}


os.environ["SWITCHBOARD_CI_GITHUB_TOKEN"] = "tkn"
gate._github_request = _boom_lookup
try:
    rc = gate.main(["--pr", "424242"])
    ok(rc == 0, "claim pass for a PR that 404s returns 0 (skips it; batch does not crash)")
finally:
    gate._github_request = _orig_request
    os.environ.pop("SWITCHBOARD_CI_GITHUB_TOKEN", None)


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
    claim_targets = list(gate._claim_gate_targets(_Args(), "o/r", "t"))
    ok([pr["number"] for _repo, pr, _mode in claim_targets] == [111],
       "_claim_gate_targets skips the 404 PR too (no aborted claim pass)")
finally:
    gate._github_request = _orig_request


print("\nAll claim-gate resilience tests passed.")
