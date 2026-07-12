"""HARDEN-72 / Lever 7 — per-PR CI attribution.

Script-style test (run directly: ``python test_ci_attribution.py``; exits nonzero on
failure) matching the Switchboard suite convention. Covers the pure attribution
builders and the switchboard_pr_gate wiring seam that posts them."""
import importlib.util
import sys
from pathlib import Path

import ci_attribution as ca

ROOT = Path(__file__).parent


def ok(condition, message):
    if not condition:
        raise AssertionError(message)
    print("  PASS ", message)


# ----- parse_failing_tests: pytest short-summary shape -----------------------------
PYTEST_LOG = """\
=== short test summary info ===
FAILED test_merge_gate.py::test_blocks_uncovered - AssertionError: expected block
FAILED test_store.py::test_write_through - assert 1 == 2
ERROR test_boot.py::test_env - fixture 'db' not found
"""
tests = ca.parse_failing_tests(PYTEST_LOG)
ok(len(tests) == 3, "parses all pytest FAILED/ERROR summary lines")
ok(tests[0].nodeid == "test_merge_gate.py::test_blocks_uncovered",
   "keeps the full pytest nodeid")
ok(tests[0].file == "test_merge_gate.py", "derives file from nodeid")
ok("expected block" in tests[0].reason, "captures the failure reason")
ok(tests[2].nodeid.startswith("test_boot.py"), "treats ERROR lines as failing tests")


# ----- parse_failing_tests: script-suite shape (section header + traceback) ---------
SUITE_LOG = """\
== Python runtime ==
Python 3.12.1
== test_merge_gate.py ==
  PASS  some check
== test_ci_attribution.py ==
Traceback (most recent call last):
  File "test_ci_attribution.py", line 12, in <module>
AssertionError: attribution must link the run
"""
suite_tests = ca.parse_failing_tests(SUITE_LOG)
ok(len(suite_tests) == 1, "script-suite: attributes to exactly the failing section")
ok(suite_tests[0].file == "test_ci_attribution.py",
   "script-suite: the LAST section before the traceback is the culprit")
ok("AssertionError" in suite_tests[0].reason, "script-suite: quotes the error line")

# ignores the shell's own scaffolding sections when there is no test failure marker
CLEAN_LOG = "== Python runtime ==\nPython 3.12.1\n== test_x.py ==\n  PASS  ok\n"
ok(ca.parse_failing_tests(CLEAN_LOG) == [], "a clean log yields no failing tests")
ok(ca.parse_failing_tests("") == [], "empty log yields no failing tests")


# ----- extract_run_links -----------------------------------------------------------
MIRROR_LOG = (
    'external_ci_mirror source_sha=abc workflow=backend-tests.yml\n'
    '{\n  "status": "failure",\n  "run_url": "https://github.com/o/ci/actions/runs/42",\n'
    '  "logs_url": "https://github.com/o/ci/actions/runs/42/logs"\n}\n'
)
links = ca.extract_run_links(MIRROR_LOG)
ok(links.get("run_url") == "https://github.com/o/ci/actions/runs/42",
   "extracts the external run_url from the mirror log")
ok(links.get("logs_url", "").endswith("/logs"), "extracts the logs_url too")
ok(ca.extract_run_links('{"run_url": null}') == {}, "ignores null run_url")
# Line form (survives the gate's 4000-char JSON truncation).
ok(ca.extract_run_links("run_url=https://x/runs/7\nlogs_url=https://x/runs/7/logs\n").get(
    "run_url") == "https://x/runs/7", "extracts run_url from the plain line form")


# ----- HARDENING (adversarial-review findings) -------------------------------------
# #1: a PASSING test that prints a "FAILED <token>" line must NOT hijack attribution;
# the real culprit (the traceback'd section) must win.
HIJACK_LOG = """\
== test_alpha.py ==
  PASS  emits FAILED case_7 in its own output
FAILED case_7
== test_zeta.py ==
Traceback (most recent call last):
AssertionError: the real failure
"""
hj = ca.parse_failing_tests(HIJACK_LOG)
ok(len(hj) == 1 and hj[0].file == "test_zeta.py",
   "a bare 'FAILED case_7' from a passing test does not hijack; the real culprit wins")
ok(all("case_7" not in t.nodeid for t in hj), "the non-nodeid token is rejected")
ok(ca.parse_failing_tests("FAILED 0\nTraceback (most recent call last):\n== test_z.py ==\n"
                          "AssertionError: x\n")[0].file == "test_z.py",
   "a 'FAILED 0' summary cell is not treated as a failing test")

