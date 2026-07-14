#!/usr/bin/env python3
"""Executable regression tests for portable CO exact-source cache archives."""
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts" / "build_co_repo_cache.sh"
TMP = Path(tempfile.mkdtemp(prefix="co-repo-cache-test-"))
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def run(*args, cwd=None, check=True):
    return subprocess.run(
        [str(arg) for arg in args], cwd=cwd, check=check,
        text=True, capture_output=True)


try:
    source = TMP / "source"
    archive = TMP / "projectplanner.mirror.tar.gz"
    source.mkdir()
    run("git", "init", "-q", source)
    run("git", "config", "user.email", "cache-test@example.test", cwd=source)
    run("git", "config", "user.name", "Cache Test", cwd=source)
    (source / "proof.txt").write_text("portable cache\n", encoding="utf-8")
    run("git", "add", "proof.txt", cwd=source)
    run("git", "commit", "-q", "-m", "cache fixture", cwd=source)
    commit = run("git", "rev-parse", "HEAD", cwd=source).stdout.strip()

    built = run(SCRIPT, source, commit, archive)
    ok(archive.is_file() and "built archive=" in built.stdout,
       "builder creates a checksummed exact-source mirror archive")
    verified = run(SCRIPT, "--verify", archive, commit)
    ok("verified archive=" in verified.stdout,
       "published archive passes extraction, full git fsck, and pinned-commit verification")
    entries = run("tar", "-tzf", archive).stdout.splitlines()
    ok(any(entry.startswith("projectplanner.git/") for entry in entries)
       and not any("/._" in entry or entry.startswith("._") for entry in entries),
       "portable archive contains the expected mirror root and no AppleDouble entries")

    unpacked = TMP / "tampered"
    unpacked.mkdir()
    run("tar", "-xzf", archive, "-C", unpacked)
    apple_double = unpacked / "projectplanner.git" / "objects" / "pack" / "._pack-test"
    apple_double.parent.mkdir(parents=True, exist_ok=True)
    apple_double.write_text("forbidden metadata", encoding="utf-8")
    tampered = TMP / "tampered.tar.gz"
    with tarfile.open(tampered, "w:gz") as archive_file:
        archive_file.add(unpacked / "projectplanner.git", arcname="projectplanner.git")
    rejected = run(SCRIPT, "--verify", tampered, commit, check=False)
    ok(rejected.returncode != 0 and "AppleDouble" in rejected.stderr,
       "verifier rejects AppleDouble metadata before a cache can be published")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nCO repo cache: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
