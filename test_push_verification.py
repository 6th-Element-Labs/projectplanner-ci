#!/usr/bin/env python3
"""Unit tests for push_verification.verify_push_evidence (offline, injected GitHub)."""
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import push_verification as pv  # noqa: E402

REPO = "6th-Element-Labs/projectplanner"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if condition:
        passed += 1
    else:
        failed += 1


def req_status(status, capture=None):
    def _fn(url, token=""):
        if capture is not None:
            capture.append(url)
        return status, {}
    return _fn


def req_http_error(code):
    def _fn(url, token=""):
        raise urllib.error.HTTPError(url, code, "err", {}, None)
    return _fn


def req_url_error(url, token=""):
    raise urllib.error.URLError("connection refused")


# present: commit found on remote
seen = []
r = pv.verify_push_evidence({"head_sha": "abc123", "branch": "codex/X-1"}, REPO,
                            request_fn=req_status(200, seen))
ok(r["status"] == pv.PRESENT, "commit 200 -> present")
ok(seen and seen[0].endswith("/commits/abc123"), "verifies the exact commit sha")

# absent: commit not on remote -> fail closed
r = pv.verify_push_evidence({"head_sha": "deadbeef", "branch": "codex/X-1"}, REPO,
                            request_fn=req_http_error(404))
ok(r["status"] == pv.ABSENT, "commit 404 -> absent (fail closed)")
ok(r["failure_class"] if False else r.get("reason") == "commit_not_on_remote",
   "absent reason names the missing commit")

# absent via 422 (unknown object)
r = pv.verify_push_evidence({"head_sha": "nope"}, REPO, request_fn=req_http_error(422))
ok(r["status"] == pv.ABSENT, "commit 422 -> absent")

# branch-only path checks the branch ref
seen = []
r = pv.verify_push_evidence({"branch": "codex/X-2"}, REPO,
                            request_fn=req_status(200, seen))
ok(r["status"] == pv.PRESENT and seen[0].endswith("/branches/codex%2FX-2"),
   "branch-only -> checks (url-encoded) branch ref")

r = pv.verify_push_evidence({"branch": "codex/gone"}, REPO, request_fn=req_http_error(404))
ok(r["status"] == pv.ABSENT and r.get("reason") == "branch_not_on_remote",
   "missing branch 404 -> absent")

# unreachable remote -> unverified (allowed, warned)
r = pv.verify_push_evidence({"head_sha": "abc"}, REPO, request_fn=req_url_error)
ok(r["status"] == pv.UNVERIFIED and r.get("reason") == "remote_unreachable",
   "URLError -> unverified (never fail closed on transport)")

# auth/rate limit -> unverified (cannot prove absence)
r = pv.verify_push_evidence({"head_sha": "abc"}, REPO, request_fn=req_http_error(403))
ok(r["status"] == pv.UNVERIFIED, "403 -> unverified, not absent")

# no repo configured -> unverified, no network attempted
called = []
r = pv.verify_push_evidence({"head_sha": "abc"}, "", request_fn=req_status(200, called))
ok(r["status"] == pv.UNVERIFIED and not called, "no canonical repo -> unverified, no call")

# already merged -> skipped
r = pv.verify_push_evidence({"head_sha": "abc", "merged_sha": "def"}, REPO,
                            request_fn=req_status(200))
ok(r["status"] == pv.SKIPPED and r.get("reason") == "already_merged", "merged -> skipped")

# no git evidence (docs/offline) -> skipped
r = pv.verify_push_evidence({"artifact_or_review_note": "x"}, REPO,
                            request_fn=req_status(200))
ok(r["status"] == pv.SKIPPED and r.get("reason") == "no_git_evidence",
   "no git evidence -> skipped (docs work unaffected)")

# unparseable remote_ref but a real PR exists -> trust PR
r = pv.verify_push_evidence({"remote_ref": "refs/tags/v1", "pr_url": "http://x/pr/9"}, REPO,
                            request_fn=req_status(200))
ok(r["status"] == pv.PRESENT and r.get("reason") == "pr_evidence", "PR evidence -> present")

# remote_ref of form refs/heads/<b> resolves to a branch check
seen = []
r = pv.verify_push_evidence({"remote_ref": "refs/heads/codex/X-3"}, REPO,
                            request_fn=req_status(404, seen))
ok(r["status"] == pv.ABSENT and "/branches/codex%2FX-3" in seen[0],
   "refs/heads/<b> remote_ref -> branch check, 404 absent")

# token precedence
os.environ.pop("PM_GITHUB_TOKEN", None)
os.environ.pop("GITHUB_TOKEN", None)
os.environ["SWITCHBOARD_CI_GITHUB_TOKEN"] = "tok-ci"
ok(pv.github_token_from_env() == "tok-ci", "falls back to SWITCHBOARD_CI_GITHUB_TOKEN")
os.environ["PM_GITHUB_TOKEN"] = "tok-pm"
ok(pv.github_token_from_env() == "tok-pm", "PM_GITHUB_TOKEN wins")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
