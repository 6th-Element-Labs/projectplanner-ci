#!/usr/bin/env python3
"""Tests for ci_verify_dispatch pull-model relay."""
import json
import os
import sys
import tempfile
import urllib.error

_TMP = tempfile.mkdtemp(prefix="ci-verify-dispatch-")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ci_verify_dispatch as cvd  # noqa: E402

passed = failed = 0
VALID_SHA = "a" * 40


def ok(cond, msg):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    passed += 1 if cond else 0
    failed += 0 if cond else 1


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body=""):
        self.code = code
        self._body = body.encode("utf-8")

    def read(self):
        return self._body


calls = []


def _fake_request(method, path, *, token, body=None):
    calls.append({"method": method, "path": path, "token": token, "body": body})
    if method.upper() == "GET" and "/pulls/" in path:
        return {"head": {"sha": VALID_SHA}}
    if method.upper() == "GET" and "/commits/" in path:
        return {"sha": VALID_SHA}
    return {"ok": True}


orig = cvd._github_request
cvd._github_request = _fake_request

os.environ.pop("SWITCHBOARD_CI_PULL_MODEL", None)
ok(not cvd.is_pull_model_enabled(), "pull model off by default")
os.environ["SWITCHBOARD_CI_PULL_MODEL"] = "1"
ok(cvd.is_pull_model_enabled(), "pull model on when env set")

ok(cvd.normalize_commit_sha(VALID_SHA) == VALID_SHA, "normalize accepts valid sha")
try:
    cvd.normalize_commit_sha("mhead")
    ok(False, "normalize rejects test fixture sha mhead")
except cvd.CiVerifyDispatchError as exc:
    ok("40 lowercase hex" in str(exc), "normalize explains fixture rejection")

calls.clear()
res = cvd.dispatch_verify(384, head_sha=VALID_SHA, token="tok")
ok(res["dispatched"] and res["pr"] == 384 and res["head_sha"] == VALID_SHA,
   "dispatch_verify returns structured payload")
post_calls = [c for c in calls if c["method"] == "POST"]
ok(len(post_calls) == 1 and post_calls[0]["path"].endswith("/projectplanner-ci/dispatches"),
   "dispatch posts repository_dispatch to projectplanner-ci")
payload = post_calls[0]["body"]
ok(payload["event_type"] == "verify-pr"
   and payload["client_payload"]["pr"] == 384
   and payload["client_payload"]["head_sha"] == VALID_SHA,
   "dispatch carries pr + head_sha client_payload")

calls.clear()
dry = cvd.dispatch_verify(384, head_sha=VALID_SHA, token="tok", dry_run=True)
ok(dry["dispatched"] is False and dry["dry_run"] is True
   and len([c for c in calls if c["method"] == "POST"]) == 0,
   "dry_run validates without POST")

calls.clear()
resolved, source, stale = cvd.resolve_head_sha(99, "", repo=cvd.DEFAULT_CANONICAL_REPO, token="tok")
ok(resolved == VALID_SHA and source == "github_pr_api" and stale is None
   and len(calls) == 1, "blank webhook sha resolves from live PR API")

calls.clear()
resolved2, source2, stale2 = cvd.resolve_head_sha(
    99, "d" * 40, repo=cvd.DEFAULT_CANONICAL_REPO, token="tok")
ok(resolved2 == VALID_SHA and stale2 == "d" * 40,
   "stale webhook sha is ignored in favor of live PR head")

calls.clear()
cvd._github_request = _fake_request
cvd.verify_commit_exists(VALID_SHA, repo=cvd.DEFAULT_CANONICAL_REPO, token="tok")
ok(len(calls) == 1 and "/commits/" in calls[0]["path"],
   "verify_commit_exists GETs canonical commit before dispatch")

try:
    cvd.dispatch_verify(384, head_sha="abc123", token="tok", strict_explicit=True)
    ok(False, "short sha should raise when passed explicitly")
except cvd.CiVerifyDispatchError:
    ok(True, "short sha raises CiVerifyDispatchError when passed explicitly")

skip = cvd.try_dispatch_verify(384, head_sha="chead25", token="tok")
ok(skip["dispatched"] and skip["head_sha"] == VALID_SHA,
   "bogus webhook sha ignored; live PR head dispatched")

_TOKEN_ENV_KEYS = (
    "SWITCHBOARD_CI_DISPATCH_TOKEN",
    "SWITCHBOARD_CI_GITHUB_TOKEN",
    "PM_GITHUB_TOKEN",
    "GITHUB_TOKEN",
)
_saved_tokens = {k: os.environ.pop(k, None) for k in _TOKEN_ENV_KEYS}
try:
    cvd.dispatch_verify(1, head_sha=VALID_SHA, token="")
    ok(False, "missing token should raise")
except cvd.CiVerifyDispatchError as exc:
    ok("token is required" in str(exc).lower(), "missing token raises clearly")
finally:
    for key, value in _saved_tokens.items():
        if value is not None:
            os.environ[key] = value


def _boom(*a, **k):
    raise cvd.CiVerifyDispatchError("GitHub API POST failed: HTTP 403 Forbidden")


cvd._github_request = _boom
try:
    cvd.dispatch_verify(2, head_sha=VALID_SHA, token="bad")
    ok(False, "HTTP error should raise")
except cvd.CiVerifyDispatchError as exc:
    ok("403" in str(exc), "HTTP failures surface status code")

cvd._github_request = orig

print(f"\nci_verify_dispatch: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
