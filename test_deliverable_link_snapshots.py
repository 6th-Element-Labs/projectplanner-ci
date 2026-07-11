#!/usr/bin/env python3
"""BUG-31 — deliverable links use slim project batches, never full get_task."""
import os
import shutil
import tempfile


_TMP = tempfile.mkdtemp(prefix="deliverable-link-snapshots-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

import store


passed = failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")


try:
    store.init_project_registry()
    store.create_project("Link Home", project_id="link-home", actor="test")
    store.create_project("Link Tasks", project_id="link-tasks", actor="test")
    deliverable = store.create_deliverable(
        {"id": "batch-proof", "title": "Batch proof"}, project="link-home")
    task_ids = []
    for index in range(8):
        task = store.create_task(
            {"workstream_id": "BATCH", "title": f"Linked task {index}"},
            actor="test", project="link-tasks")
        task_ids.append(task["task_id"])
        store.link_task_to_deliverable(
            deliverable["id"], "link-tasks", task["task_id"], project="link-home")

    expected = store.get_task(task_ids[0], project="link-tasks")
    ninth = store.create_task(
        {"workstream_id": "BATCH", "title": "Linked task 8"},
        actor="test", project="link-tasks")
    real_get_task = store.get_task
    real_snapshots = store._deliverable_task_snapshots
    real_conn = store._conn
    snapshot_calls = []
    batch_queries = []

    def forbidden_get_task(*args, **kwargs):
        raise AssertionError("deliverable link path called full get_task")

    def tracked_snapshots(project, ids):
        snapshot_calls.append((project, list(ids)))
        return real_snapshots(project, ids)

    def traced_conn(*args, **kwargs):
        conn = real_conn(*args, **kwargs)
        conn.set_trace_callback(lambda sql: batch_queries.append(sql))
        return conn

    store.get_task = forbidden_get_task
    store._deliverable_task_snapshots = tracked_snapshots
    store._conn = traced_conn
    try:
        loaded = store.get_deliverable(deliverable["id"], project="link-home")
        check("eight links are decorated without full get_task",
              len(loaded["task_links"]) == 8)
        check("one snapshot batch serves all links from one project",
              snapshot_calls == [("link-tasks", task_ids)])
        evidence_tables = ("tasks", "task_git_state", "external_ci_runs",
                           "publication_evidence")
        batch_counts = {
            table: sum(f"FROM {table} " in query for query in batch_queries)
            for table in evidence_tables
        }
        check("task and evidence rows use four fixed batch queries",
              batch_counts == {table: 1 for table in evidence_tables})
        first = loaded["task_links"][0]["task"]
        check("slim snapshot preserves the public link shape",
              set(first) == {"task_id", "title", "status", "workstream",
                             "provenance", "external_ci", "publication"})
        check("batched evidence matches full task-detail semantics",
              first["provenance"] == expected["provenance"] and
              first["external_ci"] == expected["external_ci"] and
              first["publication"] == expected["publication"])

        snapshot_calls.clear()
        linked = store.link_task_to_deliverable(
            deliverable["id"], "link-tasks", ninth["task_id"], project="link-home")
        check("link validation also avoids full get_task",
              len(linked["task_links"]) == 9)
        check("link write uses one validation batch and one response batch",
              snapshot_calls == [
                  ("link-tasks", [ninth["task_id"]]),
                  ("link-tasks", task_ids + [ninth["task_id"]]),
              ])
    finally:
        store.get_task = real_get_task
        store._deliverable_task_snapshots = real_snapshots
        store._conn = real_conn
finally:
    shutil.rmtree(_TMP, ignore_errors=True)


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
