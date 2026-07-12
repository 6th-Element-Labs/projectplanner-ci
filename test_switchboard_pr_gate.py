import importlib.util
import os
import tempfile
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
    # GET .../commits/<sha>/statuses backs post_status idempotency; default to no prior
    # status so the POST proceeds. Individual tests below override the return as needed.
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
        context="Switchboard CI / VM gate",
        description="x" * 200,
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
ok(body["context"] == "Switchboard CI / VM gate",
   "commit status uses the documented VM-gate context")
ok(len(body["description"]) <= 140, "commit status description is GitHub-safe")
ok(body["target_url"].endswith("/pull/18"), "commit status links back to the PR")
tag_a = gate._run_tag(31, "abcdef1234567890")
tag_b = gate._run_tag(31, "abcdef1234567890")
ok(tag_a.startswith("pr-31-abcdef123456"), "gate run tag preserves PR and SHA prefix")
ok(tag_a != tag_b, "gate run tag is unique for concurrent invocations")


def fake_python(path: Path, version):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        f"  echo '{{\"executable\":\"{path}\",\"version\":{list(version)}}}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


with tempfile.TemporaryDirectory(prefix="switchboard-python-select-") as tmp:
    tmp_path = Path(tmp)
    py39 = fake_python(tmp_path / "py39", (3, 9, 6))
    py312 = fake_python(tmp_path / "py312", (3, 12, 1))
    selected = gate.select_ci_python(tmp_path, explicit=str(py312))
    ok(selected["path"] == str(py312) and selected["version_text"] == "3.12.1",
       "PR gate selects an explicit supported Python runtime")
    saved_env = {k: os.environ.get(k) for k in ("PATH", "PYTHON", "SWITCHBOARD_CI_PYTHON")}
    try:
        os.environ["PATH"] = ""
        os.environ.pop("PYTHON", None)
        os.environ.pop("SWITCHBOARD_CI_PYTHON", None)
        try:
            gate.select_ci_python(tmp_path / "empty", explicit=str(py39))
        except gate.GateError as exc:
            ok("Need Python 3.10+" in str(exc) and "3.9.6" in str(exc),
               "PR gate fails closed with clear unsupported-Python diagnostic")
        else:
            raise AssertionError("unsupported Python should fail closed")
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    repo_py = fake_python(tmp_path / "repo" / ".venv" / "bin" / "python", (3, 11, 8))
    saved_python = {k: os.environ.get(k) for k in ("PYTHON", "SWITCHBOARD_CI_PYTHON")}
    try:
        os.environ.pop("PYTHON", None)
        os.environ.pop("SWITCHBOARD_CI_PYTHON", None)
        selected_repo = gate.select_ci_python(tmp_path / "repo")
    finally:
        for key, value in saved_python.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    ok(selected_repo["path"] == str(repo_py) and selected_repo["source"] == "repo_venv",
       "PR gate selects repo .venv before ambient python3")

    calls = []
    original_run = gate._run
    try:
        gate._run = lambda cmd, **_kwargs: calls.append(cmd)
        log_path = tmp_path / "gate.log"
        gate.run_switchboard_gate(
            tmp_path / "worktree",
            log_path,
            timeout_s=30,
            python_runtime=selected,
        )
    finally:
        gate._run = original_run
    ok(calls and calls[0][:3] == [str(py312), "-m", "venv"],
       "PR gate creates its venv with the selected Python runtime")
    ok("version=3.12.1" in log_path.read_text(encoding="utf-8"),
       "PR gate log records selected Python version")

try:
    gate.post_status(
        "6th-Element-Labs/projectplanner",
        "abc123",
        "pending",
        context="Switchboard CI / VM gate",
        description="running",
        token="",
    )
except gate.GateError:
    print("  PASS  missing token fails closed")
else:
    raise AssertionError("missing token should fail closed")

