#!/usr/bin/env python3
"""SESSION-3 repo/worktree preflight contract tests."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="repo-preflight-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

P = "switchboard"
AGENT = "codex/PREFLIGHT-1-clean"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def run(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=str(repo), text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def commit(repo, name, text, message=None):
    path = Path(repo) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    run(repo, "add", name)
    run(repo, "commit", "-m", message or f"commit {name}")


def make_repo(name="repo"):
    root = Path(_TMP) / name
    remote = Path(_TMP) / f"{name}.git"
    run(_TMP, "init", "--bare", str(remote))
    run(_TMP, "init", str(root))
    run(root, "config", "user.email", "switchboard@example.test")
    run(root, "config", "user.name", "Switchboard Test")
    commit(root, "base.txt", "base\n", "base")
    run(root, "branch", "-M", "master")
    run(root, "remote", "add", "origin", str(remote))
    run(root, "push", "-u", "origin", "master")
    run(root, "checkout", "-b", "codex/PREFLIGHT-1-clean")
    run(root, "push", "-u", "origin", "codex/PREFLIGHT-1-clean")
    return root, remote


try:
    store.init_db(P)

    repo, _ = make_repo("clean")
    clean = store.repo_preflight(
        str(repo), project=P, task_id="PREFLIGHT-1", agent_id=AGENT,
        expected_branch="codex/PREFLIGHT-1-clean")
    ok(clean["verdict"] == "pass" and clean["ok"] is True,
       "clean task-scoped branch passes")
    ok(clean["branch"] == "codex/PREFLIGHT-1-clean" and
       clean["upstream"] == "origin/codex/PREFLIGHT-1-clean" and
       clean["base_distance"]["behind"] == 0,
       "clean report records branch, upstream, and base distance")

    commit(repo, "static/separators.css", "/* ======= */\nhr { border: 0; }\n",
           "separator lines are not conflicts")
    separators = store.repo_preflight(
        str(repo), project=P, task_id="PREFLIGHT-1", agent_id=AGENT,
        expected_branch="codex/PREFLIGHT-1-clean")
    ok(separators["verdict"] == "pass" and separators["conflict_marker_count"] == 0,
       "standalone separator lines are not conflict markers")

    (repo / "scratch.txt").write_text("dirty\n", encoding="utf-8")
    dirty = store.repo_preflight(
        str(repo), project=P, task_id="PREFLIGHT-1", agent_id=AGENT,
        expected_branch="codex/PREFLIGHT-1-clean")
    ok(dirty["verdict"] == "deny" and dirty["dirty"] is True,
       "dirty or untracked worktree denies")
    ok(any(f["failure_class"] == "dirty_worktree" for f in dirty["findings"]),
       "dirty report uses dirty_worktree failure class")
    (repo / "scratch.txt").unlink()

    (repo / "web" / "sat1.ts").parent.mkdir(exist_ok=True)
    (repo / "web" / "sat1.ts").write_text(
        "<<<<<<< ours\nleft\n=======\nright\n>>>>>>> theirs\n",
        encoding="utf-8",
    )
    conflict = store.repo_preflight(
        str(repo), project=P, task_id="PREFLIGHT-1", agent_id=AGENT,
        expected_branch="codex/PREFLIGHT-1-clean")
    ok(conflict["verdict"] == "deny" and conflict["conflict_marker_count"] == 1,
       "Helm SAT-style conflict markers deny")
    ok(any(f["failure_class"] == "conflict_markers" for f in conflict["findings"]),
       "conflict report uses conflict_markers failure class")
    shutil.rmtree(repo / "web")

    wrong_branch = store.repo_preflight(
        str(repo), project=P, task_id="PREFLIGHT-2", agent_id="codex/PREFLIGHT-2-other",
        expected_branch="codex/PREFLIGHT-2-other")
    ok(wrong_branch["verdict"] == "deny" and
       any(f["failure_class"] == "wrong_branch" for f in wrong_branch["findings"]),
       "wrong branch denies with wrong_branch failure class")

    run(repo, "checkout", "master")
    commit(repo, "main.txt", "main moved\n", "move master")
    run(repo, "push", "origin", "master")
    run(repo, "checkout", "codex/PREFLIGHT-1-clean")
    stale = store.repo_preflight(
        str(repo), project=P, task_id="PREFLIGHT-1", agent_id=AGENT,
        expected_branch="codex/PREFLIGHT-1-clean")
    ok(stale["verdict"] == "deny" and stale["base_distance"]["behind"] == 1,
       "branch behind canonical base denies as stale")
    ok(any(f["failure_class"] == "stale_base" for f in stale["findings"]),
       "stale report uses stale_base failure class")

    wrong_repo_root = Path(_TMP) / "wrong-repo"
    run(_TMP, "init", str(wrong_repo_root))
    run(wrong_repo_root, "config", "user.email", "switchboard@example.test")
    run(wrong_repo_root, "config", "user.name", "Switchboard Test")
    commit(wrong_repo_root, "base.txt", "base\n", "base")
    run(wrong_repo_root, "branch", "-M", "master")
    run(wrong_repo_root, "remote", "add", "origin", "git@github.com:someone/else.git")
    wrong_repo = store.repo_preflight(str(wrong_repo_root), project=P)
    ok(wrong_repo["verdict"] == "deny" and
       any(f["failure_class"] == "wrong_repo" for f in wrong_repo["findings"]),
       "GitHub origin outside project repo topology denies as wrong_repo")

    lease_repo, _ = make_repo("leased")
    lease = store.claim_resources(
        "claude/PREFLIGHT-1-other", "worktree", [str(lease_repo)],
        task_id="PREFLIGHT-1", project=P)
    leased = store.repo_preflight(
        str(lease_repo), project=P, task_id="PREFLIGHT-1", agent_id=AGENT,
        expected_branch="codex/PREFLIGHT-1-clean")
    ok(lease.get("lease_id") and leased["verdict"] == "deny" and
       any(f["failure_class"] == "shared_worktree_collision" for f in leased["findings"]),
       "active worktree lease held by another agent denies")

    session_task = store.create_task(
        {"workstream_id": "SESSION", "title": "preflight hygiene writeback"},
        actor="test", project=P)
    session_repo, _ = make_repo("session")
    created = store.create_work_session({
        "task_id": session_task["task_id"],
        "agent_id": "codex/SESSION-3-repo-preflight",
        "repo_role": "canonical",
        "branch": "codex/PREFLIGHT-1-clean",
        "worktree_path": str(session_repo),
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": "unknown",
        "policy_profile": "code_strict",
    }, actor="test", project=P)
    preflighted = store.preflight_work_session(
        created["work_session"]["work_session_id"], actor="test", project=P,
        expected_branch="codex/PREFLIGHT-1-clean")
    updated = preflighted["updated"]["work_session"]
    ok(preflighted["preflight"]["verdict"] == "pass" and
       updated["dirty_status"] == "clean" and
       updated["hygiene"]["repo_preflight"]["schema"] == store.REPO_PREFLIGHT_SCHEMA,
       "preflight_work_session writes hygiene, dirty status, and typed schema")

    # SESSION-14: remediation field + co-change contracts --------------------------
    dirty_finding = next(
        (f for f in dirty["findings"] if f.get("failure_class") == "dirty_worktree"), None)
    ok(bool(dirty_finding and dirty_finding.get("remediation")),
       "dirty_worktree finding includes remediation")

    lock_repo, _ = make_repo("lock-contract")
    commit(lock_repo, "package.json", '{"name":"demo","version":"1.0.0"}\n', "add package.json")
    # Establish a lockfile at the shared base, then change package.json alone.
    commit(lock_repo, "package-lock.json", '{"lockfileVersion":3}\n', "add lock")
    run(lock_repo, "checkout", "master")
    run(lock_repo, "merge", "--ff-only", "codex/PREFLIGHT-1-clean")
    run(lock_repo, "push", "origin", "master")
    run(lock_repo, "checkout", "codex/PREFLIGHT-1-clean")
    commit(lock_repo, "package.json",
           '{"name":"demo","version":"1.0.1"}\n', "bump package without lock")
    lock_denied = store.repo_preflight(
        str(lock_repo), project=P, task_id="PREFLIGHT-1", agent_id=AGENT,
        expected_branch="codex/PREFLIGHT-1-clean")
    lock_finding = next(
        (f for f in lock_denied["findings"]
         if f.get("failure_class") == "co_change_contract"
         and f.get("contract_id") == "npm_lock"),
        None)
    ok(lock_denied["verdict"] == "deny" and lock_finding is not None,
       "package.json without lockfile update denies via co_change_contract")
    ok(bool(lock_finding and "npm" in (lock_finding.get("remediation") or "").lower()),
       "co-change finding carries lockfile remediation")

    commit(lock_repo, "package-lock.json", '{"lockfileVersion":3,"packages":{}}\n',
           "update lock with package")
    lock_ok = store.repo_preflight(
        str(lock_repo), project=P, task_id="PREFLIGHT-1", agent_id=AGENT,
        expected_branch="codex/PREFLIGHT-1-clean")
    ok(not any(f.get("failure_class") == "co_change_contract" for f in lock_ok["findings"]),
       "package.json + package-lock.json co-change satisfies contract")

    # Topology can disable defaults with an empty co_change_contracts list.
    store.set_meta("repo_topology", {
        "schema": "switchboard.project_repo_topology.v1",
        "roles": {"canonical": {"repo": "6th-Element-Labs/projectplanner"}},
        "co_change_contracts": [],
    }, project=P)
    disabled_repo, _ = make_repo("lock-disabled")
    commit(disabled_repo, "package.json", '{"name":"x"}\n', "package only")
    disabled = store.repo_preflight(
        str(disabled_repo), project=P, task_id="PREFLIGHT-1", agent_id=AGENT,
        expected_branch="codex/PREFLIGHT-1-clean")
    ok(not any(f.get("failure_class") == "co_change_contract" for f in disabled["findings"]),
       "empty topology.co_change_contracts disables default contracts")
    # Restore switchboard topology so later env use isn't polluted.
    store.set_meta("repo_topology", {}, project=P)

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
