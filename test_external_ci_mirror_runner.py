#!/usr/bin/env python3
"""CI-MIRROR-2 operational runner regressions."""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="external-ci-runner-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import external_ci_mirror  # noqa: E402
import store  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        self.now += 1.0
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeRunner:
    def __init__(self, mode="success"):
        self.mode = mode
        self.commands = []
        self.run_list_calls = 0
        self.remote_read_calls = 0

    def __call__(self, args, cwd):
        self.commands.append(args)
        if args[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(args, 0, "true\n", "")
        if args[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(
                args, 0, "abcdef1234567890abcdef1234567890abcdef12\n", "")
        if args[:3] == ["git", "fetch", "--no-tags"]:
            return subprocess.CompletedProcess(args, 0, "", "fetched")
        if args[:3] == ["git", "ls-remote", "--heads"]:
            self.remote_read_calls += 1
            if self.mode == "race_same" and self.remote_read_calls == 1:
                return subprocess.CompletedProcess(args, 0, "", "")
            if self.mode == "same_ref":
                return subprocess.CompletedProcess(
                    args, 0,
                    "abcdef1234567890abcdef1234567890abcdef12\t"
                    "refs/heads/ci/existing/ref\n", "")
            if self.mode == "race_same":
                return subprocess.CompletedProcess(
                    args, 0,
                    "abcdef1234567890abcdef1234567890abcdef12\t"
                    "refs/heads/ci/existing/ref\n", "")
            if self.mode == "different_ref":
                return subprocess.CompletedProcess(
                    args, 0,
                    "1111111111111111111111111111111111111111\t"
                    "refs/heads/ci/existing/ref\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "push"]:
            if self.mode in {"push_fail", "race_same"}:
                return subprocess.CompletedProcess(args, 1, "", "permission denied")
            return subprocess.CompletedProcess(args, 0, "", "pushed")
        if args[:3] == ["gh", "workflow", "run"]:
            if self.mode == "trigger_fail":
                return subprocess.CompletedProcess(args, 1, "", "workflow does not have workflow_dispatch")
            return subprocess.CompletedProcess(args, 0, "queued\n", "")
        if args[:3] == ["gh", "run", "list"]:
            self.run_list_calls += 1
            conclusion = "failure" if self.mode == "workflow_fail" else "success"
            status = "in_progress" if self.run_list_calls == 1 else "completed"
            payload = [{
                "databaseId": 42,
                "status": status,
                "conclusion": None if status != "completed" else conclusion,
                "url": "https://github.com/6th-Element-Labs/public-ci/actions/runs/42",
                "headSha": "1234567public",
            }]
            return subprocess.CompletedProcess(args, 0, external_ci_mirror.json.dumps(payload), "")
        if args[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(
                args, 0,
                external_ci_mirror.json.dumps({
                    "artifacts": [{
                        "name": "strict-log",
                        "archive_download_url": "https://example.test/artifact.zip",
                        "expired": False,
                    }]
                }),
                "",
            )
        return subprocess.CompletedProcess(args, 99, "", "unexpected command")


def make_request(task_id):
    return {
        "source_project": "private-product",
        "source_branch": "codex/CIQA-1-proof",
        "source_sha": "abcdef1234567890abcdef1234567890abcdef12",
        "workflow": "strict.yml",
        "task_id": task_id,
        "claim_id": "claim-123",
        "agent_id": "codex/CIQA-1-proof",
        "poll_interval_seconds": 1,
        "timeout_seconds": 30,
    }


try:
    store.init_project_registry()
    store.init_db(P)
    store.create_project(
        "Private Product",
        project_id="private-product",
        github_repo="6th-Element-Labs/private-product",
        actor="test",
    )
    store.set_project_repo_topology(
        project="private-product",
        public_ci_repo="6th-Element-Labs/public-ci",
        public_ci_required_status_contexts="public-ci/full-suite",
    )
    source_path = os.path.join(_TMP, "source")
    os.makedirs(source_path)

    orphan_task = store.create_task(
        {"workstream_id": "CIQA", "title": "orphan recovery"}, actor="test", project=P)
    orphan_request = make_request(orphan_task["task_id"])
    orphan_request["source_sha"] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    orphan = store.create_external_ci_run(
        {**orphan_request, "_execution_owner_id": "crashed-owner",
         "_execution_now": 10.0, "_execution_lease_seconds": 5.0},
        actor="test", project=P)
    recovery_clock = FakeClock()
    recovery_runner = FakeRunner()
    recovered = external_ci_mirror.request_external_ci_mirror_run(
        {**orphan_request, "_execution_owner_id": "retry-owner"},
        source_path, actor="test", project=P, runner=recovery_runner,
        sleep_fn=recovery_clock.sleep, now_fn=recovery_clock.time)
    ok(orphan["status"] == "requested" and recovered["status"] == "success" and
       recovered["execution_fence"] == 2 and recovery_runner.commands,
       "expired requested owner is atomically reclaimed and executed on retry")
    stale_write = store.update_external_ci_run(
        recovered["run_id"],
        {"status": "error", "_execution_owner_id": "crashed-owner",
         "_execution_fence": 1},
        actor="test", project=P)
    ok(stale_write.get("error") == "external_ci_execution_fence_lost" and
       store.get_external_ci_run(recovered["run_id"], project=P)["status"] == "success",
       "superseded execution fence cannot overwrite the reclaimed run")

    healthy_task = store.create_task(
        {"workstream_id": "CIQA", "title": "healthy coalescing"}, actor="test", project=P)
    healthy_request = make_request(healthy_task["task_id"])
    healthy_request["source_sha"] = "9999999999999999999999999999999999999999"
    store.create_external_ci_run(
        {**healthy_request, "_execution_owner_id": "healthy-owner",
         "_execution_now": 1000.0, "_execution_lease_seconds": 100.0},
        actor="test", project=P)
    healthy_runner = FakeRunner()
    healthy_duplicate = external_ci_mirror.request_external_ci_mirror_run(
        {**healthy_request, "_execution_owner_id": "duplicate-owner"},
        source_path, actor="test", project=P, runner=healthy_runner,
        sleep_fn=lambda _seconds: None, now_fn=lambda: 1001.0)
    ok(healthy_duplicate.get("coalesced") is True and
       healthy_duplicate["status"] == "requested" and not healthy_runner.commands,
       "healthy requested owner coalesces concurrent duplicates without execution")

    task = store.create_task({"workstream_id": "CIQA", "title": "runner proof"},
                             actor="test", project=P)
    clock = FakeClock()
    runner = FakeRunner()
    success = external_ci_mirror.request_external_ci_mirror_run(
        make_request(task["task_id"]), source_path, actor="test", project=P,
        runner=runner, sleep_fn=clock.sleep, now_fn=clock.time)
    ok(success["ok"] is True and success["status"] == "success",
       "runner mirrors, dispatches, polls, and records success")
    ok(success["ci_repo"] == "6th-Element-Labs/public-ci" and
       success["status_context"] == "public-ci/full-suite",
       "runner defaults ci_repo/status_context from repo_topology")
    ok(success["run_url"].endswith("/42") and success["artifacts"][0]["name"] == "strict-log",
       "success stores run URL and artifacts")
    effects = store.list_external_effects(effect_type="external_ci_mirror",
                                          task_id=task["task_id"], project=P)
    ok(len(effects) == 1 and effects[0]["status"] == "verified",
       "successful CI mirror verifies the external side effect")
    ok(any(cmd[:2] == ["git", "push"] and
           cmd[-1].endswith(":refs/heads/" + success["mirror_branch"])
           for cmd in runner.commands),
       "mirror push targets the deterministic ci/ branch")
    workflow_runs = [cmd for cmd in runner.commands if cmd[:3] == ["gh", "workflow", "run"]]
    ok(workflow_runs and
       "source_sha=abcdef1234567890abcdef1234567890abcdef12" in workflow_runs[0] and
       "status_context=public-ci/full-suite" in workflow_runs[0],
       "workflow dispatch receives canonical source SHA and status context")

    push_task = store.create_task({"workstream_id": "CIQA", "title": "push trigger"},
                                  actor="test", project=P)
    push_request = make_request(push_task["task_id"])
    push_request["source_sha"] = "bbbbbb1234567890abcdef1234567890abcdef12"
    push_request["push_triggered"] = True
    push_request["poll_after_push"] = True
    push_request["cleanup_mirror_branch"] = True
    push_request["source_fetch_ref"] = "refs/pull/42/head"
    push_runner = FakeRunner()
    push_success = external_ci_mirror.request_external_ci_mirror_run(
        push_request, source_path, actor="test", project=P,
        runner=push_runner, sleep_fn=clock.sleep, now_fn=clock.time)
    ok(push_success["ok"] is True and
       not any(cmd[:3] == ["gh", "workflow", "run"] for cmd in push_runner.commands),
       "push trigger mode relies on the mirror push and skips workflow_dispatch")
    ok(any(cmd[:3] == ["gh", "run", "list"] for cmd in push_runner.commands),
       "push trigger mode still polls and records the provider run")
    ok(any(cmd[:3] == ["git", "fetch", "--no-tags"] and
           cmd[-1] == "refs/pull/42/head" for cmd in push_runner.commands),
       "push trigger mode fetches the canonical PR head before mirroring its exact SHA")
    ok(any(cmd[:2] == ["git", "push"] and "--delete" in cmd
           for cmd in push_runner.commands) and
       push_success["result"]["branch_cleanup"]["status"] == "deleted",
       "terminal scratchpad run deletes its disposable mirror branch")

    duplicate_runner = FakeRunner()
    duplicate = external_ci_mirror.request_external_ci_mirror_run(
        make_request(task["task_id"]), source_path, actor="test", project=P,
        runner=duplicate_runner, sleep_fn=clock.sleep, now_fn=clock.time)
    ok(duplicate.get("coalesced") is True and not duplicate_runner.commands,
       "terminal duplicate is idempotent and does not push or dispatch again")

    same_ref_task = store.create_task(
        {"workstream_id": "CIQA", "title": "same remote ref"}, actor="test", project=P)
    same_ref_request = make_request(same_ref_task["task_id"])
    same_ref_request["source_sha"] = "cccccc1234567890abcdef1234567890abcdef12"
    same_ref_request["push_triggered"] = True
    same_ref_request["poll_after_push"] = False
    same_ref = external_ci_mirror.request_external_ci_mirror_run(
        same_ref_request, source_path, actor="test", project=P,
        runner=FakeRunner("same_ref"), sleep_fn=clock.sleep, now_fn=clock.time)
    ok(same_ref["ok"] is True and same_ref["result"]["mirror_ref_reused"] is True,
       "an existing mirror ref at the exact SHA is idempotent success")

    race_ref_task = store.create_task(
        {"workstream_id": "CIQA", "title": "concurrent remote ref"}, actor="test", project=P)
    race_ref_request = make_request(race_ref_task["task_id"])
    race_ref_request["source_sha"] = "dddddd1234567890abcdef1234567890abcdef12"
    race_ref_request["push_triggered"] = True
    race_ref_request["poll_after_push"] = False
    race_ref = external_ci_mirror.request_external_ci_mirror_run(
        race_ref_request, source_path, actor="test", project=P,
        runner=FakeRunner("race_same"), sleep_fn=clock.sleep, now_fn=clock.time)
    ok(race_ref["ok"] is True and race_ref["result"]["mirror_ref_reused"] is True,
       "a concurrent exact-ref creation closes the readback/push race")

    different_ref_task = store.create_task(
        {"workstream_id": "CIQA", "title": "different remote ref"}, actor="test", project=P)
    different_ref_request = make_request(different_ref_task["task_id"])
    different_ref_request["source_sha"] = "2222222222222222222222222222222222222222"
    different_ref = external_ci_mirror.request_external_ci_mirror_run(
        different_ref_request, source_path, actor="test", project=P,
        runner=FakeRunner("different_ref"), sleep_fn=clock.sleep, now_fn=clock.time)
    ok(different_ref["ok"] is False and
       different_ref["failure_class"] == "mirror_sync_failed",
       "an existing mirror ref at a different SHA fails closed")

    for mode, expected_class, source_sha in (
        ("push_fail", "mirror_sync_failed",
         "3333333333333333333333333333333333333333"),
        ("trigger_fail", "workflow_trigger_failed",
         "4444444444444444444444444444444444444444"),
        ("workflow_fail", "workflow_failed",
         "5555555555555555555555555555555555555555"),
    ):
        t = store.create_task({"workstream_id": "CIQA", "title": mode},
                              actor="test", project=P)
        failure_request = make_request(t["task_id"])
        failure_request["source_sha"] = source_sha
        result = external_ci_mirror.request_external_ci_mirror_run(
            failure_request, source_path, actor="test", project=P,
            runner=FakeRunner(mode), sleep_fn=clock.sleep, now_fn=clock.time)
        ok(result["ok"] is False and result["failure_class"] == expected_class,
           f"{mode} records {expected_class}")
        effect = store.list_external_effects(effect_type="external_ci_mirror",
                                             task_id=t["task_id"], project=P)[0]
        ok(effect["status"] in {"failed", "dead_letter"} and effect["last_error"],
           f"{mode} leaves visible failed side-effect state")

    listed = store.list_external_ci_runs(status="success", project=P)
    ok(any(r["run_id"] == success["run_id"] for r in listed),
       "external CI runs can be listed by status after execution")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

# --- BUG-158 hardening: a caller cannot make a run unreclaimable ------------
# The REST route forwards the caller's raw body into the run request, so the
# lease bound and the owner identity must both be server-owned. An unbounded
# lease would recreate the orphan-forever run this fence exists to recover.
_clamp = store.clamp_external_ci_lease_seconds
ok(_clamp(10 ** 12) == store.EXTERNAL_CI_EXECUTION_LEASE_MAX_SECONDS,
   "an enormous requested lease is clamped to the recoverable maximum")
ok(_clamp(float("inf")) == store.EXTERNAL_CI_EXECUTION_LEASE_SECONDS,
   "a non-finite lease falls back to the default rather than never expiring")
ok(_clamp(0) >= 1.0 and _clamp(-5) >= 1.0,
   "a zero or negative lease still yields a positive deadline")
ok(_clamp("not-a-number") == store.EXTERNAL_CI_EXECUTION_LEASE_SECONDS,
   "an unparseable lease falls back to the default")

_mirror_src = pathlib.Path(__file__).resolve().parent.joinpath(
    "external_ci_mirror.py").read_text(encoding="utf-8")
ok('request.pop("_execution_owner_id", None)' in _mirror_src,
   "a caller-supplied execution owner id is dropped, never honoured")
ok("execution_owner_id = \"ecio-\" + uuid.uuid4().hex" in _mirror_src,
   "the execution owner identity is always generated server-side")

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