# #2: benign output containing an 'Error:'/'assert' substring but NO traceback -> no attribution.
BENIGN = "== test_beta.py ==\n  PASS  handles the Error: prefix and assert path\n"
ok(ca.parse_failing_tests(BENIGN) == [],
   "no traceback -> no false section attribution even with Error:/assert substrings")

# #3: _first_error_line quotes an exception line, not a loose 'assert' substring.
ok(ca._first_error_line("checking asserts: all good\nValueError: boom") == "ValueError: boom",
   "error-line quote picks the exception line, not a benign 'assert' substring")

# #4: ANSI colour codes don't defeat the pytest parser (external mirror logs are coloured).
ANSI_LOG = "\x1b[31mFAILED\x1b[0m \x1b[1mtest_x.py::test_y\x1b[0m - AssertionError: boom\n"
ansi = ca.parse_failing_tests(ANSI_LOG)
ok(ansi and ansi[0].nodeid == "test_x.py::test_y",
   "ANSI colour codes are stripped before parsing the pytest nodeid")


# ----- summarize_failures ----------------------------------------------------------
ok(ca.summarize_failures(tests) == "test_merge_gate.py::test_blocks_uncovered (+2 more)",
   "summary names the first test and counts the rest")
ok(ca.summarize_failures([]) == "", "empty summary for no failures")


# ----- build_failure_attribution: direct failing-test link -------------------------
# External red: target_url is the CI run, NOT the PR; description names the test.
ext = ca.build_failure_attribution(
    repo="o/r", sha="deadbeef", pr_number=7,
    pr_url="https://github.com/o/r/pull/7",
    run_url="https://github.com/o/ci/actions/runs/99",
    failure_class="workflow_failed", log_text=PYTEST_LOG)
ok(ext.target_url == "https://github.com/o/ci/actions/runs/99",
   "red status links the external CI run, not the PR")
ok(ext.target_url != ext.pr_number, "red target is a URL, not a PR number")
ok("test_merge_gate.py::test_blocks_uncovered" in ext.description,
   "red description names the failing test")
ok(ext.failure_class == "workflow_failed", "red attribution preserves the failure class")

# Local red (no external run): fall back to the commit checks page, still name the test.
local = ca.build_failure_attribution(
    repo="o/r", sha="deadbeef", pr_number=7,
    pr_url="https://github.com/o/r/pull/7", log_text=SUITE_LOG)
ok(local.target_url == "https://github.com/o/r/commit/deadbeef/checks",
   "local red links the commit checks page when there is no external run")
ok("test_ci_attribution.py" in local.description, "local red names the failing test file")
ok(local.run_url == "", "local red has no external run url")

# Infra red with no parseable tests: keep the reason, point at the commit checks page.
infra = ca.build_failure_attribution(
    repo="o/r", sha="deadbeef", pr_url="https://github.com/o/r/pull/7",
    log_text="dispatch error, no verdict", error_text="mirror sync failed: boom")
ok(infra.failing_tests == [], "infra red parses no failing tests")
ok("mirror sync failed" in infra.description, "infra red keeps the error reason visible")
ok(infra.target_url.endswith("/commit/deadbeef/checks"),
   "infra red still points at a checks link, not a bare PR")


# ----- build_success_attribution ---------------------------------------------------
succ = ca.build_success_attribution(
    repo="o/r", sha="c0ffee", pr_number=7, pr_url="https://github.com/o/r/pull/7",
    run_url="https://github.com/o/ci/actions/runs/100")
ok(succ.target_url == "https://github.com/o/ci/actions/runs/100",
   "green status also links the CI run (attributable pass or fail)")
ok(succ.state == "success", "success attribution has success state")
no_run = ca.build_success_attribution(repo="o/r", sha="c0ffee", pr_number=7,
                                      pr_url="https://github.com/o/r/pull/7")
ok(no_run.target_url == "https://github.com/o/r/pull/7",
   "green with no external run falls back to the PR link")


# ----- to_payload is JSON-serializable and carries the schema ----------------------
import json  # noqa: E402
payload = ext.to_payload()
ok(payload["schema"] == ca.SCHEMA, "payload carries the attribution schema")
ok(json.dumps(payload) and payload["failing_tests"][0]["nodeid"].startswith("test_merge_gate"),
   "payload serializes and includes failing tests")


