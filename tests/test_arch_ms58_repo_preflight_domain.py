#!/usr/bin/env python3
"""ARCH-MS-58: repo_preflight lives under domain/provenance (no shell git)."""
from __future__ import annotations

import importlib
import os
import re
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms58-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

passed = failed = 0
SHELL_BEFORE = 2268


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    try:
        importlib.import_module("switchboard.domain.provenance.preflight")
        ok(True, "switchboard.domain.provenance.preflight imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"preflight import failed: {exc!r}")

    ok((ROOT / "src/switchboard/domain/provenance/preflight.py").is_file(),
       "preflight.py exists under domain/provenance")

    from switchboard.domain.provenance import preflight as preflight_mod  # noqa: E402
    import store  # noqa: E402

    ok(store.repo_preflight is preflight_mod.repo_preflight,
       "store facade delegates repo_preflight")
    ok(store._repo_git is preflight_mod._repo_git,
       "store facade delegates _repo_git")
    ok(store.repo_preflight.__module__
       == "switchboard.domain.provenance.preflight",
       "repo_preflight lives under domain.provenance.preflight")
    ok("side-effect-free" in (store.repo_preflight.__doc__ or ""),
       "docstring keeps side-effect-free report contract")

    shell_src = (ROOT / "src/switchboard/storage/repositories/shell.py").read_text()
    pre_src = (ROOT / "src/switchboard/domain/provenance/preflight.py").read_text()

    for name in (
        "_repo_preflight_finding",
        "_repo_git",
        "_repo_remote_slug",
        "_repo_parse_status",
        "_repo_git_dir",
        "_repo_merge_state",
        "_repo_list_candidate_files",
        "_repo_scan_conflict_markers",
        "_repo_worktree_collisions",
        "repo_preflight",
    ):
        ok(f"def {name}(" not in shell_src,
           f"shell residual no longer defines {name}")
        ok(f"def {name}(" in pre_src,
           f"domain preflight defines {name}")

    ok("subprocess.run" not in shell_src
       or not re.search(r'\["git"', shell_src),
       "shell has no subprocess git invocations")
    ok('["git", "-C", repo_path, *args]' in pre_src
       or '["git", "-C", repo_path' in pre_src,
       "git subprocess lives in domain preflight")
    ok("from switchboard.domain.provenance.preflight import" in shell_src,
       "shell re-exports domain preflight")

    shell_lines = shell_src.count("\n") + 1
    ok(shell_lines <= SHELL_BEFORE - 250,
       f"shell residual shrank meaningfully ({shell_lines} <= {SHELL_BEFORE - 250})")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
