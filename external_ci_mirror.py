"""External CI mirror runner.

This module turns Switchboard's external_ci_runs model into provider action:
push an exact source SHA to a disposable public CI branch, dispatch a workflow,
poll the run, and write the result back to Switchboard. The private/source repo
remains the source of truth; the public repo is only verification infrastructure.
"""
import json
import os
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import store


CommandRunner = Callable[[List[str], str], subprocess.CompletedProcess]
SleepFn = Callable[[float], None]
NowFn = Callable[[], float]


class ExternalCiError(Exception):
    def __init__(self, failure_class: str, message: str, result: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.failure_class = failure_class
        self.message = message
        self.result = result or {}


def _default_run(args: List[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=60)


def _run(args: List[str], cwd: str, runner: Optional[CommandRunner] = None) -> subprocess.CompletedProcess:
    return (runner or _default_run)(args, cwd)


def _check(args: List[str], cwd: str, failure_class: str, label: str,
           runner: Optional[CommandRunner] = None) -> subprocess.CompletedProcess:
    cp = _run(args, cwd, runner)
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip() or f"{label} failed"
        raise ExternalCiError(failure_class, detail, {
            "command": args,
            "returncode": cp.returncode,
            "stdout": (cp.stdout or "").strip(),
            "stderr": (cp.stderr or "").strip(),
        })
    return cp


def _json(args: List[str], cwd: str, failure_class: str, label: str,
          runner: Optional[CommandRunner] = None) -> Any:
    cp = _check(args, cwd, failure_class, label, runner)
    text = (cp.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception as e:
        raise ExternalCiError(failure_class, f"{label} returned non-JSON output", {
            "command": args,
            "stdout": text,
            "error": str(e),
        })


def _repo_parts(repo: str) -> Tuple[str, str]:
    owner, name = (repo or "").split("/", 1)
    return owner, name


def _mirror_url(mirror_repo: str, mirror_remote_url: str = "") -> str:
    if mirror_remote_url:
        return mirror_remote_url
    return f"https://github.com/{mirror_repo}.git"


def _workflow_inputs_args(inputs: Dict[str, Any]) -> List[str]:
    args: List[str] = []
    for key in sorted((inputs or {}).keys()):
        value = inputs[key]
        if value is None:
            continue
        args.extend(["-f", f"{key}={value}"])
    return args


def _workflow_inputs_for_run(run: Dict[str, Any], request: Dict[str, Any]) -> Dict[str, Any]:
    inputs = dict(request.get("workflow_inputs") or {})
    inputs.setdefault("source_sha", run.get("source_sha"))
    if run.get("status_context"):
        inputs.setdefault("status_context", run.get("status_context"))
    return inputs


def _select_run(runs: Any, triggered_after: float = 0.0) -> Optional[Dict[str, Any]]:
    if not isinstance(runs, list):
        return None
    candidates = [r for r in runs if isinstance(r, dict)]
    if not candidates:
        return None
    # gh returns newest first; keep that behavior but tolerate fake/test order.
    return candidates[0]


def _artifact_list(mirror_repo: str, run_id: Any, cwd: str,
                   runner: Optional[CommandRunner] = None) -> List[Dict[str, Any]]:
    owner, repo = _repo_parts(mirror_repo)
    path = f"repos/{owner}/{repo}/actions/runs/{run_id}/artifacts"
    try:
        raw = _json(["gh", "api", path], cwd, "workflow_poll_failed",
                    "artifact readback", runner)
    except ExternalCiError:
        return []
    artifacts = raw.get("artifacts") if isinstance(raw, dict) else raw
    if not isinstance(artifacts, list):
        return []
    out = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        out.append({
            "name": artifact.get("name"),
            "url": artifact.get("archive_download_url") or artifact.get("url"),
            "expired": artifact.get("expired"),
        })
    return out


def _run_url(mirror_repo: str, run_id: Any) -> str:
    return f"https://github.com/{mirror_repo}/actions/runs/{run_id}"


def _update_failure(run: Dict[str, Any], failure_class: str, reason: str,
                    readback: Dict[str, Any], actor: str, project: str) -> Dict[str, Any]:
    status = "failure" if failure_class == "workflow_failed" else "error"
    updated = store.update_external_ci_run(
        run["run_id"],
        {
            "status": status,
            "conclusion": readback.get("conclusion") or ("failure" if status == "failure" else "error"),
            "run_url": readback.get("run_url"),
            "logs_url": readback.get("logs_url"),
            "artifacts": readback.get("artifacts") or [],
            "failure_class": failure_class,
            "failure_reason": reason,
            "result": readback,
        },
        actor=actor,
        project=project,
    )
    if run.get("effect_key"):
        store.fail_external_effect(
            run["effect_key"], reason, readback=readback,
            dead_letter=failure_class in {"mirror_sync_failed", "workflow_failed"},
            actor=actor, project=project)
    updated["ok"] = False
    return updated


def request_external_ci_mirror_run(request: Dict[str, Any], source_path: str,
                                   actor: str = "system",
                                   project: str = store.DEFAULT_PROJECT,
                                   runner: Optional[CommandRunner] = None,
                                   sleep_fn: SleepFn = time.sleep,
                                   now_fn: NowFn = time.time) -> Dict[str, Any]:
    """Create/resume an external CI mirror run and execute push/dispatch/poll."""
    if not source_path or not os.path.isdir(source_path):
        return {"error": "source_path must be an existing local git checkout",
                "failure_class": "mirror_sync_failed"}
    run = store.create_external_ci_run(request or {}, actor=actor, project=project)
    if run.get("error"):
        return run
    if run.get("status") in store.EXTERNAL_CI_TERMINAL_STATUSES:
        run["resumed_terminal"] = True
        return run
    try:
        return _execute_run(run, source_path, actor, project, runner, sleep_fn, now_fn,
                            request or {})
    except ExternalCiError as e:
        return _update_failure(run, e.failure_class, e.message, e.result,
                               actor=actor, project=project)


def poll_external_ci_mirror_run(run_id: str, source_path: str,
                                actor: str = "system",
                                project: str = store.DEFAULT_PROJECT,
                                runner: Optional[CommandRunner] = None,
                                sleep_fn: SleepFn = time.sleep,
                                now_fn: NowFn = time.time,
                                poll_interval_seconds: float = 15.0,
                                timeout_seconds: float = 1800.0) -> Dict[str, Any]:
    run = store.get_external_ci_run(run_id, project=project)
    if not run:
        return {"error": "external_ci_run not found", "run_id": run_id}
    if run.get("status") in store.EXTERNAL_CI_TERMINAL_STATUSES:
        run["resumed_terminal"] = True
        return run
    if not source_path or not os.path.isdir(source_path):
        return _update_failure(
            run, "workflow_poll_failed",
            "source_path must be an existing local git checkout for gh polling context",
            {"run_id": run_id}, actor=actor, project=project)
    try:
        return _poll_run(run, source_path, actor, project, runner, sleep_fn, now_fn,
                         poll_interval_seconds, timeout_seconds)
    except ExternalCiError as e:
        return _update_failure(run, e.failure_class, e.message, e.result,
                               actor=actor, project=project)


def _execute_run(run: Dict[str, Any], source_path: str, actor: str,
                 project: str, runner: Optional[CommandRunner],
                 sleep_fn: SleepFn, now_fn: NowFn,
                 request: Dict[str, Any]) -> Dict[str, Any]:
    source_sha = run["source_sha"]
    mirror_branch = run["mirror_branch"]
    mirror_repo = run["mirror_repo"]
    workflow = run["workflow"]
    mirror_remote_url = _mirror_url(mirror_repo, request.get("mirror_remote_url") or "")

    _check(["git", "rev-parse", "--is-inside-work-tree"], source_path,
           "mirror_sync_failed", "source checkout validation", runner)
    resolved = _check(["git", "rev-parse", "--verify", f"{source_sha}^{{commit}}"],
                      source_path, "mirror_sync_failed", "source SHA validation", runner)
    resolved_sha = (resolved.stdout or "").strip() or source_sha

    push_ref = f"{resolved_sha}:refs/heads/{mirror_branch}"
    push = _check(["git", "push", mirror_remote_url, push_ref],
                  source_path, "mirror_sync_failed", "mirror push", runner)
    mirrored = store.update_external_ci_run(
        run["run_id"],
        {
            "status": "mirrored",
            "result": {
                "source_repo": run.get("source_repo"),
                "source_sha": source_sha,
                "resolved_source_sha": resolved_sha,
                "ci_repo": mirror_repo,
                "mirror_remote_url": mirror_remote_url,
                "mirror_branch": mirror_branch,
                "status_context": run.get("status_context"),
                "mirror_push_stdout": (push.stdout or "").strip(),
                "mirror_push_stderr": (push.stderr or "").strip(),
            },
        },
        actor=actor,
        project=project,
    )
    if run.get("effect_key"):
        store.mark_external_effect_issued(
            run["effect_key"],
            {
                "mirror_repo": mirror_repo,
                "mirror_branch": mirror_branch,
                "status_context": run.get("status_context"),
                "source_repo": run.get("source_repo"),
                "ci_repo": mirror_repo,
                "source_sha": source_sha,
                "resolved_source_sha": resolved_sha,
            },
            actor=actor,
            project=project,
        )

    push_triggered = bool(request.get("push_triggered"))
    poll_after_push = request.get("poll_after_push")
    if poll_after_push is None:
        poll_after_push = not push_triggered
    result_payload = {**(mirrored.get("result") or {})}
    if push_triggered:
        result_payload["push_triggered"] = True
        result_payload["workflow_dispatch"] = "skipped_push_triggered"
        updated = store.update_external_ci_run(
            run["run_id"],
            {"status": "triggered", "result": result_payload},
            actor=actor,
            project=project,
        )
        if not poll_after_push:
            updated["ok"] = True
            return updated
        return _poll_run({**run, "status": "triggered"}, source_path, actor, project,
                         runner, sleep_fn, now_fn,
                         float(request.get("poll_interval_seconds") or 15),
                         float(request.get("timeout_seconds") or 1800))

    trigger_args = ["gh", "workflow", "run", workflow, "--repo", mirror_repo, "--ref", mirror_branch]
    trigger_args.extend(_workflow_inputs_args(_workflow_inputs_for_run(run, request)))
    trigger = _check(trigger_args, source_path, "workflow_trigger_failed",
                     "workflow dispatch", runner)
    store.update_external_ci_run(
        run["run_id"],
        {
            "status": "triggered",
            "result": {
                **result_payload,
                "workflow_dispatch_stdout": (trigger.stdout or "").strip(),
                "workflow_dispatch_stderr": (trigger.stderr or "").strip(),
            },
        },
        actor=actor,
        project=project,
    )
    return _poll_run({**run, "status": "triggered"}, source_path, actor, project,
                     runner, sleep_fn, now_fn,
                     float(request.get("poll_interval_seconds") or 15),
                     float(request.get("timeout_seconds") or 1800))


def _poll_run(run: Dict[str, Any], source_path: str, actor: str,
              project: str, runner: Optional[CommandRunner],
              sleep_fn: SleepFn, now_fn: NowFn,
              poll_interval_seconds: float, timeout_seconds: float) -> Dict[str, Any]:
    deadline = now_fn() + max(1.0, timeout_seconds)
    mirror_repo = run["mirror_repo"]
    mirror_branch = run["mirror_branch"]
    workflow = run["workflow"]
    selected: Optional[Dict[str, Any]] = None
    while now_fn() <= deadline:
        runs = _json(
            ["gh", "run", "list", "--repo", mirror_repo, "--workflow", workflow,
             "--branch", mirror_branch,
             "--json", "databaseId,status,conclusion,url,headSha,createdAt,updatedAt",
             "--limit", "20"],
            source_path,
            "workflow_poll_failed",
            "workflow run list",
            runner,
        )
        selected = _select_run(runs)
        if not selected:
            store.update_external_ci_run(
                run["run_id"], {"status": "triggered", "result": {"poll": "no_run_yet"}},
                actor=actor, project=project)
            sleep_fn(max(0.1, poll_interval_seconds))
            continue
        status = (selected.get("status") or "").lower()
        conclusion = (selected.get("conclusion") or "").lower()
        run_id = selected.get("databaseId") or selected.get("id")
        run_url = selected.get("url") or _run_url(mirror_repo, run_id)
        logs_url = f"{run_url}/logs" if run_url else None
        if status and status != "completed":
            store.update_external_ci_run(
                run["run_id"],
                {"status": "running", "run_url": run_url, "logs_url": logs_url,
                 "result": {"provider_run": selected}},
                actor=actor,
                project=project,
            )
            sleep_fn(max(0.1, poll_interval_seconds))
            continue
        artifacts = _artifact_list(mirror_repo, run_id, source_path, runner)
        result = {
            "provider_run": selected,
            "tested_public_sha": selected.get("headSha"),
            "source_repo": run.get("source_repo"),
            "source_sha": run["source_sha"],
            "ci_repo": mirror_repo,
            "mirror_repo": mirror_repo,
            "mirror_branch": mirror_branch,
            "status_context": run.get("status_context"),
        }
        if conclusion == "success":
            updated = store.update_external_ci_run(
                run["run_id"],
                {"status": "success", "conclusion": "success", "run_url": run_url,
                 "logs_url": logs_url, "artifacts": artifacts, "result": result},
                actor=actor, project=project,
            )
            if run.get("effect_key"):
                store.verify_external_effect(run["effect_key"], readback=result,
                                             actor=actor, project=project)
            updated["ok"] = True
            return updated
        raise ExternalCiError(
            "workflow_failed",
            f"workflow completed with conclusion {conclusion or 'unknown'}",
            {"run_url": run_url, "logs_url": logs_url, "artifacts": artifacts,
             "conclusion": conclusion or "unknown", **result},
        )
    raise ExternalCiError(
        "workflow_poll_failed",
        f"workflow did not complete before timeout_seconds={timeout_seconds}",
        {"provider_run": selected or {}, "mirror_repo": mirror_repo,
         "mirror_branch": mirror_branch, "workflow": workflow},
    )
