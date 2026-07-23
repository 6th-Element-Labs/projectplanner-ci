"""SIMPLIFY-8: verify(sha) -> {pending|green|red, url, contexts, stall?}."""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import store  # noqa: E402
from switchboard.application.commands import verify_ci  # noqa: E402

passed = 0
failed = 0


def ok(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok  {msg}")
    else:
        failed += 1
        print(f"FAIL  {msg}")


def _sha(label: str) -> str:
    return hashlib.sha1(f"{label}-{uuid.uuid4().hex}".encode()).hexdigest()


def _home(tmp: str) -> str:
    os.environ["PM_HOME"] = tmp
    os.environ["PM_DB_PATH"] = str(Path(tmp) / "maxwell.db")
    os.environ["PM_HELM_DB_PATH"] = str(Path(tmp) / "helm.db")
    os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(tmp) / "switchboard.db")
    os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(tmp) / "project_registry.db")
    # Drop any cached connection path from a prior fixture.
    if hasattr(store, "_PROJECT_CACHE"):
        try:
            store._PROJECT_CACHE.clear()
        except Exception:
            pass
    store.init_project_registry()
    store.init_db("switchboard")
    store.set_project_repo_topology(
        project="switchboard",
        canonical_repo="6th-Element-Labs/projectplanner",
        public_ci_repo="6th-Element-Labs/projectplanner-ci",
    )
    return "switchboard"


def test_invalid_sha():
    result = verify_ci.verify("not-a-sha", project="switchboard")
    ok(result["ok"] is False and result["error_code"] == "invalid_sha",
       "rejects non-sha input")
    ok(result["stall"] == "dispatch", "invalid sha stalls at dispatch")


def test_missing_run_is_dispatch_stall():
    with tempfile.TemporaryDirectory() as tmp:
        project = _home(tmp)
        sha = _sha("missing")
        result = verify_ci.verify(sha, project=project, ensure=False)
        ok(result["ok"] is True and result["status"] == "pending",
           "missing run surfaces as pending")
        ok(result["stall"] == "dispatch", "missing run attributes to dispatch")
        ok(result["contexts"] and result["contexts"][0]["context"],
           "returns required contexts even when missing")


def _mk_run(project: str, sha: str, **fields):
    payload = {
        "source_project": project,
        "source_repo": "6th-Element-Labs/projectplanner",
        "source_sha": sha,
        "mirror_repo": "6th-Element-Labs/projectplanner-ci",
        "mirror_branch": f"ci/verify/{sha[:12]}",
        "workflow": "verify",
        **fields,
    }
    created = store.create_external_ci_run(payload, actor="test", project=project)
    assert not created.get("error"), created
    return created


def test_status_and_stall_taxonomy():
    with tempfile.TemporaryDirectory() as tmp:
        project = _home(tmp)
        sha = _sha("dispatch")
        _mk_run(project, sha, status="requested",
                failure_class="mirror_sync_failed", failure_reason="push failed")
        pending = verify_ci.verify(sha, project=project)
        ok(pending["status"] == "pending" and pending["stall"] == "dispatch",
           "requested+mirror_sync_failed stalls at dispatch")

        sha2 = _sha("running")
        _mk_run(project, sha2, status="running", run_url="https://example.test/run/1")
        running = verify_ci.verify(sha2, project=project)
        ok(running["status"] == "pending" and running["stall"] == "run",
           "running stalls at run")
        ok(running["url"] == "https://example.test/run/1", "exposes run url")

        green_sha = _sha("green")
        _mk_run(project, green_sha, status="success", conclusion="success",
                run_url="https://example.test/run/2",
                status_context="Switchboard CI / VM gate")
        green = verify_ci.verify(green_sha, project=project)
        ok(green["status"] == "green" and green["stall"] is None,
           "success maps to green with no stall")

        red_sha = _sha("red")
        _mk_run(project, red_sha, status="failure", conclusion="failure",
                failure_class="workflow_failed",
                run_url="https://example.test/run/3")
        red = verify_ci.verify(red_sha, project=project)
        ok(red["status"] == "red" and red["stall"] == "run",
           "workflow_failed maps to red/run")