with tempfile.TemporaryDirectory(prefix="switchboard-pr-gate-") as tmp:
    log_path = Path(tmp) / "gate.log"
    gate._write_preflight_log(log_path, Path(tmp), {
        "status": "red",
        "project": "switchboard",
        "intended_branch": "master",
        "repo_path": tmp,
        "target_ref": "HEAD",
        "target_sha": "abc123",
        "upstream_ref": "origin/master",
        "upstream_sha": "def456",
        "branch_distance": {"behind": 2, "ahead": 1},
        "dirty": True,
        "dirty_count": 1,
        "findings": [{"severity": "high", "code": "target_branch_behind_upstream",
                      "detail": "Target is behind.", "blocking": True}],
    })
    text = log_path.read_text(encoding="utf-8")
    ok("Switchboard review git preflight" in text,
       "gate log includes review preflight header")
    ok("target_branch_behind_upstream" in text and "branch_distance" in text,
       "gate log includes stale-branch evidence")

# A conflicted / un-gateable PR (no merge ref) must post a red status and return,
# not raise uncaught — an uncaught error aborts the whole gate run and fails the
# systemd unit, stopping every other PR's gate.
posted = []
_orig_post = gate.post_status
_orig_cache = gate._ensure_cache_repo
try:
    gate.post_status = lambda repo, sha, state, **kw: posted.append((state, kw.get("description", "")))

    def _boom(*_a, **_k):
        raise gate.GateError("PR #167 has no merge ref; rebase or resolve conflicts before gating.")

    gate._ensure_cache_repo = _boom
    with tempfile.TemporaryDirectory(prefix="switchboard-pr-gate-crash-") as tmp:
        result = gate.run_gate_for_pr(
            {"number": 167, "head": {"sha": "deadbeef"}, "html_url": "https://x/pull/167"},
            repo="6th-Element-Labs/projectplanner", token="tok",
            context="Switchboard CI / VM gate", work_root=Path(tmp), source_repo=Path(tmp),
            timeout_s=5)
    ok(result["state"] == "failure" and "no merge ref" in result.get("error", ""),
       "conflicted PR returns a failure result instead of raising")
    ok(any(state == "failure" for state, _ in posted),
       "conflicted PR posts a red VM-gate status")
finally:
    gate.post_status = _orig_post
    gate._ensure_cache_repo = _orig_cache

# External CI mirror classification (ADR-0006 gate resilience): a genuine test failure
# fails the gate, but a dispatch/sync outage returns 'unavailable' so the caller falls back
# to the local suite instead of hard-reding every PR (the fleet-wide 422 breakage).
_orig_ecm = gate.external_ci_mirror.request_external_ci_mirror_run
_orig_co = gate.subprocess.check_output
try:
    gate.subprocess.check_output = lambda *a, **k: "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
    with tempfile.TemporaryDirectory(prefix="switchboard-ext-ci-") as tmp:
        wt = Path(tmp)
        lp = wt / "ext.log"

        gate.external_ci_mirror.request_external_ci_mirror_run = lambda *a, **k: {"status": "success"}
        outcome, _ = gate._verify_on_external_ci_mirror(
            wt, lp, project="switchboard", number=1, token="t", timeout_s=5)
        ok(outcome == "success", "green external CI mirror -> success")

        gate.external_ci_mirror.request_external_ci_mirror_run = lambda *a, **k: {
            "error": "HTTP 422: Unexpected inputs provided",
            "failure_class": "workflow_trigger_failed"}
        outcome, _ = gate._verify_on_external_ci_mirror(
            wt, lp, project="switchboard", number=2, token="t", timeout_s=5)
        ok(outcome == "unavailable",
           "external CI dispatch error -> unavailable (falls back to local, not a gate failure)")

        def _raise_db_lock(*a, **k):
            raise Exception("database is locked")

        gate.external_ci_mirror.request_external_ci_mirror_run = _raise_db_lock
        outcome, res = gate._verify_on_external_ci_mirror(
            wt, lp, project="switchboard", number=4, token="t", timeout_s=5)
        ok(outcome == "unavailable" and res.get("failure_class") == "mirror_exception",
           "external CI mirror RAISING (e.g. 'database is locked') -> unavailable, not a gate failure")

        gate.external_ci_mirror.request_external_ci_mirror_run = lambda *a, **k: {
            "status": "failure", "failure_class": "test", "run_url": "https://x/runs/1"}
        try:
            gate._verify_on_external_ci_mirror(
                wt, lp, project="switchboard", number=3, token="t", timeout_s=5)
            ok(False, "genuine external CI test failure should raise")
        except gate.GateError:
            ok(True, "genuine external CI test failure (class=test) still fails the gate")

        # The class external_ci_mirror ACTUALLY emits for a red suite is workflow_failed
        # (+ status=="failure"). It must hard-fail the gate, NOT fall back to the local hog.
        gate.external_ci_mirror.request_external_ci_mirror_run = lambda *a, **k: {
            "status": "failure", "failure_class": "workflow_failed",
            "conclusion": "failure", "run_url": "https://x/runs/2"}
        try:
            gate._verify_on_external_ci_mirror(
                wt, lp, project="switchboard", number=5, token="t", timeout_s=5)
            ok(False, "workflow_failed should raise (real red suite, not infra)")
        except gate.GateError:
            ok(True, "workflow_failed (the class the mirror really emits) hard-fails; no local fallback")
