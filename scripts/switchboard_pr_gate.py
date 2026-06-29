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


DEFAULT_REPO = "6th-Element-Labs/projectplanner"
DEFAULT_CONTEXT = "Switchboard CI / VM gate"
DEFAULT_WORKDIR = "/var/lib/projectplanner/ci-gate"


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


def run_switchboard_gate(worktree: Path, log_path: Path, *, timeout_s: int) -> None:
    env = os.environ.copy()
    venv_python = worktree / ".venv" / "bin" / "python"
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"Switchboard CI VM gate started at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        log.write(f"worktree={worktree}\n\n")
        log.flush()
        _run(["python3", "-m", "venv", ".venv"], cwd=worktree, stdout=log)
        _run([str(venv_python), "-m", "pip", "install", "--disable-pip-version-check",
              "-r", "requirements.txt"], cwd=worktree, stdout=log, timeout=timeout_s)
        env.update({
            "PYTHON": str(venv_python),
            "SWITCHBOARD_CI_STRICT": "1",
            "SWITCHBOARD_CI_REQUIRE_NODE": "1",
        })
        _run(["scripts/switchboard_ci.sh"], cwd=worktree, env=env, stdout=log,
             timeout=timeout_s)


def run_gate_for_pr(pr: Dict[str, Any], *, repo: str, token: str, context: str,
                    work_root: Path, source_repo: Path, timeout_s: int,
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
    cache = _ensure_cache_repo(work_root, source_repo, repo)
    run_dir = _prepare_worktree(cache, work_root, pr, run_tag)
    try:
        run_switchboard_gate(run_dir, log_path, timeout_s=timeout_s)
        post_status(repo, sha, "success", context=context,
                    description="Switchboard VM gate passed", target_url=pr_url,
                    token=token)
        return {"pr": number, "sha": sha, "state": "success", "log": str(log_path)}
    except Exception as exc:
        post_status(repo, sha, "failure", context=context,
                    description=f"Switchboard VM gate failed: {exc}", target_url=pr_url,
                    token=token)
        return {"pr": number, "sha": sha, "state": "failure", "log": str(log_path),
                "error": str(exc)}
    finally:
        if not keep_worktree:
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
    parser.add_argument("--workdir", default=os.environ.get("SWITCHBOARD_CI_WORKDIR",
                                                            DEFAULT_WORKDIR))
    parser.add_argument("--source-repo", default=os.environ.get("SWITCHBOARD_CI_SOURCE_REPO",
                                                               str(Path.cwd())))
    parser.add_argument("--timeout-s", type=int,
                        default=int(os.environ.get("SWITCHBOARD_CI_TIMEOUT_SECONDS", "1800")))
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
    for pr in _select_prs(args, args.repo, token):
        if pr.get("draft") and os.environ.get("SWITCHBOARD_CI_SKIP_DRAFTS", "1") != "0":
            continue
        result = run_gate_for_pr(pr, repo=args.repo, token=token, context=args.context,
                                 work_root=root, source_repo=source_repo,
                                 timeout_s=args.timeout_s,
                                 keep_worktree=args.keep_worktree)
        print(json.dumps(result, sort_keys=True))
        results.append(result)
    failed = [r for r in results if r.get("state") != "success"]
    return 1 if args.fail_on_red and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
