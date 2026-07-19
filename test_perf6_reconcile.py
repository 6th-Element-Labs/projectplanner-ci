#!/usr/bin/env python3
"""PERF-6: timer/reconcile discipline regression checks."""
import os
import shutil
import subprocess
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="perf6-reconcile-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jobs  # noqa: E402
import store  # noqa: E402
from db.connection import _conn  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _pr(repo, number, task_id):
    return {
        "number": number,
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "title": f"{task_id}: batched",
        "merged_at": None,
        "merge_commit_sha": "",
        "base": {"ref": "master", "repo": {"default_branch": "master"}},
        "head": {"ref": f"codex/{task_id}-batched", "sha": str(number).zfill(40)},
    }


try:
    original_graphql = store._github_prs_graphql
    repo = "example/perf6"
    keys = [(repo, 101), (repo, 102)]
    graphql_calls = []

    def fake_graphql(pr_keys, token=""):
        graphql_calls.append((tuple(pr_keys), token))
        return {key: _pr(key[0], key[1], f"PERF-{key[1]}") for key in pr_keys}

    store._github_prs_graphql = fake_graphql
    fetched, checks = store._fetch_github_prs(keys, token="tok")
    ok(len(fetched) == 2 and len(graphql_calls) == 1 and
       not checks.get("github_pr_rest_fallback_fetches"),
       "mutable PR state is fetched with one GraphQL batch and no REST fan-out")
    ok(checks.get("github_pr_fetch_mode") == "graphql" and
       checks.get("github_pr_graphql_queries") == 1,
       "batch fetch reports GraphQL mode and query count")

    store._github_prs_graphql = original_graphql

    store.init_project_registry()
    project = "perf6-incremental"
    store.create_project("PERF6 Incremental", project_id=project, actor="test")
    store.init_db(project)
    first = store.create_task({"workstream_id": "PERF", "title": "first"},
                              actor="test", project=project)
    second = store.create_task({"workstream_id": "PERF", "title": "second"},
                               actor="test", project=project)
    full = store.reconcile(project=project, incremental=True)
    full_checks = full.get("external_checks") or {}
    ok(full_checks.get("incremental") is True and full_checks.get("board_task_checks") == 2,
       "first incremental reconcile checks the complete small fixture inside one bounded page")
    store.update_task(second["task_id"], {"title": "second changed"}, actor="test", project=project)
    delta = store.reconcile(project=project, incremental=True)
    delta_checks = delta.get("external_checks") or {}
    ok(delta_checks.get("changed_task_count") == 1 and
       1 <= delta_checks.get("board_task_checks", 0) <= 200,
       "later incremental reconcile prioritizes the changed task inside a bounded page")
    ok(store.get_meta("reconcile.activity_cursor", project=project) == delta["activity_cursor"],
       "incremental reconcile persists the consumed activity cursor")

    # A large historical ledger must be page work, never an interactive full
    # scan. Populate 100k irrelevant rows, start in the middle, and prove one
    # call advances by exactly the requested bounded amount.
    with _conn(project) as c:
        c.executemany(
            "INSERT INTO activity(task_id,actor,kind,payload,created_at) VALUES "
            "(NULL,'perf6','history.noise','{}',?)",
            [(float(i),) for i in range(100_000)],
        )
        ids = c.execute(
            "SELECT MIN(id) AS first_id, MAX(id) AS last_id FROM activity "
            "WHERE kind='history.noise'"
        ).fetchone()
        midpoint = int(ids["first_id"]) + 50_000
        query_plan = [str(row["detail"]) for row in c.execute(
            "EXPLAIN QUERY PLAN SELECT id,task_id FROM activity "
            "WHERE id>? ORDER BY id LIMIT ?", (midpoint, 26)).fetchall()]
    store.set_meta("reconcile.activity_cursor", midpoint, project=project)
    started = time.monotonic()
    bounded = store.reconcile(
        project=project, incremental=True,
        activity_limit=25, task_limit=10, evidence_limit=50)
    elapsed = time.monotonic() - started
    bounded_checks = bounded.get("external_checks") or {}
    ok(bounded["activity_cursor"] - midpoint == 25
       and bounded_checks.get("activity_batch_limit") == 25
       and bounded_checks.get("activity_has_more") is True,
       "100k-row history advances by one exact 25-row activity batch")
    ok(elapsed < 1.5, f"bounded historical reconcile stays interactive ({elapsed:.3f}s)")
    ok(any("PRIMARY KEY" in detail or "rowid" in detail.lower() for detail in query_plan),
       f"historical activity paging uses the indexed id seek ({query_plan})")

    lock_path = os.path.join(_TMP, "single-flight.lock")
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl,sys,time; f=open(sys.argv[1],'w'); "
         "fcntl.flock(f.fileno(), fcntl.LOCK_EX); print('locked', flush=True); time.sleep(1)",
         lock_path],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 2
        while time.time() < deadline:
            if holder.stdout and holder.stdout.readline().strip() == "locked":
                break
        with jobs._single_flight_lock(lock_path) as acquired:
            ok(acquired is False, "single-flight lock refuses an overlapping reconcile run")
    finally:
        holder.terminate()
        holder.wait(timeout=2)

    ok(first["task_id"] != second["task_id"], "test fixture created distinct tasks")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nPERF-6 reconcile discipline: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
