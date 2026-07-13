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
    return {"ok": True}


orig = cvd._github_request
cvd._github_request = _fake_request

os.environ.pop("SWITCHBOARD_CI_PULL_MODEL", None)
ok(not cvd.is_pull_model_enabled(), "pull model off by default")
os.environ["SWITCHBOARD_CI_PULL_MODEL"] = "1"
ok(cvd.is_pull_model_enabled(), "pull model on when env set")

calls.clear()
res = cvd.dispatch_verify(384, head_sha="abc123", token="tok")
ok(res["dispatched"] and res["pr"] == 384 and res["head_sha"] == "abc123",
   "dispatch_verify returns structured payload")
ok(len(calls) == 1 and calls[0]["method"] == "POST"
   and calls[0]["path"].endswith("/projectplanner-ci/dispatches"),
   "dispatch posts repository_dispatch to projectplanner-ci")
payload = calls[0]["body"]
ok(payload["event_type"] == "verify-pr"
   and payload["client_payload"]["pr"] == 384
   and payload["client_payload"]["head_sha"] == "abc123",
   "dispatch carries pr + head_sha client_payload")

_TOKEN_ENV_KEYS = (
    "SWITCHBOARD_CI_DISPATCH_TOKEN",
    "SWITCHBOARD_CI_GITHUB_TOKEN",
    "PM_GITHUB_TOKEN",
    "GITHUB_TOKEN",
)
_saved_tokens = {k: os.environ.pop(k, None) for k in _TOKEN_ENV_KEYS}
try:
    cvd.dispatch_verify(1, token="")
    ok(False, "missing token should raise")
except RuntimeError as exc:
    ok("token is required" in str(exc).lower(), "missing token raises clearly")
finally:
    for key, value in _saved_tokens.items():
        if value is not None:
            os.environ[key] = value

def _boom(*a, **k):
    raise RuntimeError("GitHub API POST failed: HTTP 403 Forbidden")

cvd._github_request = _boom
try:
    cvd.dispatch_verify(2, token="bad")
    ok(False, "HTTP error should raise")
except RuntimeError as exc:
    ok("403" in str(exc), "HTTP failures surface status code")

cvd._github_request = orig

print(f"\nci_verify_dispatch: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