finally:
    gate.external_ci_mirror.request_external_ci_mirror_run = _orig_ecm
    gate.subprocess.check_output = _orig_co

# Idempotency: post_status must NOT re-POST when the latest status for (sha, context) already
# matches — this is what stops the 1000-status-cap 422 loop that wedged the gate on long-lived PRs.
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

# A PR whose head SHA already has a terminal VM-gate status is skipped: no suite run, no re-post.
# This is what stops the timer re-running full CI (mirror/local venv hog) for every decided PR.
_orig_req2 = gate._github_request
_orig_cache2 = gate._ensure_cache_repo
try:
    gate._github_request = _idem_request(
        [{"context": "Switchboard CI / VM gate", "state": "success", "description": "passed"}])

    def _must_not_run(*_a, **_k):
        raise AssertionError("suite must not run for an already-gated SHA")

    gate._ensure_cache_repo = _must_not_run
    result = gate.run_gate_for_pr(
        {"number": 42, "head": {"sha": "cafe1234"}, "html_url": "https://x/pull/42"},
        repo="6th-Element-Labs/projectplanner", token="t",
        context="Switchboard CI / VM gate", work_root=Path("/tmp"), source_repo=Path("/tmp"),
        timeout_s=5)
    ok(result.get("skipped") == "already_gated" and result.get("state") == "success",
       "already-gated head SHA is skipped (no suite re-run, no re-post)")
finally:
    gate._github_request = _orig_req2
    gate._ensure_cache_repo = _orig_cache2

# ---------------------------------------------------------------------------
# HARDEN-70 / CI-3: gate native merge-queue (gh-readonly-queue) refs so an enabled
# merge queue doesn't hang waiting for the required status on the merge-group head SHA.
# ---------------------------------------------------------------------------

# list_merge_queue_refs parses GitHub's matching-refs response into {ref, sha} and fails open.
_orig_req_mq = gate._github_request
try:
    def _mq_refs_req(method, path, *, token, body=None):
        if method == "GET" and "matching-refs/heads/gh-readonly-queue" in path:
            return [
                {"ref": "refs/heads/gh-readonly-queue/master/pr-18-abc",
                 "object": {"sha": "mgsha18"}},
                {"ref": "refs/heads/gh-readonly-queue/master/pr-20-def",
                 "object": {"sha": "mgsha20"}},
                {"ref": "refs/heads/gh-readonly-queue/master/broken"},  # no object.sha -> dropped
            ]
        return []
    gate._github_request = _mq_refs_req
    refs = gate.list_merge_queue_refs("6th-Element-Labs/projectplanner", token="t")
    ok([r["sha"] for r in refs] == ["mgsha18", "mgsha20"],
       "list_merge_queue_refs returns each merge-group head SHA, dropping malformed rows")
    ok(all(r["ref"].startswith("refs/heads/gh-readonly-queue/") for r in refs),
       "list_merge_queue_refs keeps the gh-readonly-queue ref names")

    def _boom_req(method, path, *, token, body=None):
        raise gate.GateError("HTTP 404")
    gate._github_request = _boom_req
    ok(gate.list_merge_queue_refs("r", token="t") == [],
       "list_merge_queue_refs fails open to [] (empty/disabled queue is not an error)")
