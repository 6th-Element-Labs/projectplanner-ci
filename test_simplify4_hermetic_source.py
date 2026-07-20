#!/usr/bin/env python3
"""SIMPLIFY-4 / SIMPLIFY-13 regression proof for Agent Host Work Session sources."""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from adapters import agent_host_enrollment as enrollment
from adapters import switchboard_core as core


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


root = Path(tempfile.mkdtemp(prefix="simplify4-")).resolve()
try:
    origin = root / "origin.git"
    operator = root / "operator"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "clone", str(origin), str(operator)], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    git(operator, "config", "user.email", "test@example.test")
    git(operator, "config", "user.name", "Switchboard Test")
    (operator / "tracked.txt").write_text("one\n", encoding="utf-8")
    git(operator, "add", "tracked.txt")
    git(operator, "commit", "-m", "initial")
    git(operator, "branch", "-M", "master")
    git(operator, "push", "-u", "origin", "master")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/master")

    state_root = root / "host-state"
    mirror = enrollment._provision_host_source_mirror(operator, state_root)
    (operator / "operator-only.tmp").write_text("dirty\n", encoding="utf-8")
    assert git(mirror, "status", "--porcelain") == ""
    assert mirror != operator and mirror.parent == state_root / "source"

    (mirror / "named-offender.tmp").write_text("dirty\n", encoding="utf-8")
    try:
        core.create_external_work_session(
            "switchboard", "SIMPLIFY-4", "codex/SIMPLIFY-4", "codex", str(mirror))
    except RuntimeError as exc:
        assert "named-offender.tmp" in str(exc)
    else:
        raise AssertionError("dirty mirror was accepted")
    (mirror / "named-offender.tmp").unlink()

    # Advance origin, then suppress the provisioner's fetch to model a stale mirror.
    (operator / "tracked.txt").write_text("two\n", encoding="utf-8")
    git(operator, "add", "tracked.txt")
    git(operator, "commit", "-m", "advance")
    git(operator, "push", "origin", "master")
    original_run = core.subprocess.run

    def stale_fetch(command, **kwargs):
        if command[:4] == ["git", "-C", str(mirror), "fetch"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(command, **kwargs)

    core.subprocess.run = stale_fetch
    try:
        try:
            core.create_external_work_session(
                "switchboard", "SIMPLIFY-4", "codex/SIMPLIFY-4", "codex", str(mirror))
        except RuntimeError as exc:
            assert "external source mirror is stale" in str(exc)
            assert "origin/master=" in str(exc)
        else:
            raise AssertionError("stale mirror was accepted")
    finally:
        core.subprocess.run = original_run

    # SIMPLIFY-13: install/update fetch is best-effort — a network blip must not
    # refuse enrollment. Work Session provision remains the hard freshness gate.
    original_enrollment_run = enrollment.subprocess.run

    def fetch_blip(command, **kwargs):
        if (len(command) >= 4 and command[0] == "git"
                and command[1] == "-C" and command[3] == "fetch"):
            return subprocess.CompletedProcess(
                command, 1, "", "fatal: unable to access origin (network blip)")
        return original_enrollment_run(command, **kwargs)

    enrollment.subprocess.run = fetch_blip
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            again = enrollment._provision_host_source_mirror(operator, state_root)
        assert again == mirror, "install-time fetch failure refused the host"
        warned = err.getvalue()
        assert "WARNING" in warned and "fetch" in warned.lower(), warned
    finally:
        enrollment.subprocess.run = original_enrollment_run

    def suppress_install_fetch(command, **kwargs):
        if (len(command) >= 4 and command[0] == "git"
                and command[1] == "-C" and command[3] == "fetch"):
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_enrollment_run(command, **kwargs)

    enrollment.subprocess.run = suppress_install_fetch
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            still = enrollment._provision_host_source_mirror(operator, state_root)
        assert still == mirror, "install-time stale mirror refused the host"
        warned = err.getvalue()
        assert "WARNING" in warned and "stale" in warned.lower(), warned
    finally:
        enrollment.subprocess.run = original_enrollment_run
finally:
    shutil.rmtree(root, ignore_errors=True)

print("SIMPLIFY-4/13 hermetic source proof passed")