# ----- record_attribution: best-effort + deduped -----------------------------------
import store  # noqa: E402


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        return self

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return _FakeCursor(self._rows)

    def __exit__(self, *a):
        return False


recorded = []
orig_conn = store._conn
orig_append = store.append_activity
try:
    store._conn = lambda project: _FakeConn([])  # no prior rows
    store.append_activity = lambda kind, actor, payload=None, task_id=None, project=None: (
        recorded.append({"kind": kind, "payload": payload, "task_id": task_id}) or 123)
    rid = ca.record_attribution(ext, project="switchboard", task_id="HARDEN-72")
    ok(rid == 123, "record_attribution appends a ci.attribution activity")
    ok(recorded and recorded[0]["kind"] == "ci.attribution", "activity kind is ci.attribution")
    ok(recorded[0]["task_id"] == "HARDEN-72", "activity is threaded to the claimed task")

    # Dedupe: an identical (pr, sha, state) already logged -> no second write.
    prior = {"payload": json.dumps({"pr_number": 7, "merge_group": "",
                                    "head_sha": "deadbeef", "state": "failure"})}
    store._conn = lambda project: _FakeConn([prior])
    recorded.clear()
    rid2 = ca.record_attribution(ext, project="switchboard", task_id="HARDEN-72")
    ok(rid2 is None and not recorded, "record_attribution dedupes an identical prior record")
finally:
    store._conn = orig_conn
    store.append_activity = orig_append


# ----- gate wiring seam: switchboard_pr_gate posts the attributed link -------------
SPEC = importlib.util.spec_from_file_location(
    "switchboard_pr_gate", ROOT / "scripts" / "switchboard_pr_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

fake_pr = {"number": 7, "head": {"sha": "deadbeef"},
           "html_url": "https://github.com/o/r/pull/7",
           "base": {"ref": "master", "sha": "base"}}
posts = []


def _install_gate_stubs():
    gate.latest_status = lambda *a, **k: None
    gate._ensure_cache_repo = lambda *a, **k: Path("/tmp")
    gate._prepare_worktree = lambda *a, **k: Path("/tmp")
    gate._pr_preflight = lambda *a, **k: {"status": "pass"}
    gate._cleanup_worktree = lambda *a, **k: None
    gate._record_gate_attribution = lambda *a, **k: None
    gate._read_log = lambda log_path: PYTEST_LOG
    gate.post_status = lambda repo, sha, state, *, context, description, target_url="", token: (
        posts.append({"state": state, "description": description, "target_url": target_url}))


# Failure path: enriched GateError -> red status links the run, not the PR.
def _boom(*a, **k):
    err = gate.GateError("external CI mirror not green")
    err.run_url = "https://github.com/o/ci/actions/runs/500"
    err.logs_url = ""
    err.failure_class = "workflow_failed"
    raise err


_install_gate_stubs()
posts.clear()
gate._run_suite_in_worktree = _boom
res = gate.run_gate_for_pr(fake_pr, repo="o/r", token="t", context="ctx",
                           work_root=Path("/tmp"), source_repo=Path("/tmp"), timeout_s=5)
red = [p for p in posts if p["state"] == "failure"]
ok(len(red) == 1, "gate posts exactly one red status on suite failure")
ok(red[0]["target_url"] == "https://github.com/o/ci/actions/runs/500",
   "gate red status links the failing CI run (not the PR)")
ok("test_merge_gate.py::test_blocks_uncovered" in red[0]["description"],
   "gate red status names the failing test")
ok(res["state"] == "failure" and res["target_url"].endswith("/runs/500"),
   "gate result carries the attributed target_url")


# Success path: external run -> green status links the run.
_install_gate_stubs()
posts.clear()
gate._run_suite_in_worktree = lambda *a, **k: {
    "ran_external": True, "run_url": "https://github.com/o/ci/actions/runs/501", "logs_url": ""}
res2 = gate.run_gate_for_pr(fake_pr, repo="o/r", token="t", context="ctx",
                            work_root=Path("/tmp"), source_repo=Path("/tmp"), timeout_s=5)
green = [p for p in posts if p["state"] == "success"]
ok(len(green) == 1, "gate posts exactly one green status on suite success")
ok(green[0]["target_url"] == "https://github.com/o/ci/actions/runs/501",
   "gate green status links the verifying CI run")


print("\nAll ci_attribution tests passed.")
