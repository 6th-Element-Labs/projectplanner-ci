#!/usr/bin/env python3
"""Claim-gate tests for scripts/switchboard_pr_gate.py (CI-7 claim-only runner)."""
import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location(
    "switchboard_pr_gate", ROOT / "scripts" / "switchboard_pr_gate.py"
)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def ok(condition, message):
    if not condition:
        raise AssertionError(message)
    print("  PASS ", message)


calls = []


def fake_request(method, path, *, token, body=None):
    calls.append({"method": method, "path": path, "token": token, "body": body})
    if method == "GET" and "/statuses" in path:
        return []
    return {"ok": True}


original_request = gate._github_request
try:
    gate._github_request = fake_request
    gate.post_status(
        "6th-Element-Labs/projectplanner",
        "abc123",
        "success",
        context="Switchboard / claim gate",
        description="Backed by CI-7",
        target_url="https://github.com/6th-Element-Labs/projectplanner/pull/18",
        token="token-value",
    )
finally:
    gate._github_request = original_request

posts = [c for c in calls if c["method"] == "POST"]
ok(len(posts) == 1, "post_status issues exactly one POST when no prior status exists")
call = posts[0]
body = call["body"]
ok(call["method"] == "POST", "commit status uses POST")
ok(call["path"] == "repos/6th-Element-Labs/projectplanner/statuses/abc123",
   "commit status targets the PR head SHA")
ok(call["token"] == "token-value", "commit status passes the configured token")
ok(body["state"] == "success", "commit status preserves the success state")
ok(body["context"] == "Switchboard / claim gate",
   "commit status uses the documented claim-gate context")
ok(len(body["description"]) <= 140, "commit status description is GitHub-safe")
ok(body["target_url"].endswith("/pull/18"), "commit status links back to the PR")

try:
    gate.post_status(
        "6th-Element-Labs/projectplanner",
        "abc123",
        "pending",
        context="Switchboard / claim gate",
        description="running",
        token="",
    )
except gate.GateError:
    print("  PASS  missing token fails closed")
else:
    raise AssertionError("missing token should fail closed")

# Idempotency: post_status must NOT re-POST when the latest status already matches.
def _idem_request(rows):
    def _req(method, path, *, token, body=None):
        _req.calls.append((method, path, body))
        if method == "GET" and "/statuses" in path:
            return rows
        return {"ok": True}
    _req.calls = []
    return _req


_orig_req = gate._github_request
try:
    same = _idem_request([{"context": "Switchboard / claim gate", "state": "success",
                           "description": "Backed by HARDEN-67"}])
    gate._github_request = same
    res = gate.post_status("r", "sha1", "success", context="Switchboard / claim gate",
                           description="Backed by HARDEN-67", token="t")
    ok(res.get("skipped") == "unchanged", "post_status skips an unchanged re-post (422-cap guard)")
    ok(not any(m == "POST" for m, _p, _b in same.calls),
       "no POST is issued when the status is unchanged")

    changed = _idem_request([{"context": "Switchboard / claim gate", "state": "success",
                              "description": "Backed by HARDEN-67"}])
    gate._github_request = changed
    gate.post_status("r", "sha1", "success", context="Switchboard / claim gate",
                     description="Backed by HARDEN-99 (newly claimed)", token="t")
    ok(any(m == "POST" for m, _p, _b in changed.calls),
       "post_status still POSTs when the verdict/description actually changes")
finally:
    gate._github_request = _orig_req

# A mixed good/bad --pr set skips only the bad one (404 must not abort the pass).
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
       "_claim_gate_targets skips a PR that 404s (no aborted claim pass)")
finally:
    gate._github_request = _orig_req

# main() returns 2 when no token is configured.
_saved = os.environ.get("PM_GITHUB_TOKEN"), os.environ.get("GITHUB_TOKEN"), os.environ.get(
    "SWITCHBOARD_CI_GITHUB_TOKEN")
for key in ("PM_GITHUB_TOKEN", "GITHUB_TOKEN", "SWITCHBOARD_CI_GITHUB_TOKEN"):
    os.environ.pop(key, None)
try:
    ok(gate.main([]) == 2, "main fails closed when no GitHub token is configured")
finally:
    for key, val in zip(("PM_GITHUB_TOKEN", "GITHUB_TOKEN", "SWITCHBOARD_CI_GITHUB_TOKEN"), _saved):
        if val is not None:
            os.environ[key] = val

print("\n12 passed, 0 failed")