finally:
    gate._github_request = _orig_req_mq

# The merge-group run tag encodes the queue ref slug and the head SHA prefix.
mq_tag = gate._run_tag_mq("refs/heads/gh-readonly-queue/master/pr-18-abcdef", "0123456789abcdef")
ok(mq_tag.startswith("mq-pr-18-abcdef") and "0123456789ab" in mq_tag,
   "merge-group run tag encodes the queue ref slug and head SHA prefix")

_mq_names = ("latest_status", "post_status", "_ensure_cache_repo",
             "_prepare_merge_group_worktree", "_run_suite_in_worktree", "_cleanup_worktree")

# A green merge-group suite posts the required `Switchboard CI / VM gate` status to the
# merge-group HEAD SHA — this is precisely what lets the native queue advance.
posted_mq = []
_saved_mq = {name: getattr(gate, name) for name in _mq_names}
try:
    gate.latest_status = lambda *a, **k: None
    gate.post_status = lambda repo, sha, state, **kw: posted_mq.append((sha, state, kw.get("context")))
    gate._ensure_cache_repo = lambda *a, **k: Path("/tmp/cache")
    gate._prepare_merge_group_worktree = lambda *a, **k: Path("/tmp/run")
    gate._run_suite_in_worktree = lambda *a, **k: None
    gate._cleanup_worktree = lambda *a, **k: None
    with tempfile.TemporaryDirectory(prefix="switchboard-mq-") as tmp:
        res = gate.run_gate_for_merge_group(
            "refs/heads/gh-readonly-queue/master/pr-18-abc", "mgsha18abc123",
            repo="6th-Element-Labs/projectplanner", token="t",
            context="Switchboard CI / VM gate", work_root=Path(tmp), source_repo=Path(tmp),
            timeout_s=5)
    ok(res["state"] == "success" and res["sha"] == "mgsha18abc123",
       "merge-group gate returns success for a green suite")
    ok(("mgsha18abc123", "success", "Switchboard CI / VM gate") in posted_mq,
       "merge-group gate posts the required VM-gate status to the merge-group head SHA")
    ok(any(state == "pending" for _s, state, _c in posted_mq),
       "merge-group gate posts a pending status while the suite runs")
finally:
    for name, fn in _saved_mq.items():
        setattr(gate, name, fn)

# An already-gated merge-group head SHA is skipped: no suite run, no re-post (queue is decided).
_saved_mq2 = {name: getattr(gate, name) for name in ("latest_status", "post_status", "_ensure_cache_repo")}
try:
    gate.latest_status = lambda *a, **k: {"state": "success", "context": "Switchboard CI / VM gate"}

    def _no_post(*_a, **_k):
        raise AssertionError("must not re-post for an already-gated merge group")

    def _no_run(*_a, **_k):
        raise AssertionError("suite must not run for an already-gated merge group")

    gate.post_status = _no_post
    gate._ensure_cache_repo = _no_run
    res = gate.run_gate_for_merge_group(
        "refs/heads/gh-readonly-queue/master/pr-18-abc", "mgsha18abc123",
        repo="r", token="t", context="Switchboard CI / VM gate",
        work_root=Path("/tmp"), source_repo=Path("/tmp"), timeout_s=5)
    ok(res.get("skipped") == "already_gated" and res["state"] == "success",
       "already-gated merge-group head SHA is skipped (no suite re-run, no re-post)")
finally:
    for name, fn in _saved_mq2.items():
        setattr(gate, name, fn)