def test_callback_stall_when_gh_context_missing():
    with tempfile.TemporaryDirectory() as tmp:
        project = _home(tmp)
        sha = _sha("callback")
        _mk_run(project, sha, status="success", conclusion="success",
                status_context="Switchboard CI / VM gate",
                required_status_contexts=["Switchboard CI / VM gate"])

        def reader(_ctx):
            return {"state": "pending", "target_url": None}

        result = verify_ci.verify(sha, project=project, status_reader=reader)
        ok(result["status"] == "pending" and result["stall"] == "callback",
           "board-green + GH-pending attributes to callback")


def test_ensure_is_sha_only_and_hides_dispatch():
    with tempfile.TemporaryDirectory() as tmp:
        project = _home(tmp)
        sha = _sha("ensure")
        with mock.patch.object(
                verify_ci, "_ensure_dispatch",
                return_value={"dispatched": True, "run_id": "ecir-1",
                              "skip_reason": None, "head_sha": sha}) as ensure:
            result = verify_ci.verify(
                sha, project=project, ensure=True, actor="jobs/verify")
            ok(ensure.called, "ensure path invokes hidden dispatch")
            ok(ensure.call_args.args[0] == sha, "ensure takes exactly the SHA")
            ok(result["ensured"] is True, "result stamps ensured")
            ok("mirror_branch" not in result, "public surface hides mirror_branch")


def test_ensure_pr_hint_cannot_replace_explicit_sha():
    sha = _sha("merge-group")
    with mock.patch("ci_scratchpad_dispatch.dispatch_scratchpad_ref",
                    return_value={"dispatched": True, "head_sha": sha}) as dispatch, \
            mock.patch("ci_scratchpad_dispatch.try_dispatch_scratchpad") as pr_dispatch:
        result = verify_ci._ensure_dispatch(
            sha,
            project="switchboard",
            pr_number=412,
            repo="6th-Element-Labs/projectplanner",
            source_fetch_ref="refs/heads/gh-readonly-queue/master/pr-412-merge",
        )
    ok(result["head_sha"] == sha, "explicit merge-group SHA remains authoritative")
    ok(dispatch.call_args.args[:2] == (
        sha, "refs/heads/gh-readonly-queue/master/pr-412-merge"),
       "exact SHA and fetch ref use the ref dispatcher even with a PR hint")
    ok(not pr_dispatch.called, "PR-head resolution is not used for exact-SHA ensure")

    with mock.patch("ci_scratchpad_dispatch.dispatch_scratchpad_ref",
                    return_value={"dispatched": True, "head_sha": sha}) as dispatch, \
            mock.patch("ci_scratchpad_dispatch.try_dispatch_scratchpad") as pr_dispatch:
        verify_ci._ensure_dispatch(
            sha,
            project="switchboard",
            pr_number=412,
            repo="6th-Element-Labs/projectplanner",
        )
    ok(dispatch.call_args.args[:2] == (sha, sha),
       "PR hint without a fetch ref still fetches the authoritative SHA")
    ok(not pr_dispatch.called, "PR hint never re-resolves an exact-SHA request")


def test_mapping_entry():
    with tempfile.TemporaryDirectory() as tmp:
        project = _home(tmp)
        sha = _sha("mapping")
        result = verify_ci.execute_mapping_result(
            {"sha": sha, "project": project, "ensure": False}, actor="mcp")
        ok(result["schema"] == "switchboard.verify_ci.v1", "schema stamped")
        ok(result["status"] in {"pending", "green", "red"}, "surface status only")


if __name__ == "__main__":
    test_invalid_sha()
    test_missing_run_is_dispatch_stall()
    test_status_and_stall_taxonomy()
    test_callback_stall_when_gh_context_missing()
    test_ensure_is_sha_only_and_hides_dispatch()
    test_ensure_pr_hint_cannot_replace_explicit_sha()
    test_mapping_entry()
    print(f"\nverify_ci: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
