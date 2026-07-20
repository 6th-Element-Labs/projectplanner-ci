#!/usr/bin/env python3
"""BUG-109: host release rollout without collisions or bootstrap races.

Live incident 2026-07-21: two builders independently chose "0.2.26" from
different commits; the older-content bundle installed first and the monotonic
check then blocked the genuinely newer build. Separately, the 0.2.27 update's
launchd bootstrap raced the asynchronous teardown of the job its own bootout
had removed (EIO 5) and rolled back until a manual bootout-first retry.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401
from adapters import agent_host_enrollment as enrollment

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


with tempfile.TemporaryDirectory(prefix="bug109-") as raw:
    repo = Path(raw) / "src"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
    git(repo, "config", "user.email", "t@example.test")
    git(repo, "config", "user.name", "BUG-109")
    for n in range(3):
        (repo / f"f{n}.txt").write_text(str(n), encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"c{n}")

    # ---- commit-derived versions: deterministic, collision-proof ----------
    v3 = enrollment._auto_bundle_version(repo)
    ok(v3 == "0.3.3", f"version derives from commit count ({v3})")
    ok(enrollment._auto_bundle_version(repo) == v3,
       "two builders at the same commit produce the IDENTICAL version — the "
       "parallel-0.2.26 collision is unrepresentable")
    (repo / "f3.txt").write_text("3", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c3")
    ok(enrollment._auto_bundle_version(repo) == "0.3.4",
       "a later commit always outranks an earlier one")
    ok(enrollment._parse_version("0.3.4") > enrollment._parse_version("0.3.3"),
       "the derived versions honour SemVer precedence for the update check")
    try:
        enrollment._auto_bundle_version(Path(raw))
        ok(False, "non-checkout source is refused")
    except enrollment.EnrollmentError:
        ok(True, "non-checkout source is refused")

    # ---- manifest carries source provenance -------------------------------
    ok(enrollment._source_git_head(repo) == git(repo, "rev-parse", "HEAD"),
       "bundle provenance helper reports the exact source HEAD sha")
    src = Path("adapters/agent_host_enrollment.py").read_text() \
        if Path("adapters/agent_host_enrollment.py").exists() else \
        (Path(ROOT) / "adapters/agent_host_enrollment.py").read_text()
    ok('"source_sha": source_sha' in src and '"auto"' in src,
       "create_signed_bundle embeds source_sha and the CLI accepts --version auto")

# ---- bootstrap retry: tolerate launchd's asynchronous teardown ------------
calls = []


def racing_runner(command, **kwargs):
    calls.append(list(command))
    if command[1] == "bootout":
        return subprocess.CompletedProcess(command, 0, "", "")
    if command[1] == "print":
        return subprocess.CompletedProcess(command, 113, "", "not found")
    if command[1] == "bootstrap":
        first = sum(1 for c in calls if c[1] == "bootstrap") == 1
        return subprocess.CompletedProcess(
            command, 5 if first else 0, "", "Bootstrap failed: 5" if first else "")
    return subprocess.CompletedProcess(command, 0, "", "")


enrollment.control_service("darwin", "restart", Path("/tmp/x.plist"),
                           runner=racing_runner)
seq = [c[1] for c in calls]
ok(seq == ["bootout", "bootstrap", "print", "bootstrap"],
   f"a racing bootstrap waits for the label to clear and retries once ({seq})")

calls.clear()


def hard_failure(command, **kwargs):
    calls.append(list(command))
    if command[1] == "print":
        return subprocess.CompletedProcess(command, 113, "", "")
    if command[1] == "bootstrap":
        return subprocess.CompletedProcess(command, 5, "", "Bootstrap failed: 5")
    return subprocess.CompletedProcess(command, 0, "", "")


try:
    enrollment.control_service("darwin", "restart", Path("/tmp/x.plist"),
                               runner=hard_failure)
    ok(False, "a persistently failing bootstrap still raises after one retry")
except enrollment.EnrollmentError:
    ok(sum(1 for c in calls if c[1] == "bootstrap") == 2,
       "a persistently failing bootstrap still raises after one retry")

calls.clear()


def clean_runner(command, **kwargs):
    calls.append(list(command))
    return subprocess.CompletedProcess(command, 0, "", "")


enrollment.control_service("darwin", "restart", Path("/tmp/x.plist"),
                           runner=clean_runner)
ok([c[1] for c in calls] == ["bootout", "bootstrap"],
   "a clean restart performs no extra polling or retries")

# A non-EIO bootstrap failure (e.g. a fixture's injected rc 1) must fail
# immediately with NO poll and NO retry — rollback flows depend on the exact
# call sequence.
calls.clear()


def injected_failure(command, **kwargs):
    calls.append(list(command))
    if command[1] == "bootstrap":
        return subprocess.CompletedProcess(command, 1, "", "injected failure")
    return subprocess.CompletedProcess(command, 0, "", "")


try:
    enrollment.control_service("darwin", "restart", Path("/tmp/x.plist"),
                               runner=injected_failure)
    ok(False, "non-EIO bootstrap failures surface immediately, no retry")
except enrollment.EnrollmentError:
    ok([c[1] for c in calls] == ["bootout", "bootstrap"],
       "non-EIO bootstrap failures surface immediately with no poll or retry")

print(f"\nBUG-109 release rollout: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
