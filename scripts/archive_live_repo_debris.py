#!/usr/bin/env python3
"""Archive known-safe untracked deploy debris out of a live git checkout.

This is intentionally conservative: only untracked files matching explicit operational patterns
are moved. Unknown untracked files stay in place so repo_preflight can keep failing loudly.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List


SAFE_DEBRIS_PATTERNS = (
    ".switchboard/*",
    ".switchboard/**",
    ".env.bak",
    ".env.bak*",
    "._*",
    "**/._*",
    "*.bak",
    "*.bak.*",
    "*.new",
    "app.js",
    "scripts/seed_helm_layers_northstar.py",
)


def _run_git(repo: Path, args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def git_untracked(repo: Path) -> List[str]:
    result = _run_git(repo, ["status", "--porcelain=v1", "-uall"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git status failed")
    out: List[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("?? "):
            out.append(line[3:])
    return sorted(out)


def is_safe_debris(rel_path: str) -> bool:
    rel = rel_path.strip()
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel or rel == ".env":
        return False
    return any(fnmatch.fnmatch(rel, pattern) for pattern in SAFE_DEBRIS_PATTERNS)


def _unique_archive_dir(root: Path, label: str = "") -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    suffix = f"-{label}" if label else ""
    candidate = root / f"{stamp}{suffix}"
    if not candidate.exists():
        return candidate
    for idx in range(2, 100):
        alt = root / f"{stamp}{suffix}-{idx}"
        if not alt.exists():
            return alt
    raise RuntimeError("could not allocate archive directory")


def _prune_empty_parents(path: Path, stop: Path) -> None:
    cur = path.parent
    stop = stop.resolve()
    while cur.resolve() != stop and str(cur.resolve()).startswith(str(stop)):
        try:
            cur.rmdir()
        except OSError:
            break
        cur = cur.parent


def archive_debris(repo: str, archive_root: str, *, apply: bool = False,
                   label: str = "repo-hygiene") -> Dict[str, object]:
    repo_path = Path(repo).resolve()
    archive_base = Path(archive_root).resolve()
    untracked = git_untracked(repo_path)
    matched = [p for p in untracked if is_safe_debris(p)]
    skipped = [p for p in untracked if not is_safe_debris(p)]
    archive_dir = _unique_archive_dir(archive_base, label) if matched else None
    moved: List[Dict[str, str]] = []
    if apply and archive_dir:
        archive_dir.mkdir(parents=True, exist_ok=False)
        for rel in matched:
            src = (repo_path / rel).resolve()
            if not str(src).startswith(str(repo_path) + os.sep) or not src.exists():
                skipped.append(rel)
                continue
            dest = archive_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            moved.append({"from": str(src), "to": str(dest)})
            _prune_empty_parents(src, repo_path)
    return {
        "schema": "switchboard.repo_hygiene_archive.v1",
        "repo": str(repo_path),
        "archive_root": str(archive_base),
        "archive_dir": str(archive_dir) if archive_dir else None,
        "apply": bool(apply),
        "matched_count": len(matched),
        "skipped_count": len(skipped),
        "moved_count": len(moved),
        "matched": matched,
        "skipped": skipped,
        "moved": moved,
        "safe_patterns": list(SAFE_DEBRIS_PATTERNS),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--archive-root", default="/var/lib/projectplanner/repo-hygiene-archive")
    ap.add_argument("--label", default="repo-hygiene")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    print(json.dumps(
        archive_debris(args.repo, args.archive_root, apply=args.apply, label=args.label),
        indent=2,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
