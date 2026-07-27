#!/usr/bin/env python3
"""Canonical coordinate helpers for the one trusted scratchpad route."""
import os
import sys
import tempfile
import urllib.error

_TMP = tempfile.mkdtemp(prefix="ci-verify-coordinates-")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ci_verify_dispatch as cvd  # noqa: E402

passed = failed = 0
VALID_SHA = "a" * 40
BASE_SHA = "b" * 40
MERGE_BASE_SHA = "c" * 40


def ok(cond, msg):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    passed += int(bool(cond))
    failed += int(not cond)


calls = []


def _fake_request(method, path, *, token, body=None):
    calls.append({"method": method, "path": path, "token": token, "body": body})
    if method.upper() == "GET" and "/pulls/" in path:
        return {"head": {"sha": VALID_SHA}, "base": {"sha": BASE_SHA}}
    if method.upper() == "GET" and "/compare/" in path:
        return {"merge_base_commit": {"sha": MERGE_BASE_SHA}}
    if method.upper() == "GET" and "/commits/" in path:
        return {"sha": VALID_SHA}
    raise AssertionError(f"unexpected request: {method} {path}")


orig = cvd._github_request
cvd._github_request = _fake_request


class _JsonResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"ok":true}'


orig_urlopen = cvd.urllib.request.urlopen
cvd.urllib.request.urlopen = lambda *_args, **_kwargs: _JsonResponse()
ok(orig("POST", "https://api.github.test/proof", token="tok", body={"x": 1})
   == {"ok": True},
   "real GitHub request helper encodes and decodes JSON")
cvd.urllib.request.urlopen = orig_urlopen

ok(cvd.normalize_commit_sha(VALID_SHA) == VALID_SHA, "normalize accepts a full SHA")
try:
    cvd.normalize_commit_sha("mhead")
    ok(False, "normalize rejects fixture-like SHAs")
except cvd.CiVerifyDispatchError as exc:
    ok("40 lowercase hex" in str(exc), "normalization failure explains the exact contract")

calls.clear()
resolved, source, stale = cvd.resolve_head_sha(
    99, "", repo=cvd.DEFAULT_CANONICAL_REPO, token="tok")
ok(resolved == VALID_SHA and source == "github_pr_api" and stale is None,
   "blank webhook SHA resolves from the live PR API")

calls.clear()
resolved2, _source2, stale2 = cvd.resolve_head_sha(
    99, "d" * 40, repo=cvd.DEFAULT_CANONICAL_REPO, token="tok")
ok(resolved2 == VALID_SHA and stale2 == "d" * 40,
   "a stale webhook SHA is fenced by the live PR head")

calls.clear()
merge_base = cvd.fetch_pr_merge_base_sha(
    99, VALID_SHA, repo=cvd.DEFAULT_CANONICAL_REPO, token="tok")
ok(merge_base == MERGE_BASE_SHA
   and any("/compare/" in call["path"] for call in calls),
   "merge-base resolution yields a public-history object for impacted-test selection")

calls.clear()
cvd.verify_commit_exists(VALID_SHA, repo=cvd.DEFAULT_CANONICAL_REPO, token="tok")
ok(len(calls) == 1 and "/commits/" in calls[0]["path"],
   "exact commit existence is checked before mirroring")

try:
    cvd.resolve_head_sha(
        99, "abc123", repo=cvd.DEFAULT_CANONICAL_REPO,
        token="tok", strict_explicit=True)
    ok(False, "strict explicit short SHA should raise")
except cvd.CiVerifyDispatchError:
    ok(True, "strict explicit short SHA fails closed")

_TOKEN_ENV_KEYS = (
    "SWITCHBOARD_CI_DISPATCH_TOKEN",
    "SWITCHBOARD_CI_GITHUB_TOKEN",
    "PM_GITHUB_TOKEN",
    "GITHUB_TOKEN",
)
saved_tokens = {key: os.environ.pop(key, None) for key in _TOKEN_ENV_KEYS}
try:
    cvd.fetch_pr_head_sha(1, token="")
    ok(False, "missing token should raise")
except cvd.CiVerifyDispatchError as exc:
    ok("token is required" in str(exc).lower(), "missing token is explicit")
finally:
    for key, value in saved_tokens.items():
        if value is not None:
            os.environ[key] = value

cvd._github_request = orig

print(f"\nci_verify_coordinates: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
