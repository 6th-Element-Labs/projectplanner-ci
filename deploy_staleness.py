#!/usr/bin/env python3
"""BUG-114: make production deploy lag a visible, auditable signal.

A merge to canonical master used to reach the running system only when a human
remembered to SSH in and run ``deploy/redeploy.sh`` — so the board could stamp
work Done, CI could be green, and the product would still behave as if the
change never shipped (the "overstated Done" failure class, one deployment layer
up). This module computes the gap between the running SHA and canonical master
and persists it to a small state file, so:

  * ``deploy/auto_deploy.sh`` can decide whether a redeploy is even needed, and
  * the ``/health/version`` endpoint can surface "prod is N commits behind
    master" without ever shelling out to git on the request path.

The functions are deliberately pure/​injectable so the decision logic is tested
without a live checkout; ``main()`` wires them to a real repo for the timer.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from typing import Callable, Optional

SCHEMA = "switchboard.deploy_staleness.v1"
DEFAULT_CANONICAL_REF = "origin/master"

# A git runner takes an argv list and returns (returncode, stdout). Injectable so
# tests never touch a network or a real remote.
GitRunner = Callable[[list], "tuple[int, str]"]


def _default_git_runner(root: str) -> GitRunner:
    def run(args: list) -> "tuple[int, str]":
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=120, check=False,
        )
        return proc.returncode, (proc.stdout or "").strip()
    return run


def staleness_payload(running_sha: str, canonical_sha: str, commits_behind: int,
                      *, canonical_ref: str = DEFAULT_CANONICAL_REF,
                      checked_at: Optional[float] = None,
                      last_deploy_at: Optional[float] = None,
                      last_deploy_sha: Optional[str] = None,
                      last_deploy_ok: Optional[bool] = None,
                      last_deploy_error: Optional[str] = None) -> dict:
    """Build the canonical deploy-staleness payload.

    ``deploy_stale`` is derived, never stored independently, so the boolean can
    never disagree with the count it summarizes. A negative or unknown
    ``commits_behind`` (e.g. git failed) is coerced to a non-negative int and
    still reported, because a silent 0 would read as "up to date".
    """
    behind = int(commits_behind) if commits_behind is not None else 0
    if behind < 0:
        behind = 0
    payload = {
        "schema": SCHEMA,
        "running_sha": running_sha or "",
        "canonical_sha": canonical_sha or "",
        "canonical_ref": canonical_ref,
        "commits_behind": behind,
        "deploy_stale": behind > 0,
        "checked_at": checked_at,
        "last_deploy_at": last_deploy_at,
        "last_deploy_sha": last_deploy_sha,
        "last_deploy_ok": last_deploy_ok,
        "last_deploy_error": last_deploy_error,
    }
    return payload


def write_state(path: str, payload: dict) -> None:
    """Atomically write the state file, world-readable.

    The timer usually runs as root while the web app reads as the service user,
    so the file must be readable by both. The temp-file+rename keeps a concurrent
    reader from ever seeing a half-written document.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".deploy-state-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_state(path: str) -> Optional[dict]:
    """Return the persisted payload, or None when absent/unreadable/malformed.

    Reads never raise: a missing or corrupt signal must degrade to "unknown", not
    take down the health endpoint that reports it.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def compute_staleness(git: GitRunner, *, canonical_ref: str = DEFAULT_CANONICAL_REF,
                      fetch: bool = False) -> "tuple[str, str, int, Optional[str]]":
    """Return (running_sha, canonical_sha, commits_behind, error).

    ``error`` is a short human string when git could not answer (network blip on
    fetch, detached remote, etc.); callers still get whatever SHAs resolved so a
    transient fetch failure degrades gracefully instead of erasing the signal.
    """
    error: Optional[str] = None
    if fetch:
        rc, out = git(["fetch", "--quiet", "origin",
                       canonical_ref.split("/", 1)[-1]])
        if rc != 0:
            error = "fetch_failed"
    rc_head, running = git(["rev-parse", "HEAD"])
    if rc_head != 0:
        running = ""
    rc_canon, canonical = git(["rev-parse", canonical_ref])
    if rc_canon != 0:
        canonical = ""
        error = error or "canonical_unresolved"
    behind = 0
    if running and canonical:
        rc_behind, count = git(["rev-list", "--count", f"HEAD..{canonical_ref}"])
        if rc_behind == 0 and count.isdigit():
            behind = int(count)
        else:
            error = error or "count_failed"
    return running, canonical, behind, error


def refresh(root: str, state_path: str, *, git: Optional[GitRunner] = None,
            canonical_ref: str = DEFAULT_CANONICAL_REF, fetch: bool = False,
            now: Optional[float] = None, carry_deploy: bool = True) -> dict:
    """Recompute staleness for ``root`` and persist it to ``state_path``.

    ``carry_deploy`` preserves the previous file's ``last_deploy_*`` fields so a
    plain staleness refresh (the timer's common case) never erases the record of
    the last actual deploy.
    """
    runner = git or _default_git_runner(root)
    running, canonical, behind, _error = compute_staleness(
        runner, canonical_ref=canonical_ref, fetch=fetch)
    prior = read_state(state_path) or {} if carry_deploy else {}
    payload = staleness_payload(
        running, canonical, behind, canonical_ref=canonical_ref, checked_at=now,
        last_deploy_at=prior.get("last_deploy_at"),
        last_deploy_sha=prior.get("last_deploy_sha"),
        last_deploy_ok=prior.get("last_deploy_ok"),
        last_deploy_error=prior.get("last_deploy_error"))
    write_state(state_path, payload)
    return payload


def record_deploy(state_path: str, *, deployed_sha: str, ok: bool,
                  error: Optional[str] = None, now: Optional[float] = None,
                  git: Optional[GitRunner] = None, root: Optional[str] = None,
                  canonical_ref: str = DEFAULT_CANONICAL_REF) -> dict:
    """Persist the outcome of a deploy attempt, recomputing current staleness.

    After a successful redeploy the running SHA has advanced, so we recompute
    (no fetch — the caller just fetched) rather than assume behind==0.
    """
    runner = git or (_default_git_runner(root) if root else None)
    if runner is not None:
        running, canonical, behind, _err = compute_staleness(
            runner, canonical_ref=canonical_ref, fetch=False)
    else:
        prior = read_state(state_path) or {}
        running = deployed_sha if ok else prior.get("running_sha", "")
        canonical = prior.get("canonical_sha", "")
        behind = 0 if ok else int(prior.get("commits_behind", 0) or 0)
    payload = staleness_payload(
        running, canonical, behind, canonical_ref=canonical_ref, checked_at=now,
        last_deploy_at=now, last_deploy_sha=deployed_sha, last_deploy_ok=ok,
        last_deploy_error=None if ok else (error or "deploy_failed"))
    write_state(state_path, payload)
    return payload


def default_state_path() -> str:
    """Resolve the state-file path the health endpoint and timer share.

    Priority: explicit ``PM_DEPLOY_STATE_FILE``; else next to the board db
    (``PM_DB_PATH``'s directory) so it lands in the service-owned data dir on
    prod; else the repo root as a last resort for a bare checkout.
    """
    explicit = (os.environ.get("PM_DEPLOY_STATE_FILE") or "").strip()
    if explicit:
        return explicit
    db_path = (os.environ.get("PM_DB_PATH") or "").strip()
    if db_path:
        return os.path.join(os.path.dirname(os.path.abspath(db_path)),
                            "deploy-state.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "deploy-state.json")


def health_view(state_path: Optional[str] = None) -> dict:
    """Compact, auth-safe payload for ``/health/version``.

    Never raises and never shells git on the request path; a missing signal is
    reported as ``deploy_signal: "unknown"`` rather than an error.
    """
    path = state_path or default_state_path()
    state = read_state(path)
    if not state:
        return {"schema": SCHEMA, "deploy_signal": "unknown"}
    return {
        "schema": SCHEMA,
        "deploy_signal": "stale" if state.get("deploy_stale") else "current",
        "running_sha": state.get("running_sha") or "",
        "canonical_sha": state.get("canonical_sha") or "",
        "canonical_ref": state.get("canonical_ref") or DEFAULT_CANONICAL_REF,
        "commits_behind": int(state.get("commits_behind") or 0),
        "checked_at": state.get("checked_at"),
        "last_deploy_at": state.get("last_deploy_at"),
        "last_deploy_ok": state.get("last_deploy_ok"),
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy staleness signal.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_refresh = sub.add_parser("refresh", help="recompute and write the signal")
    p_refresh.add_argument("--root", required=True)
    p_refresh.add_argument("--state", required=True)
    p_refresh.add_argument("--canonical-ref", default=DEFAULT_CANONICAL_REF)
    p_refresh.add_argument("--fetch", action="store_true")

    p_deploy = sub.add_parser("record-deploy", help="record a deploy outcome")
    p_deploy.add_argument("--root", required=True)
    p_deploy.add_argument("--state", required=True)
    p_deploy.add_argument("--deployed-sha", required=True)
    p_deploy.add_argument("--canonical-ref", default=DEFAULT_CANONICAL_REF)
    p_deploy.add_argument("--ok", dest="ok", action="store_true")
    p_deploy.add_argument("--failed", dest="ok", action="store_false")
    p_deploy.add_argument("--error", default=None)
    p_deploy.set_defaults(ok=True)

    p_read = sub.add_parser("read", help="print the current health view")
    p_read.add_argument("--state", required=True)

    args = parser.parse_args(argv)
    import time
    now = time.time()
    if args.command == "refresh":
        payload = refresh(args.root, args.state, canonical_ref=args.canonical_ref,
                          fetch=args.fetch, now=now)
        print(json.dumps(payload))
        return 0
    if args.command == "record-deploy":
        payload = record_deploy(args.state, deployed_sha=args.deployed_sha,
                                ok=args.ok, error=args.error, now=now,
                                root=args.root, canonical_ref=args.canonical_ref)
        print(json.dumps(payload))
        return 0
    if args.command == "read":
        print(json.dumps(health_view(args.state)))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
