"""External CI mirror runner.

This module turns Switchboard's external_ci_runs model into provider action:
push an exact source SHA to a disposable public CI ref, dispatch a workflow,
poll the run, and write the result back to Switchboard. The private/source repo
remains the source of truth; the public repo is only verification infrastructure.
"""
import json
import os
import subprocess
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import store


CommandRunner = Callable[[List[str], str], subprocess.CompletedProcess]
SleepFn = Callable[[float], None]
NowFn = Callable[[], float]

# store.EXTERNAL_CI_TERMINAL_STATUSES lumps four very different endings together.
# Split them by the only question that matters to a caller waiting on a commit status:
# did this run publish a VERDICT?
#   success / failure -> yes. CI ran; the required status exists on the SHA.
#   error / cancelled -> no. The run aborted (mirror sync failed, token expired,
#                        GitHub 5xx) and nothing was ever posted, so re-running it is
#                        the only way that SHA can ever get a status. See BUG-180.
ABORTIVE_TERMINAL_STATUSES = {"error", "cancelled"}
VERDICT_TERMINAL_STATUSES = {"success", "failure"}


def _truthy(value: Any) -> bool:
    return str(value if value is not None else "").strip().lower() in {
        "1", "true", "yes", "on"}


# Env vars any of which give git/gh a usable credential for the canonical repo.
GITHUB_CREDENTIAL_ENV_VARS = (
    "PM_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN",
    "SWITCHBOARD_CI_GITHUB_TOKEN", "SWITCHBOARD_CI_DISPATCH_TOKEN",
    "PRIVATE_READ_TOKEN",
)


def _canonical_repo_hint() -> str:
    """An ``owner/repo`` slug good enough to pick the right App installation.

    Installation tokens are per-installation, and an installation covers one owner —
    so any repo under that owner resolves identically. Prefer the configured canonical
    repo, then the mirror, then the org default."""
    for name in ("SWITCHBOARD_CI_REPO", "PM_GITHUB_REPO_SWITCHBOARD", "PM_GITHUB_REPO",
                 "SWITCHBOARD_CI_MIRROR_REPO"):
        value = (os.environ.get(name) or "").strip()
        if "/" in value:
            return value
    return "6th-Element-Labs/projectplanner"


def _github_credential_error() -> str:
    """Return a human-actionable message if no GitHub credential is visible, else ''.

    Cheap pre-flight so a credential-less caller is refused before any run state is
    created. Deliberately env-only: it must not shell out or hit the network on the hot
    path, and every real dispatcher gets its token from the service EnvironmentFile.
    """
    if any(str(os.environ.get(name) or "").strip()
           for name in GITHUB_CREDENTIAL_ENV_VARS):
        return ""
    import getpass
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - getpass is environment-dependent
        user = "unknown"
    return (
        "no GitHub credential in the environment "
        f"(checked {', '.join(GITHUB_CREDENTIAL_ENV_VARS)}); running as user {user!r}. "
        "The mirror push would fail with \"could not read Username for "
        "'https://github.com'\". Run as the service account with its EnvironmentFile "
        "loaded — e.g. sudo -u projectplanner with /opt/projectplanner/.env sourced — "
        "not as root."
    )


