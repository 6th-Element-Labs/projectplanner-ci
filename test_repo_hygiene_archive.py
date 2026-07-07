#!/usr/bin/env python3
"""HARDEN-31 live repo hygiene archive tests."""
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts" / "archive_live_repo_debris.py"
spec = importlib.util.spec_from_file_location("archive_live_repo_debris", SCRIPT)
archive_live_repo_debris = importlib.util.module_from_spec(spec)
sys.modules["archive_live_repo_debris"] = archive_live_repo_debris
spec.loader.exec_module(archive_live_repo_debris)

_TMP = tempfile.mkdtemp(prefix="repo-hygiene-archive-")
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def run(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def write(path, text="x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


try:
    ignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    ok(".switchboard/" in ignore_text and "*.bak" in ignore_text and "**/._*" in ignore_text and
       "/scripts/seed_helm_layers_northstar.py" in ignore_text,
       ".gitignore documents known local operational debris")

    repo = Path(_TMP) / "repo"
    archive_root = Path(_TMP) / "archive"
    repo.mkdir()
    run(repo, "init")
    run(repo, "config", "user.email", "switchboard@example.test")
    run(repo, "config", "user.name", "Switchboard Test")
    write(repo / "tracked.txt", "tracked\n")
    run(repo, "add", "tracked.txt")
    run(repo, "commit", "-m", "base")

    write(repo / ".env.bak-precutover-20260707T054852Z", "backup\n")
    write(repo / "static" / "app.js.bak.pre-md", "backup\n")
    write(repo / ".switchboard" / "runner" / "run_123" / "session.json", "{}\n")
    write(repo / "deploy" / "._Caddyfile", "appledouble\n")
    write(repo / "app.js", "accidental root bundle\n")
    write(repo / "scripts" / "seed_helm_layers_northstar.py", "one-off live seed\n")
    write(repo / "notes.txt", "unknown\n")
    write(repo / ".env", "secret\n")

    dry = archive_live_repo_debris.archive_debris(str(repo), str(archive_root), apply=False)
    ok(dry["matched_count"] == 6 and dry["moved_count"] == 0,
       "dry-run identifies only known-safe debris")
    ok("notes.txt" in dry["skipped"] and ".env" in dry["skipped"],
       "dry-run leaves unknown files and .env visible")

    applied = archive_live_repo_debris.archive_debris(str(repo), str(archive_root), apply=True)
    ok(applied["moved_count"] == 6,
       "apply moves known-safe debris")
    ok(not (repo / ".env.bak-precutover-20260707T054852Z").exists() and
       not (repo / ".switchboard" / "runner" / "run_123" / "session.json").exists(),
       "safe files are removed from the repo checkout")
    ok((repo / "notes.txt").exists() and (repo / ".env").exists(),
       "unknown files and .env remain in place")
    archive_dir = Path(applied["archive_dir"])
    ok((archive_dir / ".env.bak-precutover-20260707T054852Z").exists() and
       (archive_dir / ".switchboard" / "runner" / "run_123" / "session.json").exists(),
       "archive preserves original relative paths")
    remaining = archive_live_repo_debris.git_untracked(repo)
    ok(remaining == [".env", "notes.txt"],
       "only intentionally skipped files remain visible to git")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
