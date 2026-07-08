#!/usr/bin/env python3
"""VM-backed Switchboard PR conformance gate.

GitHub Actions is the preferred CI surface, but a repo or org policy failure can
make workflow runs die before any job is created. This runner gives Switchboard
an equivalent PR-visible gate: run the strict local suite in an isolated
worktree, then post a GitHub commit status to the PR head SHA.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# This script lives in scripts/, so Python's script-dir sys.path entry does not
# cover repo-root modules; without this the systemd ci-gate unit dies on import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import external_ci_mirror  # noqa: E402
import pr_provenance_gate  # noqa: E402
import review_preflight  # noqa: E402
import store  # noqa: E402


DEFAULT_REPO = "6th-Element-Labs/projectplanner"
DEFAULT_CONTEXT = "Switchboard CI / VM gate"
DEFAULT_CLAIM_CONTEXT = "Switchboard / claim gate"
DEFAULT_WORKDIR = "/var/lib/projectplanner/ci-gate"
MIN_PYTHON_VERSION = (3, 10)
# Board project whose repo_topology decides where CI runs: a configured public_ci
# sandbox (free GitHub-hosted runners) vs. the legacy local venv on this box.
SWITCHBOARD_CI_PROJECT = (os.environ.get("SWITCHBOARD_CI_PROJECT") or "switchboard").strip() or "switchboard"
# external_ci_mirror failure_class values that mean the suite actually RAN and was red
# (a genuine gate failure). Every other failure_class is an infra/dispatch problem where
# the mirror produced no test verdict -> fall back to the local suite instead of hard-failing.
_EXTERNAL_CI_TEST_FAILURE_CLASSES = {"test", "test_failed", "tests_failed"}


class GateError(RuntimeError):
    pass


def _repo() -> str:
    return (
        os.environ.get("SWITCHBOARD_CI_REPO")
        or os.environ.get("PM_GITHUB_REPO_SWITCHBOARD")
        or os.environ.get("PM_GITHUB_REPO")
        or os.environ.get("GITHUB_REPOSITORY")
        or DEFAULT_REPO
    ).strip()


def _token() -> str:
    return (
        os.environ.get("SWITCHBOARD_CI_GITHUB_TOKEN")
        or os.environ.get("PM_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    ).strip()


def _run(cmd: List[str], *, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
         stdout=None, check: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, text=True,
                          stdout=stdout, stderr=subprocess.STDOUT if stdout else None,
                          check=check, timeout=timeout)


def _python_info(executable: str) -> Optional[Dict[str, Any]]:
    if not executable:
        return None
    try:
        proc = subprocess.run(
            [executable, "-c",
             "import json,sys; print(json.dumps({'executable': sys.executable, "
             "'version': list(sys.version_info[:3])}))"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        info = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return None
    version = tuple(int(x) for x in (info.get("version") or [])[:2])
    if len(version) < 2:
        return None
    info["supported"] = version >= MIN_PYTHON_VERSION
    info["version_text"] = ".".join(str(x) for x in (info.get("version") or []))
    return info


def _python_candidates(source_repo: Path, explicit: str = "") -> Iterable[Dict[str, str]]:
    explicit = (explicit or os.environ.get("SWITCHBOARD_CI_PYTHON") or "").strip()
    if explicit:
        yield {"source": "explicit", "path": explicit}
    env_python = (os.environ.get("PYTHON") or "").strip()
    if env_python and env_python != explicit:
        yield {"source": "PYTHON", "path": env_python}
    repo_python = source_repo / ".venv" / "bin" / "python"
    yield {"source": "repo_venv", "path": str(repo_python)}
    for name in ("python3.12", "python3.11", "python3.10", "python3"):
        resolved = shutil.which(name) or ""
        if resolved:
            yield {"source": name, "path": resolved}


def select_ci_python(source_repo: Path, explicit: str = "") -> Dict[str, Any]:
    seen = set()
    rejected = []
    for candidate in _python_candidates(source_repo, explicit=explicit):
        path = candidate["path"]
        if path in seen:
            continue
        seen.add(path)
        info = _python_info(path)
        if not info:
            rejected.append({**candidate, "reason": "not_executable"})
            continue
        record = {**candidate, **info}
        if record.get("supported"):
            return record
        rejected.append({**record, "reason": "unsupported_version"})
    details = ", ".join(
        f"{r.get('source')}={r.get('path')}:{r.get('version_text') or r.get('reason')}"
        for r in rejected
    )
    raise GateError(
        "No supported Python runtime found for Switchboard VM gate. "
        "Need Python 3.10+ because strict CI installs mcp>=1.9. "
        "Set SWITCHBOARD_CI_PYTHON to a supported interpreter or provision /opt/projectplanner/.venv. "
        f"Checked: {details or 'no candidates'}"
    )


def _github_request(method: str, path: str, *, token: str, body: Optional[Dict[str, Any]] = None) -> Any:
    url = path if path.startswith("https://") else f"https://api.github.com/{path.lstrip('/')}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GateError(f"GitHub API {method} {url} failed: HTTP {exc.code} {detail}") from exc


def _status_description(text: str) -> str:
    text = " ".join(text.split())
    return text[:140]


def post_status(repo: str, sha: str, state: str, *, context: str, description: str,
                target_url: str = "", token: str) -> Any:
    if not token:
        raise GateError("A GitHub token is required to post PR gate status.")
    payload: Dict[str, Any] = {
        "state": state,
        "context": context,
        "description": _status_description(description),
    }
    if target_url:
        payload["target_url"] = target_url
    return _github_request("POST", f"repos/{repo}/statuses/{sha}", token=token, body=payload)


def list_open_prs(repo: str, *, token: str) -> List[Dict[str, Any]]:
    return _github_request("GET", f"repos/{repo}/pulls?state=open&per_page=100", token=token)


def get_pr(repo: str, number: int, *, token: str) -> Dict[str, Any]:
    return _github_request("GET", f"repos/{repo}/pulls/{int(number)}", token=token)


def list_pr_files(repo: str, number: int, *, token: str) -> List[str]:
    """Changed file paths on a PR (first page) — used only for the docs-only exemption."""
    try:
        rows = _github_request(
            "GET", f"repos/{repo}/pulls/{int(number)}/files?per_page=100", token=token)
    except GateError:
        return []
    return [str(row.get("filename") or "") for row in rows or [] if row.get("filename")]


def run_claim_gate_for_pr(pr: Dict[str, Any], *, repo: str, token: str,
                          context: str, mode: str) -> Optional[Dict[str, Any]]:
    """SESSION-12 provenance gate: post a second, independent commit status that
    checks whether a fleet PR is backed by a claimed task / Work Session. Reads the
    production board (this process' store), never the PR worktree. mode is resolved
    per-repo by the caller (primary repo uses gate_mode(); others default to warn)."""
    if mode == "off":
        return None
    number = int(pr["number"])
    sha = pr["head"]["sha"]
    pr_url = pr.get("html_url", f"https://github.com/{repo}/pull/{number}")
    changed_paths = list_pr_files(repo, number, token=token)
    verdict = pr_provenance_gate.evaluate_pr_provenance(
        pr, repo=repo, mode=mode, changed_paths=changed_paths)
    post_status(repo, sha, verdict["state"], context=context,
                description=verdict["context_description"], target_url=pr_url, token=token)
    return {"repo": repo, "pr": number, "sha": sha, "context": context,
            "state": verdict["state"], "reason": verdict.get("reason"),
            "would_block": verdict.get("would_block"), "mode": mode}


def _claim_gate_targets(args: argparse.Namespace, primary_repo: str, token: str):
    """Yield (repo, pr, mode) to claim-gate. For an explicit --pr set, only the named
    PRs on the primary repo. Otherwise every project's canonical repo (registry-driven),
    so a new project is covered automatically the moment it sets a canonical repo."""
    skip_drafts = os.environ.get("SWITCHBOARD_CI_SKIP_DRAFTS", "1") != "0"
    if args.pr:
        mode = pr_provenance_gate.resolve_mode(primary_repo, primary_repo)
        for number in args.pr:
            yield primary_repo, get_pr(primary_repo, number, token=token), mode
        return
    repos = store.list_canonical_repos()  # {repo: [project_ids]}
    ordered = [primary_repo] + [r for r in sorted(repos) if r != primary_repo]
    seen = set()
    for repo in ordered:
        if not repo or repo in seen:
            continue
        seen.add(repo)
        mode = pr_provenance_gate.resolve_mode(repo, primary_repo)
        if mode == "off":
            continue
        try:
            prs = list_open_prs(repo, token=token)
        except GateError as exc:
            print(json.dumps({"repo": repo, "context": "claim", "state": "error",
                              "error": str(exc)}, sort_keys=True))
            continue
        for pr in prs:
            if pr.get("draft") and skip_drafts:
                continue
            yield repo, pr, mode


def _origin_url(source_repo: Path, repo: str) -> str:
    explicit = os.environ.get("SWITCHBOARD_CI_GIT_REMOTE", "").strip()
    if explicit:
        return explicit
    try:
        out = subprocess.check_output(["git", "remote", "get-url", "origin"],
                                      cwd=str(source_repo), text=True).strip()
        if out:
            return out
    except subprocess.CalledProcessError:
        pass
    return f"git@github.com:{repo}.git"


def _ensure_cache_repo(root: Path, source_repo: Path, repo: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    cache = root / "repo.git"
    origin = _origin_url(source_repo, repo)
    if not cache.exists():
        _run(["git", "clone", "--bare", origin, str(cache)])
    else:
        _run(["git", "-C", str(cache), "remote", "set-url", "origin", origin])
    _run(["git", "-C", str(cache), "fetch", "--prune", "origin",
          "+refs/heads/*:refs/heads/*",
          "+refs/pull/*/head:refs/pull/*/head",
          "+refs/pull/*/merge:refs/pull/*/merge"])
    return cache


def _run_tag(number: int, sha: str) -> str:
    return f"pr-{int(number)}-{sha[:12]}-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _prepare_worktree(cache: Path, root: Path, pr: Dict[str, Any], run_tag: str) -> Path:
    number = int(pr["number"])
    merge_ref = f"refs/pull/{number}/merge"
    merge = subprocess.run(["git", "-C", str(cache), "rev-parse", "--verify",
                            f"{merge_ref}^{{commit}}"],
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if merge.returncode != 0:
        raise GateError(f"PR #{number} has no merge ref; rebase or resolve conflicts before gating.")
    test_sha = merge.stdout.strip()
    run_dir = root / "runs" / run_tag
    if run_dir.exists():
        _run(["git", "-C", str(cache), "worktree", "remove", "--force", str(run_dir)],
             check=False)
        shutil.rmtree(run_dir, ignore_errors=True)
    _run(["git", "-C", str(cache), "worktree", "add", "--detach", str(run_dir), test_sha])
    return run_dir


def _cleanup_worktree(cache: Path, run_dir: Path) -> None:
    _run(["git", "-C", str(cache), "worktree", "remove", "--force", str(run_dir)],
         check=False)
    shutil.rmtree(run_dir, ignore_errors=True)


def _write_preflight_log(log_path: Path, worktree: Path, preflight: Dict[str, Any]) -> None:
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"Switchboard CI VM gate started at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        log.write(f"worktree={worktree}\n\n")
        log.write(review_preflight.format_preflight_header(preflight))
        log.write("\n")


def _pr_preflight(worktree: Path, pr: Dict[str, Any], *, repo: str) -> Dict[str, Any]:
    base = pr.get("base") or {}
    upstream = (base.get("sha") or base.get("ref") or "").strip()
    return review_preflight.run_git_review_preflight(
        worktree,
        target_ref="HEAD",
        upstream_ref=upstream,
        intended_project="switchboard",
        intended_branch=(base.get("ref") or ""),
        require_clean=True,
        allow_dirty=False,
        allow_behind=False,
    )


def run_switchboard_gate(worktree: Path, log_path: Path, *, timeout_s: int,
                         preflight: Optional[Dict[str, Any]] = None,
                         python_runtime: Optional[Dict[str, Any]] = None) -> None:
    env = os.environ.copy()
    # The systemd gate unit inherits the production .env (board DB paths,
    # feature flags, PM_TOP_LEVEL_PROJECTS, ...). The suite must run hermetic —
    # every test pins the PM_* state it needs — so drop inherited PM_* config
    # wholesale instead of chasing individual leaks per test.
    for key in [k for k in env if k.startswith("PM_")]:
        env.pop(key, None)
    venv_python = worktree / ".venv" / "bin" / "python"
    python_runtime = python_runtime or select_ci_python(worktree)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"Switchboard CI VM gate started at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        log.write(f"worktree={worktree}\n\n")
        if preflight:
            log.write(review_preflight.format_preflight_header(preflight))
            log.write("\n")
        log.write("Switchboard CI Python runtime\n")
        log.write(f"source={python_runtime.get('source')}\n")
        log.write(f"executable={python_runtime.get('executable') or python_runtime.get('path')}\n")
        log.write(f"version={python_runtime.get('version_text')}\n\n")
        log.flush()
        _run([python_runtime["path"], "-m", "venv", ".venv"], cwd=worktree, stdout=log)
        _run([str(venv_python), "-m", "pip", "install", "--disable-pip-version-check",
              "-r", "requirements.txt"], cwd=worktree, stdout=log, timeout=timeout_s)
        env.update({
            "PYTHON": str(venv_python),
            "SWITCHBOARD_CI_STRICT": "1",
            "SWITCHBOARD_CI_REQUIRE_NODE": "1",
        })
        _run(["scripts/switchboard_ci.sh"], cwd=worktree, env=env, stdout=log,
             timeout=timeout_s)


def _sandbox_ci_role(project: str) -> Dict[str, Any]:
    """Return the project's configured public_ci role (verification sandbox), or {}
    when none is set — in which case the gate falls back to the local venv suite."""
    try:
        topology = store.get_project_repo_topology(project=project)
    except Exception:
        return {}
    role = (topology.get("roles") or {}).get("public_ci") or {}
    if role.get("configured") and (role.get("repo") or "").strip():
        return role
    return {}


def _verify_on_external_ci_mirror(worktree: Path, log_path: Path, *, project: str,
                                  number: int, token: str, timeout_s: int):
    """Verify the PR's merge commit via the first-class ``external_ci_mirror`` runner.

    Returns ("success", result) when the mirror ran the suite green, or
    ("unavailable", result) when the mirror could not produce a verdict (dispatch/sync/
    poll error) — the caller then falls back to the local suite. Raises GateError only on
    a genuine test failure (the suite ran and was red).

    Pushes the exact merge SHA to the project's ``public_ci`` sandbox, dispatches the
    workflow, polls to a terminal status, and records an ``external_ci_run`` with a
    structured ``failure_class`` (mirror_sync/workflow_trigger/poll/test) + run_url
    evidence. Repos and status context are resolved from ``repo_topology``. Heavy CI
    never runs on this box. Fails closed. See docs/CI-STRATEGY.md and
    docs/EXTERNAL-CI-MIRROR-SPEC.md."""
    if not token:
        raise GateError("A GitHub token is required to drive the external CI mirror.")
    # external_ci_mirror shells out to `gh`, which authenticates from GH_TOKEN.
    os.environ["GH_TOKEN"] = token
    merge_sha = subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True).strip()
    workflow = (os.environ.get("SWITCHBOARD_CI_WORKFLOW") or "backend-tests.yml").strip()
    request = {
        "source_project": project,
        "source_sha": merge_sha,
        "source_branch": f"pr-{int(number)}",
        "workflow": workflow,
        "request": {"timeout_seconds": timeout_s},
    }
    try:
        result = external_ci_mirror.request_external_ci_mirror_run(
            request, str(worktree), actor="switchboard-ci/vm-gate", project=project)
    except Exception as exc:
        # The mirror machinery itself failed (e.g. transient "database is locked" on the
        # contended box, or a gh/network error) — it produced no test verdict. Treat exactly
        # like a returned infra error: unavailable, so the caller falls back to the local
        # suite instead of hard-reding the PR. External-CI plumbing must never fail the gate.
        result = {"error": str(exc), "failure_class": "mirror_exception"}
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"external_ci_mirror source_sha={merge_sha} workflow={workflow}\n")
        log.write(json.dumps(result, indent=2, default=str)[:4000] + "\n")
    status = (result.get("status") or "").strip().lower()
    fclass = (result.get("failure_class") or "").strip().lower()
    if status == "success":
        return "success", result
    if fclass in _EXTERNAL_CI_TEST_FAILURE_CLASSES:
        # The mirror actually ran the suite and it was red — a genuine gate failure.
        raise GateError(
            f"external CI mirror not green (status={status}, conclusion={result.get('conclusion')}, "
            f"class={fclass}) — {result.get('run_url') or ''}")
    # Anything else (mirror sync / workflow dispatch / poll error, or a bare error with no
    # test verdict) means the mirror could not produce a result — infra, not a test failure.
    # Return 'unavailable' so the caller falls back to the local suite: a gate that cannot
    # verify is worse than one that runs on the box (ADR-0006 — evidence-only external CI is
    # not the sole source of truth).
    return "unavailable", result


def run_gate_for_pr(pr: Dict[str, Any], *, repo: str, token: str, context: str,
                    work_root: Path, source_repo: Path, timeout_s: int,
                    python_executable: str = "",
                    keep_worktree: bool = False) -> Dict[str, Any]:
    number = int(pr["number"])
    sha = pr["head"]["sha"]
    pr_url = pr.get("html_url", f"https://github.com/{repo}/pull/{number}")
    run_tag = _run_tag(number, sha)
    logs = work_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{run_tag}.log"
    post_status(repo, sha, "pending", context=context,
                description="Switchboard VM gate is running", target_url=pr_url,
                token=token)
    cache = None
    run_dir = None
    try:
        # Inside the try so a conflicted PR (no merge ref) or a cache/worktree
        # failure posts a red status and returns instead of raising uncaught —
        # an uncaught error here aborts the whole gate run, fails the systemd
        # unit, and stops gating every other PR until the bad PR is closed.
        cache = _ensure_cache_repo(work_root, source_repo, repo)
        run_dir = _prepare_worktree(cache, work_root, pr, run_tag)
        preflight = _pr_preflight(run_dir, pr, repo=repo)
        if preflight.get("status") != "pass":
            _write_preflight_log(log_path, run_dir, preflight)
            raise GateError("Switchboard review preflight failed: " +
                            "; ".join(f.get("code", "unknown") for f in preflight.get("findings", [])))
        ci_role = _sandbox_ci_role(SWITCHBOARD_CI_PROJECT)
        ran_external = False
        if ci_role:
            # Topology declares a public_ci sandbox: verify on the first-class
            # external_ci_mirror runner (free GitHub-hosted runners), keeping heavy CI
            # off this box. Provenance preflight above is unchanged. See docs/CI-STRATEGY.md.
            outcome, _ext = _verify_on_external_ci_mirror(
                run_dir, log_path, project=SWITCHBOARD_CI_PROJECT,
                number=number, token=token, timeout_s=timeout_s)
            ran_external = outcome == "success"
            if outcome != "success":
                # Mirror could not produce a verdict (e.g. workflow dispatch/sync error).
                # Do not hard-fail the whole gate on an evidence-only mirror outage — fall
                # back to the local suite so PRs still get real verification.
                with log_path.open("a", encoding="utf-8") as log:
                    log.write("\nexternal CI mirror unavailable "
                              f"(class={_ext.get('failure_class')}); falling back to local suite.\n")
        if not ran_external:
            python_runtime = select_ci_python(source_repo, explicit=python_executable)
            run_switchboard_gate(run_dir, log_path, timeout_s=timeout_s,
                                 preflight=preflight, python_runtime=python_runtime)
        post_status(repo, sha, "success", context=context,
                    description="Switchboard VM gate passed", target_url=pr_url,
                    token=token)
        return {"pr": number, "sha": sha, "state": "success", "log": str(log_path),
                "preflight": preflight}
    except Exception as exc:
        post_status(repo, sha, "failure", context=context,
                    description=f"Switchboard VM gate failed: {exc}", target_url=pr_url,
                    token=token)
        return {"pr": number, "sha": sha, "state": "failure", "log": str(log_path),
                "error": str(exc)}
    finally:
        if run_dir is not None and cache is not None and not keep_worktree:
            _cleanup_worktree(cache, run_dir)


def _select_prs(args: argparse.Namespace, repo: str, token: str) -> Iterable[Dict[str, Any]]:
    if args.pr:
        for number in args.pr:
            yield get_pr(repo, number, token=token)
    else:
        yield from list_open_prs(repo, token=token)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Switchboard VM-backed PR gate.")
    parser.add_argument("--pr", type=int, action="append",
                        help="PR number to gate. May be repeated. Defaults to all open PRs.")
    parser.add_argument("--once-open-prs", action="store_true",
                        help="Poll open PRs once. Kept explicit for systemd timer readability.")
    parser.add_argument("--repo", default=_repo())
    parser.add_argument("--context", default=os.environ.get("SWITCHBOARD_CI_STATUS_CONTEXT",
                                                            DEFAULT_CONTEXT))
    parser.add_argument("--claim-context",
                        default=os.environ.get("SWITCHBOARD_CI_CLAIM_STATUS_CONTEXT",
                                               DEFAULT_CLAIM_CONTEXT),
                        help="Commit-status context for the SESSION-12 provenance/claim gate. "
                             "Primary-repo mode via SWITCHBOARD_CI_CLAIM_GATE_MODE (default warn); "
                             "other canonical repos via SWITCHBOARD_CI_CLAIM_GATE_MODE_DEFAULT "
                             "(default warn) or per-repo SWITCHBOARD_CI_CLAIM_GATE_MODES.")
    parser.add_argument("--no-claim-gate", action="store_true",
                        default=os.environ.get("SWITCHBOARD_CI_NO_CLAIM_GATE", "").lower()
                        in ("1", "true", "yes"),
                        help="Skip the registry-wide provenance/claim gate pass.")
    parser.add_argument("--workdir", default=os.environ.get("SWITCHBOARD_CI_WORKDIR",
                                                            DEFAULT_WORKDIR))
    parser.add_argument("--source-repo", default=os.environ.get("SWITCHBOARD_CI_SOURCE_REPO",
                                                               str(Path.cwd())))
    parser.add_argument("--timeout-s", type=int,
                        default=int(os.environ.get("SWITCHBOARD_CI_TIMEOUT_SECONDS", "1800")))
    parser.add_argument("--python", default=os.environ.get("SWITCHBOARD_CI_PYTHON", ""),
                        help="Python 3.10+ interpreter used to create the PR gate venv. "
                             "Defaults to SWITCHBOARD_CI_PYTHON, PYTHON, repo .venv, then python3.12/3.11/3.10.")
    parser.add_argument("--keep-worktree", action="store_true",
                        default=os.environ.get("SWITCHBOARD_CI_KEEP_WORKTREE", "").lower()
                        in ("1", "true", "yes"))
    parser.add_argument("--fail-on-red", action="store_true",
                        help="Return nonzero when any PR gate posts failure. Manual use only; "
                             "systemd timers should stay green when they successfully post red statuses.")
    args = parser.parse_args(argv)

    token = _token()
    if not token:
        print("ERROR: set PM_GITHUB_TOKEN, GITHUB_TOKEN, or SWITCHBOARD_CI_GITHUB_TOKEN.",
              file=sys.stderr)
        return 2
    root = Path(args.workdir)
    source_repo = Path(args.source_repo)
    results = []

    # Pass 1 — SESSION-12 provenance/claim gate across EVERY project's canonical repo
    # (registry-driven). Board-only, no code execution, so it is safe for any repo and a
    # new project is covered the moment it configures a canonical repo. Independent of the
    # test gate: a failure here never aborts pass 2.
    if not args.no_claim_gate:
        for repo, pr, mode in _claim_gate_targets(args, args.repo, token):
            try:
                claim_result = run_claim_gate_for_pr(
                    pr, repo=repo, token=token, context=args.claim_context, mode=mode)
                if claim_result:
                    print(json.dumps(claim_result, sort_keys=True))
                    results.append(claim_result)
            except Exception as exc:  # pragma: no cover - defensive
                print(json.dumps({"repo": repo, "pr": pr.get("number"),
                                  "context": args.claim_context, "state": "error",
                                  "error": str(exc)}, sort_keys=True))

    # Pass 2 — VM test gate. This runs the projectplanner test suite in a worktree, so it
    # only applies to the primary (projectplanner) repo; other repos run their own CI.
    for pr in _select_prs(args, args.repo, token):
        if pr.get("draft") and os.environ.get("SWITCHBOARD_CI_SKIP_DRAFTS", "1") != "0":
            continue
        try:
            result = run_gate_for_pr(pr, repo=args.repo, token=token, context=args.context,
                                     work_root=root, source_repo=source_repo,
                                     timeout_s=args.timeout_s,
                                     python_executable=args.python,
                                     keep_worktree=args.keep_worktree)
        except Exception as exc:  # pragma: no cover - defensive; one PR must not abort the run
            result = {"pr": pr.get("number"), "context": args.context, "state": "error",
                      "error": str(exc)}
        print(json.dumps(result, sort_keys=True))
        results.append(result)
    failed = [r for r in results if r.get("state") not in ("success", None)]
    return 1 if args.fail_on_red and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
