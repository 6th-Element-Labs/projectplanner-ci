import subprocess
import tempfile
from pathlib import Path

import review_preflight


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def run(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def commit(repo, name, text):
    path = Path(repo) / name
    path.write_text(text, encoding="utf-8")
    run(repo, "add", name)
    run(repo, "commit", "-m", f"commit {name}")


with tempfile.TemporaryDirectory(prefix="switchboard-preflight-") as tmp:
    repo = Path(tmp)
    run(repo, "init")
    run(repo, "config", "user.email", "switchboard@example.test")
    run(repo, "config", "user.name", "Switchboard Test")
    commit(repo, "base.txt", "base\n")
    run(repo, "branch", "-M", "main")

    clean = review_preflight.run_git_review_preflight(
        repo, target_ref="HEAD", upstream_ref="main",
        intended_project="switchboard", intended_branch="main")
    ok(clean["status"] == "pass" and clean["ok"], "current clean target passes")
    ok(clean["branch_distance"]["behind"] == 0 and clean["dirty"] is False,
       "pass report records clean branch distance")

    run(repo, "checkout", "-b", "feature")
    commit(repo, "feature.txt", "feature\n")
    run(repo, "checkout", "main")
    commit(repo, "main.txt", "main\n")
    run(repo, "checkout", "feature")

    stale = review_preflight.run_git_review_preflight(
        repo, target_ref="HEAD", upstream_ref="main",
        intended_project="switchboard", intended_branch="main")
    ok(stale["status"] == "red" and not stale["ok"], "behind target fails red")
    ok(stale["branch_distance"]["behind"] == 1 and stale["branch_distance"]["ahead"] == 1,
       "stale report records ahead/behind counts")
    ok(any(f["code"] == "target_branch_behind_upstream" for f in stale["findings"]),
       "stale report names target_branch_behind_upstream")
    header = review_preflight.format_preflight_header(stale)
    ok("branch_distance" in header and "target_branch_behind_upstream" in header,
       "human header includes branch distance and stale finding")

    allowed = review_preflight.run_git_review_preflight(
        repo, target_ref="HEAD", upstream_ref="main", allow_behind=True)
    ok(allowed["status"] == "yellow" and allowed["findings"][0]["blocking"] is False,
       "explicit stale override downgrades red to yellow")

    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    dirty = review_preflight.run_git_review_preflight(
        repo, target_ref="HEAD", upstream_ref="main")
    ok(dirty["status"] == "red" and dirty["dirty"] is True,
       "dirty worktree fails red")
    ok(any(f["code"] == "dirty_worktree" for f in dirty["findings"]),
       "dirty report names dirty_worktree")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
