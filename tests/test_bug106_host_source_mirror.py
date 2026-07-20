#!/usr/bin/env python3
"""BUG-106: host source mirror must accept macOS tempfile state roots.

Root cause: ``_validated_source_repo_root`` compared ``Path.absolute()`` to
``Path.resolve()``. On macOS, tempfile paths live under ``/var/folders`` which
firmlinks to ``/private/var/folders``, so the pre-#673 provisioner built a
mirror under the unresolved state root and then rejected its own output with
``source_repo_root must resolve directly to a directory``.

This suite pins both layers:
1. the provisioner must accept an *unresolved* tempfile state_root
2. the validator must canonicalize firmlink ancestors but still refuse a leaf symlink
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

from adapters.agent_host_enrollment import (
    EnrollmentError,
    _provision_host_source_mirror,
    _validated_source_repo_root,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def _local_operator_repo(root: Path) -> Path:
    origin = root / "origin.git"
    operator = root / "operator"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(
        ["git", "clone", str(origin), str(operator)],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _git(operator, "config", "user.email", "bug106@example.test")
    _git(operator, "config", "user.name", "BUG-106")
    (operator / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(operator, "add", "tracked.txt")
    _git(operator, "commit", "-m", "initial")
    _git(operator, "branch", "-M", "master")
    _git(operator, "push", "-u", "origin", "master")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/master")
    return operator.resolve()


def test_provision_accepts_unresolved_tempfile_state_root():
    """Enrollment-style TMP paths must not trip firmlink absolute!=resolve."""
    # Leave unresolved — same shape as test_agent_host_enrollment.TMP.
    state_root = Path(tempfile.mkdtemp(prefix="bug106-state-"))
    scratch = Path(tempfile.mkdtemp(prefix="bug106-src-"))
    try:
        # On macOS, /var and /tmp firmlink through /private/*; that mismatch is
        # the BUG-106 failure mode. On Linux the paths are already canonical,
        # so still exercise the provisioner — just without the firmlink assert.
        has_firmlink_ancestors = state_root.absolute() != state_root.resolve()
        operator = _local_operator_repo(scratch)
        mirror = _provision_host_source_mirror(operator, state_root)
        assert mirror.is_dir()
        assert mirror == mirror.resolve()
        assert mirror.parent == state_root.resolve() / "source"
        assert _git(mirror, "rev-parse", "--is-inside-work-tree") == "true"
        if has_firmlink_ancestors:
            # Explicit proof the validator accepted a path that absolute()!=resolve.
            assert _validated_source_repo_root(
                state_root.absolute() / "source" / mirror.name) == mirror
    finally:
        shutil.rmtree(state_root, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)


def test_validator_canonicalizes_firmlink_ancestors():
    scratch = Path(tempfile.mkdtemp(prefix="bug106-canon-"))
    try:
        operator = _local_operator_repo(scratch)
        # Re-express the checkout through the unresolved tempfile prefix when
        # the platform has firmlinks; otherwise the absolute path is already
        # canonical and this still validates.
        unresolved = Path(str(operator).replace("/private/var/", "/var/", 1))
        if unresolved.exists():
            validated = _validated_source_repo_root(unresolved)
            assert validated == operator.resolve()
        else:
            validated = _validated_source_repo_root(operator)
            assert validated == operator.resolve()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_validator_refuses_leaf_symlink_with_named_target():
    scratch = Path(tempfile.mkdtemp(prefix="bug106-symlink-")).resolve()
    try:
        operator = _local_operator_repo(scratch)
        link = scratch / "symlink-checkout"
        link.symlink_to(operator)
        try:
            _validated_source_repo_root(link)
        except EnrollmentError as exc:
            message = str(exc)
            assert "must not be a symlink" in message
            assert str(link) in message
            assert os.readlink(link) in message or str(operator) in message
        else:
            raise AssertionError("leaf symlink was accepted")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_validator_names_missing_path():
    missing = Path(tempfile.gettempdir()).resolve() / "bug106-missing-source"
    try:
        _validated_source_repo_root(missing)
    except EnrollmentError as exc:
        assert "does not exist" in str(exc)
        assert str(missing) in str(exc)
    else:
        raise AssertionError("missing path was accepted")


if __name__ == "__main__":
    test_provision_accepts_unresolved_tempfile_state_root()
    test_validator_canonicalizes_firmlink_ancestors()
    test_validator_refuses_leaf_symlink_with_named_target()
    test_validator_names_missing_path()
    print("BUG-106 host source mirror proof passed")