class ExternalCiError(Exception):
    def __init__(self, failure_class: str, message: str, result: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.failure_class = failure_class
        self.message = message
        self.result = result or {}


def _update_run(run: Dict[str, Any], fields: Dict[str, Any], actor: str,
                project: str) -> Dict[str, Any]:
    """Write only while this caller still owns the persisted execution fence."""
    guarded = dict(fields)
    if run.get("execution_owner_id"):
        guarded["_execution_owner_id"] = run["execution_owner_id"]
        guarded["_execution_fence"] = run.get("execution_fence")
    updated = store.update_external_ci_run(
        run["run_id"], guarded, actor=actor, project=project)
    if updated.get("error") == "external_ci_execution_fence_lost":
        raise ExternalCiError(
            "workflow_poll_failed",
            "external CI execution ownership was superseded",
            updated,
        )
    return updated


def _default_run(args: List[str], cwd: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if not env.get("GH_TOKEN"):
        # Prefer a GitHub App installation token: `gh` and `git` accept one exactly
        # like a PAT, but it is billed to the installation rather than to a human
        # account whose 5,000/hr the whole fleet shares (see github_app_auth). Falls
        # back to the historical PAT chain when no App is configured.
        token = ""
        try:
            import github_app_auth
            # Any repo under the same owner resolves to the same installation, and the
            # mirror only ever touches repos owned by the canonical repo's owner — so
            # the canonical slug is a sufficient hint for installation lookup.
            token = github_app_auth.resolve_token(
                repo=_canonical_repo_hint(),
                env_order=("SWITCHBOARD_CI_GITHUB_TOKEN", "PM_GITHUB_TOKEN", "GITHUB_TOKEN"))
        except Exception:  # noqa: BLE001 — never block a mirror on credential plumbing
            token = ""
        if not token:
            for name in ("SWITCHBOARD_CI_GITHUB_TOKEN", "PM_GITHUB_TOKEN", "GITHUB_TOKEN"):
                if env.get(name):
                    token = env[name]
                    break
        if token:
            env["GH_TOKEN"] = token
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=60, env=env)


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


def _mirror_ref_kind(request: Dict[str, Any]) -> str:
    kind = str((request or {}).get("mirror_ref_kind") or "branch").strip().lower()
    if kind not in {"branch", "tag"}:
        raise ExternalCiError(
            "mirror_sync_failed",
            f"unsupported mirror_ref_kind {kind!r}; expected branch or tag",
        )
    return kind


def _mirror_remote_ref(mirror_name: str, request: Dict[str, Any]) -> str:
    namespace = "tags" if _mirror_ref_kind(request) == "tag" else "heads"
    return f"refs/{namespace}/{mirror_name}"


def _remote_ref_sha(remote_url: str, remote_ref: str, cwd: str,
                    runner: Optional[CommandRunner] = None) -> str:
    readback = _check(
        ["git", "ls-remote", remote_url, remote_ref],
        cwd, "mirror_sync_failed", "mirror ref readback", runner)
    lines = [
        line.split(None, 1) for line in (readback.stdout or "").splitlines()
        if line.strip()
    ]
    return lines[0][0].strip() if lines else ""


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
    # The trusted tag workflow owns its one callback context and deliberately
    # exposes no caller-selectable status_context input. Preserve the legacy
    # injection only for branch-based consumers that still declare it.
    if run.get("status_context") and _mirror_ref_kind(request) != "tag":
        inputs.setdefault("status_context", run.get("status_context"))
    return inputs


def _github_timestamp(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _select_run(runs: Any, triggered_after: float = 0.0,
                source_sha: str = "", purpose: str = "",
                source_ref: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(runs, list):
        return None
    candidates = [r for r in runs if isinstance(r, dict)]
    if not candidates:
        return None
    expected = [
        value for value in (source_sha, purpose, source_ref)
        if str(value or "").strip()
    ]
    exact = []
    for candidate in candidates:
        title = str(candidate.get("displayTitle") or candidate.get("name") or "")
        if expected and not all(str(value) in title for value in expected):
            continue
        created_at = _github_timestamp(candidate.get("createdAt"))
        if triggered_after and (not created_at or created_at < triggered_after):
            continue
        exact.append(candidate)
    if not exact:
        return None
    # GitHub normally returns newest first. Sort explicitly so tests and alternate
    # adapters cannot make an older matching run authoritative.
    return max(exact, key=lambda item: (
        _github_timestamp(item.get("createdAt")),
        int(item.get("databaseId") or item.get("id") or 0),
    ))


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
    updated = _update_run(
        run,
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


def _cleanup_terminal_mirror_branch(run: Dict[str, Any], source_path: str,
                                    request: Dict[str, Any], actor: str, project: str,
                                    runner: Optional[CommandRunner]) -> Dict[str, Any]:
    """Best-effort CI-11 cleanup without changing the workflow verdict."""
    branch = run.get("mirror_branch") or ""
    mirror_repo = run.get("mirror_repo") or ""
    remote_ref = _mirror_remote_ref(branch, request)
    cleanup: Dict[str, Any] = {
        "attempted": True,
        "mirror_repo": mirror_repo,
        "mirror_branch": branch,
        "mirror_ref": remote_ref,
        "mirror_ref_kind": _mirror_ref_kind(request),
    }
    try:
        deleted = _check(
            ["git", "push", _mirror_url(mirror_repo, request.get("mirror_remote_url") or ""),
             f":{remote_ref}"],
            source_path, "mirror_cleanup_failed", "terminal mirror branch cleanup", runner)
        cleanup.update({
            "status": "deleted",
            "stdout": (deleted.stdout or "").strip(),
            "stderr": (deleted.stderr or "").strip(),
        })
    except ExternalCiError as exc:
        cleanup.update({"status": "failed", "error": exc.message, "detail": exc.result})
    store.append_activity(
        "external_ci.branch_cleanup",
        actor,
        {"run_id": run.get("run_id"), **cleanup},
        task_id=run.get("task_id"),
        project=project,
    )
    was_ok = run.get("ok")
    updated = _update_run(
        run,
        {"result": {**(run.get("result") or {}), "branch_cleanup": cleanup}},
        actor=actor,
        project=project,
    )
    if was_ok is not None:
        updated["ok"] = was_ok
    return updated


def _should_cleanup_terminal_ref(request: Dict[str, Any], result: Dict[str, Any]) -> bool:
    """Clean disposable refs only after a conclusive CI verdict.

    GitHub can report a workflow cancelled before a queued job has stopped
    starting. Retaining the exact ref for abortive terminal states prevents that
    late actions/checkout from racing cleanup and leaves the audited retry path
    a usable ref.
    """
    return bool((request or {}).get("cleanup_mirror_branch")) and (
        (result or {}).get("status") in {"success", "failure"}
    )


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
    # Only meaningful when we are about to shell out to the real git/gh. A caller that
    # injected its own CommandRunner has replaced that layer outright, so an environment
    # credential check says nothing about whether ITS commands can authenticate.
    credential_error = "" if runner is not None else _github_credential_error()
    if credential_error:
        # Fail BEFORE creating/claiming a run. Discovering this mid-push is what left a
        # run in `error`, and until BUG-180 that permanently poisoned the SHA. The
        # classic trigger is running this by hand as root (`sudo`) instead of as the
        # service account: the daemon has git credentials, root does not, so the push
        # dies with "could not read Username for 'https://github.com'". Refusing up
        # front costs nothing and leaves no wreckage to clean up.
        return {"error": credential_error,
                "failure_class": "mirror_sync_failed",
                "skip_reason": "github_credentials_unavailable",
                "dispatched": False, "ok": False}
    request = dict(request or {})
    # The execution owner is server-owned identity. The REST route forwards the
    # caller's raw body, so accepting an owner from it would let a caller choose
    # or collide with another dispatcher's identity. Drop any supplied value.
    request.pop("_execution_owner_id", None)
    execution_owner_id = "ecio-" + uuid.uuid4().hex
    execution_lease_seconds = store.clamp_external_ci_lease_seconds(
        request.pop("execution_lease_seconds",
                    store.EXTERNAL_CI_EXECUTION_LEASE_SECONDS))
    execution_now = now_fn()
    run = store.create_external_ci_run(
        {**request,
         "_execution_owner_id": execution_owner_id,
         "_execution_lease_seconds": execution_lease_seconds,
         "_execution_now": execution_now},
        actor=actor, project=project)
    if run.get("error"):
        return run
    if run.get("idempotent"):
        # BUG-180: clear an ABORTIVE terminal state BEFORE trying to acquire execution.
        # A completed run will not hand out an execution lease, so an aborted run fell
        # into the "coalesced" return below and was reported as a settled result — the
        # SHA could never be retried. Reset it here so the acquire path sees a live run
        # again. `error`/`cancelled` produced no verdict, so nothing is being discarded;
        # `success`/`failure` are left strictly alone.
        existing_status = str(run.get("status") or "")
        if existing_status in ABORTIVE_TERMINAL_STATUSES and _truthy(
                request.get("retry_terminal_error")):
            reset = store.reset_external_ci_run_for_retry(
                run["run_id"], actor=actor, project=project)
            if reset.get("error") or not reset.get("reset"):
                return {**run, "ok": False, "retryable": True,
                        "skip_reason": "retry_reset_failed",
                        "error": reset.get("error") or reset.get("reset_refused")}
            run = {**run, **reset, "retried_from_terminal": True}
        acquired = store.acquire_external_ci_execution(
            run["run_id"], execution_owner_id,
            lease_seconds=execution_lease_seconds,
            now=execution_now, actor=actor, project=project)
        if not acquired.get("execution_acquired"):
            acquired["coalesced"] = True
            acquired["ok"] = acquired.get("status") not in {
                "failure", "cancelled", "error"
            }
            if acquired.get("status") in ABORTIVE_TERMINAL_STATUSES:
                # Aborted, produced no CI status, and we were not asked to retry. Name
                # it instead of returning a settled-looking result.
                acquired["retryable"] = True
                acquired["skip_reason"] = "previous_run_failed"
                acquired["error"] = acquired.get("error") or (
                    f"previous mirror run {acquired.get('run_id')} ended in "
                    f"{acquired.get('status')} "
                    f"({acquired.get('failure_class') or 'unknown'}) and produced no CI "
                    "status; pass retry_terminal_error to re-run this SHA")
            return acquired
        run = {**run, **acquired} if run.get("retried_from_terminal") else acquired
    if run.get("status") in store.EXTERNAL_CI_TERMINAL_STATUSES:
        run["resumed_terminal"] = True
        # Not all terminal states mean the same thing, and conflating them wedged the
        # merge queue (BUG-180):
        #   success / failure -> CI really ran and published a verdict. The required
        #                        status exists on the SHA. Returning the run is correct.
        #   error / cancelled -> the run ABORTED before producing any verdict (mirror
        #                        sync failed, token expired, GitHub 5xx). NO status was
        #                        ever posted and none ever will be.
        # A PR head escapes the second case by pushing a new commit. A merge-group head
        # SHA is generated by GitHub and cannot change, so the queue waited forever for
        # a status that could not arrive — while every re-dispatch handed back this dead
        # run WITHOUT setting `ok`, which callers read as success.
        run["retryable"] = run.get("status") in ABORTIVE_TERMINAL_STATUSES
        if run["retryable"]:
            # Only the abortive branch changes shape. A success/failure resume keeps its
            # existing contract untouched — CI genuinely reported, and callers that
            # already handle a red verdict must not start seeing it as "not dispatched".
            run["ok"] = False
            if not _truthy(request.get("retry_terminal_error")):
                # Say so plainly instead of returning a success-shaped no-op.
                run["skip_reason"] = "previous_run_failed"
                run["error"] = run.get("error") or (
                    f"previous mirror run {run.get('run_id')} ended in "
                    f"{run.get('status')} ({run.get('failure_class') or 'unknown'}) "
                    "and produced no CI status; pass retry_terminal_error to re-run")
                return run
            # Opted-in retry: the SHA is immutable, so reuse this run row rather than
            # forking a second one, and re-execute it from a clean non-terminal state.
            reset = store.update_external_ci_run(
                run["run_id"],
                {"status": "pending", "conclusion": "", "failure_class": "",
                 "failure_reason": ""},
                actor=actor, project=project)
            if reset.get("error"):
                run["skip_reason"] = "retry_reset_failed"
                run["error"] = reset["error"]
                return run
            run = {**reset, "retried_from_terminal": True}
        else:
            return run
    try:
        result = _execute_run(run, source_path, actor, project, runner, sleep_fn, now_fn,
                              request)
    except ExternalCiError as e:
        result = _update_failure(run, e.failure_class, e.message, e.result,
                                 actor=actor, project=project)
    if _should_cleanup_terminal_ref(request or {}, result):
        result = _cleanup_terminal_mirror_branch(
            result, source_path, request or {}, actor, project, runner)
    return result


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
                         poll_interval_seconds, timeout_seconds, run.get("request") or {})
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
    source_fetch_ref = str(request.get("source_fetch_ref") or "").strip()
    if source_fetch_ref:
        source_remote = str(request.get("source_remote") or "origin").strip()
        _check(["git", "fetch", "--no-tags", source_remote, source_fetch_ref],
               source_path, "mirror_sync_failed", "source SHA fetch", runner)
    resolved = _check(["git", "rev-parse", "--verify", f"{source_sha}^{{commit}}"],
                      source_path, "mirror_sync_failed", "source SHA validation", runner)
    resolved_sha = (resolved.stdout or "").strip() or source_sha

    remote_ref = _mirror_remote_ref(mirror_branch, request)
    mirror_ref_kind = _mirror_ref_kind(request)
    remote_sha = _remote_ref_sha(
        mirror_remote_url, remote_ref, source_path, runner)
    if remote_sha and remote_sha != resolved_sha:
        raise ExternalCiError(
            "mirror_sync_failed",
            f"{remote_ref} already resolves to {remote_sha}, expected {resolved_sha}",
            {
                "mirror_repo": mirror_repo,
                "mirror_branch": mirror_branch,
                "mirror_ref": remote_ref,
                "mirror_ref_kind": mirror_ref_kind,
                "remote_sha": remote_sha,
                "expected_sha": resolved_sha,
            },
        )
    reused_ref = remote_sha == resolved_sha
    if reused_ref:
        push = subprocess.CompletedProcess(
            ["git", "push", mirror_remote_url], 0, "", "existing exact ref reused")
    else:
        push_ref = f"{resolved_sha}:{remote_ref}"
        try:
            push = _check(["git", "push", mirror_remote_url, push_ref],
                          source_path, "mirror_sync_failed", "mirror push", runner)
        except ExternalCiError:
            # Close the ls-remote/push race: another caller may have created
            # the deterministic ref after our first readback.
            raced_sha = _remote_ref_sha(
                mirror_remote_url, remote_ref, source_path, runner)
            if raced_sha != resolved_sha:
                raise
            reused_ref = True
            push = subprocess.CompletedProcess(
                ["git", "push", mirror_remote_url], 0, "",
                "concurrent exact ref reused")
    mirrored = _update_run(
        run,
        {
            "status": "mirrored",
            "result": {
                "source_repo": run.get("source_repo"),
                "source_sha": source_sha,
                "resolved_source_sha": resolved_sha,
                "source_fetch_ref": source_fetch_ref or None,
                "ci_repo": mirror_repo,
                "mirror_remote_url": mirror_remote_url,
                "mirror_branch": mirror_branch,
                "mirror_ref": remote_ref,
                "mirror_ref_kind": mirror_ref_kind,
                "mirror_ref_reused": reused_ref,
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
                "mirror_ref": remote_ref,
                "mirror_ref_kind": mirror_ref_kind,
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
        updated = _update_run(
            run,
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
                         float(request.get("timeout_seconds") or 1800), request)

    workflow_ref = str(request.get("workflow_ref") or mirror_branch).strip()
    trigger_args = ["gh", "workflow", "run", workflow, "--repo", mirror_repo,
                    "--ref", workflow_ref]
    trigger_args.extend(_workflow_inputs_args(_workflow_inputs_for_run(run, request)))
    # GitHub's createdAt is second-resolution. Floor the local fence to the same
    # precision so a run created in the dispatch second is not rejected.
    triggered_after = float(int(now_fn()))
    trigger = _check(trigger_args, source_path, "workflow_trigger_failed",
                     "workflow dispatch", runner)
    updated = _update_run(
        run,
        {
            "status": "triggered",
            "result": {
                **result_payload,
                "workflow_ref": workflow_ref,
                "workflow_dispatch_stdout": (trigger.stdout or "").strip(),
                "workflow_dispatch_stderr": (trigger.stderr or "").strip(),
            },
        },
        actor=actor,
        project=project,
    )
    if not poll_after_push:
        updated["ok"] = True
        return updated
    return _poll_run({**run, "status": "triggered"}, source_path, actor, project,
                     runner, sleep_fn, now_fn,
                     float(request.get("poll_interval_seconds") or 15),
                     float(request.get("timeout_seconds") or 1800),
                     {**request, "_triggered_after": triggered_after})


def _poll_run(run: Dict[str, Any], source_path: str, actor: str,
              project: str, runner: Optional[CommandRunner],
              sleep_fn: SleepFn, now_fn: NowFn,
              poll_interval_seconds: float, timeout_seconds: float,
              request: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    deadline = now_fn() + max(1.0, timeout_seconds)
    mirror_repo = run["mirror_repo"]
    mirror_branch = run["mirror_branch"]
    workflow = run["workflow"]
    workflow_ref = str((request or {}).get("workflow_ref") or mirror_branch).strip()
    workflow_inputs = _workflow_inputs_for_run(run, request or {})
    triggered_after = float((request or {}).get("_triggered_after") or 0.0)
    selected: Optional[Dict[str, Any]] = None
    while now_fn() <= deadline:
        runs = _json(
            ["gh", "run", "list", "--repo", mirror_repo, "--workflow", workflow,
             "--branch", workflow_ref,
             "--json",
             "databaseId,status,conclusion,url,headSha,createdAt,updatedAt,displayTitle,event",
             "--limit", "20"],
            source_path,
            "workflow_poll_failed",
            "workflow run list",
            runner,
        )
        selected = _select_run(
            runs,
            triggered_after=triggered_after,
            source_sha=run["source_sha"],
            purpose=str(workflow_inputs.get("purpose") or ""),
            source_ref=str(workflow_inputs.get("source_ref") or ""),
        )
        if not selected:
            _update_run(
                run, {"status": "triggered", "result": {"poll": "no_run_yet"}},
                actor=actor, project=project)
            sleep_fn(max(0.1, poll_interval_seconds))
            continue
        status = (selected.get("status") or "").lower()
        conclusion = (selected.get("conclusion") or "").lower()
        run_id = selected.get("databaseId") or selected.get("id")
        run_url = selected.get("url") or _run_url(mirror_repo, run_id)
        logs_url = f"{run_url}/logs" if run_url else None
        if status and status != "completed":
            _update_run(
                run,
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
            "tested_public_sha": run["source_sha"],
            "workflow_head_sha": selected.get("headSha"),
            "workflow_ref": workflow_ref,
            "source_repo": run.get("source_repo"),
            "source_sha": run["source_sha"],
            "ci_repo": mirror_repo,
            "mirror_repo": mirror_repo,
            "mirror_branch": mirror_branch,
            "status_context": run.get("status_context"),
        }
        if conclusion == "success":
            updated = _update_run(
                run,
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
