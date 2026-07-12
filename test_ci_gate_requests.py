"""HARDEN-74 — event-driven CI gate requests.

Script-style test (run directly: ``python test_ci_gate_requests.py``; exits nonzero on
failure). Covers the request-marker module, the github_sync webhook hook, and the gate's
--drain-requests entry."""
import importlib.util
import os
import tempfile
from pathlib import Path

import ci_gate_requests as cr

ROOT = Path(__file__).parent


def ok(condition, message):
    if not condition:
        raise AssertionError(message)
    print("  PASS ", message)


# ----- request / list / drain round-trip -------------------------------------------
with tempfile.TemporaryDirectory(prefix="ci-req-") as d:
    m = cr.request_ci_gate(42, repo="o/r", head_sha="abc123", dir_override=d)
    ok(m["pr_number"] == 42 and m["repo"] == "o/r" and m["head_sha"] == "abc123",
       "request_ci_gate returns the marker payload")
    ok((Path(d) / "pr-42.json").exists(), "request_ci_gate writes pr-<n>.json")
    ok([r["pr_number"] for r in cr.list_requests(d)] == [42],
       "list_requests reads the pending marker without clearing it")
    ok((Path(d) / "pr-42.json").exists(), "list_requests does not clear the marker")

    # A second request for the same PR overwrites (latest head_sha wins), not duplicates.
    cr.request_ci_gate(42, repo="o/r", head_sha="def456", dir_override=d)
    reqs = cr.list_requests(d)
    ok(len(reqs) == 1 and reqs[0]["head_sha"] == "def456",
       "re-requesting a PR overwrites the marker with the latest head sha")

    cr.request_ci_gate(7, repo="o/r", head_sha="s7", dir_override=d)
    drained = cr.drain(d)
    ok(sorted(x["pr_number"] for x in drained) == [7, 42],
       "drain returns all pending requests")
    ok(cr.list_requests(d) == [], "drain clears every marker it claimed")
    ok(cr.drain(d) == [], "draining an empty dir is a no-op")

# no dir yet -> empty, no crash
with tempfile.TemporaryDirectory(prefix="ci-req-") as d:
    missing = str(Path(d) / "does-not-exist")
    ok(cr.list_requests(missing) == [] and cr.drain(missing) == [],
       "list/drain on a missing dir return empty")

# malformed marker is skipped (and drained away), not fatal
with tempfile.TemporaryDirectory(prefix="ci-req-") as d:
    (Path(d) / "pr-9.json").write_text("{ not json", encoding="utf-8")
    cr.request_ci_gate(10, dir_override=d)
    drained = cr.drain(d)
    ok([x["pr_number"] for x in drained] == [10], "drain skips a malformed marker")
    ok(cr.list_requests(d) == [], "drain removes the malformed marker too")


# ----- feature flag ----------------------------------------------------------------
for val, expected in (("1", True), ("true", True), ("on", True),
                      ("0", False), ("", False)):
    old = os.environ.get("SWITCHBOARD_CI_EVENT_DRIVEN")
    if val == "":
        os.environ.pop("SWITCHBOARD_CI_EVENT_DRIVEN", None)
    else:
        os.environ["SWITCHBOARD_CI_EVENT_DRIVEN"] = val
    got = cr.is_event_driven_enabled()
    if old is None:
        os.environ.pop("SWITCHBOARD_CI_EVENT_DRIVEN", None)
    else:
        os.environ["SWITCHBOARD_CI_EVENT_DRIVEN"] = old
    ok(got is expected, f"event-driven flag {val!r} -> {expected}")


# ----- github_sync webhook hook ----------------------------------------------------
import github_sync  # noqa: E402

with tempfile.TemporaryDirectory(prefix="ci-req-gs-") as d:
    os.environ["SWITCHBOARD_CI_REQUEST_DIR"] = d
    try:
        os.environ.pop("SWITCHBOARD_CI_EVENT_DRIVEN", None)
        ok(github_sync._maybe_request_ci_gate("o/r", 55, "sha55") is False,
           "webhook hook is a no-op when the flag is off (no marker, no wait-removal)")
        ok(cr.list_requests(d) == [], "flag off writes no marker")

        os.environ["SWITCHBOARD_CI_EVENT_DRIVEN"] = "1"
        ok(github_sync._maybe_request_ci_gate("o/r", 55, "sha55") is True,
           "webhook hook enqueues a CI request when the flag is on")
        ok([r["pr_number"] for r in cr.list_requests(d)] == [55],
           "webhook hook writes the PR's request marker")
        # A None pr_number must never raise from inside webhook processing.
        ok(github_sync._maybe_request_ci_gate("o/r", None, "") is False,
           "webhook hook tolerates a missing PR number")
    finally:
        os.environ.pop("SWITCHBOARD_CI_EVENT_DRIVEN", None)
        os.environ.pop("SWITCHBOARD_CI_REQUEST_DIR", None)


# ----- gate --drain-requests entry (empty -> fast clean exit, no GitHub calls) ------
SPEC = importlib.util.spec_from_file_location(
    "switchboard_pr_gate", ROOT / "scripts" / "switchboard_pr_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

with tempfile.TemporaryDirectory(prefix="ci-req-drain-") as d:
    os.environ["SWITCHBOARD_CI_REQUEST_DIR"] = d
    os.environ["SWITCHBOARD_CI_GITHUB_TOKEN"] = "token-x"
    try:
        rc = gate.main(["--drain-requests"])
        ok(rc == 0, "gate --drain-requests exits 0 when nothing is pending")
    finally:
        os.environ.pop("SWITCHBOARD_CI_REQUEST_DIR", None)
        os.environ.pop("SWITCHBOARD_CI_GITHUB_TOKEN", None)


print("\nAll ci_gate_requests tests passed.")