# A red merge-group suite posts a labelled failure and returns instead of raising, so one bad
# group never aborts the gate run (and every other queued group keeps getting gated).
posted_fail = []
_saved_mq3 = {name: getattr(gate, name) for name in _mq_names}
try:
    gate.latest_status = lambda *a, **k: None
    gate.post_status = lambda repo, sha, state, **kw: posted_fail.append((state, kw.get("description", "")))
    gate._ensure_cache_repo = lambda *a, **k: Path("/tmp/cache")
    gate._prepare_merge_group_worktree = lambda *a, **k: Path("/tmp/run")

    def _red_suite(*_a, **_k):
        raise gate.GateError("2 tests failed")

    gate._run_suite_in_worktree = _red_suite
    gate._cleanup_worktree = lambda *a, **k: None
    with tempfile.TemporaryDirectory(prefix="switchboard-mq-red-") as tmp:
        res = gate.run_gate_for_merge_group(
            "refs/heads/gh-readonly-queue/master/pr-9-x", "mgredsha",
            repo="r", token="t", context="Switchboard CI / VM gate",
            work_root=Path(tmp), source_repo=Path(tmp), timeout_s=5)
    ok(res["state"] == "failure" and "2 tests failed" in res.get("error", ""),
       "merge-group suite failure returns a failure result instead of raising")
    ok(any(state == "failure" and "merge queue" in desc for state, desc in posted_fail),
       "merge-group gate posts a red VM-gate status labelled (merge queue)")
finally:
    for name, fn in _saved_mq3.items():
        setattr(gate, name, fn)

# --- token-HTTPS origin + leak-safe credential env (CI-gate SSH-trust fix) ----------
_env_keys = ("SWITCHBOARD_CI_GIT_REMOTE", "SWITCHBOARD_CI_GITHUB_TOKEN")
_saved_env = {k: os.environ.get(k) for k in _env_keys}
_SECRET = "ghs_TOPSECRETtokenVALUE123"
try:
    os.environ.pop("SWITCHBOARD_CI_GIT_REMOTE", None)
    os.environ["SWITCHBOARD_CI_GITHUB_TOKEN"] = _SECRET

    origin = gate._origin_url(Path("/nonexistent-source"), "6th-Element-Labs/projectplanner")
    ok(origin == "https://github.com/6th-Element-Labs/projectplanner.git",
       "with a token set, origin is clean token-less HTTPS (no SSH host-key needed)")
    ok(_SECRET not in origin,
       "the token never appears in the origin URL (can't leak via argv/CalledProcessError)")

    genv = gate._git_env()
    helper = genv["GIT_CONFIG_VALUE_0"]
    ok(genv["GIT_CONFIG_KEY_0"] == "credential.helper" and genv["GIT_CONFIG_COUNT"] == "1",
       "git env injects a credential helper via GIT_CONFIG_* (never on the command line)")
    ok("${SWITCHBOARD_CI_GITHUB_TOKEN}" in helper and _SECRET not in helper,
       "the helper references the token by variable name only — the secret is never in config")
    ok(genv.get("GIT_TERMINAL_PROMPT") == "0",
       "git env disables interactive credential prompts so a bad token fails fast")

    os.environ["SWITCHBOARD_CI_GIT_REMOTE"] = "git@example.com:custom/mirror.git"
    ok(gate._origin_url(Path("/x"), "a/b") == "git@example.com:custom/mirror.git",
       "an explicit SWITCHBOARD_CI_GIT_REMOTE still wins over the token default")

    os.environ.pop("SWITCHBOARD_CI_GIT_REMOTE", None)
    os.environ.pop("SWITCHBOARD_CI_GITHUB_TOKEN", None)
    ok(gate._origin_url(Path("/nonexistent-source"), "a/b") == "git@github.com:a/b.git",
       "with no token and an unreadable source origin, prior SSH fallback is preserved")
    ok(gate._git_env() is None,
       "no token -> git env is None (inherit os.environ, unchanged local/SSH behaviour)")
finally:
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print("\n46 passed, 0 failed")
