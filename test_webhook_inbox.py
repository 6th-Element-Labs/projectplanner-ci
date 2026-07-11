#!/usr/bin/env python3
"""PERF-1: durable webhook inbox — accept-and-ack, never drop.

Proves the DONE criteria:
  * a synthetic burst that today drops deliveries (applier lock-storming) loses ZERO
    with the inbox — enqueue is the durable commit point, decoupled from apply;
  * the drain worker is idempotent on replay;
  * inbox depth is observable.
"""
import os
import shutil
import sys
import tempfile
import threading

_TMP = tempfile.mkdtemp(prefix="webhook-inbox-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ.pop("PM_GITHUB_WEBHOOK_SECRET", None)  # dev mode: signature check passes
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import github_sync  # noqa: E402
import store  # noqa: E402
import webhook_inbox  # noqa: E402

P = "switchboard"
REPO = "6th-Element-Labs/projectplanner"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def pr_body(task_id, number, sha, action="opened", merged=False, merge_sha=""):
    pr = {
        "number": number,
        "title": f"fix({task_id}): durable webhook inbox",
        "body": "",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "head": {"ref": f"codex/{task_id}-perf1", "sha": sha,
                 "repo": {"full_name": REPO}},
        "base": {"ref": "master"},
    }
    if merged:
        pr["merged"] = True
        pr["merge_commit_sha"] = merge_sha
    return {"action": action, "repository": {"full_name": REPO, "name": "projectplanner",
                                             "default_branch": "master"},
            "pull_request": pr}


def activity_count(task_id, kind):
    with store._conn(P) as c:
        return c.execute("SELECT COUNT(*) FROM activity WHERE task_id=? AND kind=?",
                         (task_id, kind)).fetchone()[0]


import json  # noqa: E402

try:
    store.init_project_registry()
    store.init_db(P)
    store.set_project_repo_topology(
        project=P, canonical_repo=REPO,
        public_ci_repo="6th-Element-Labs/public-ci",
        public_repo="6th-Element-Labs/projectplanner-public")

    # ---- 1. Never-drop under a broken (lock-storming) applier ------------------
    # Simulate the exact failure that drops deliveries today: the provenance apply
    # blows up on the request path. With the inbox, enqueue already committed the
    # event, so nothing is lost — the drain retries and converges.
    N = 40
    tasks = [store.create_task({"workstream_id": "PERF", "title": f"burst {i}"},
                               actor="seed", project=P)["task_id"] for i in range(N)]

    original_handle_pr = github_sync.handle_pr
    github_sync.handle_pr = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("database is locked"))

    for i, tid in enumerate(tasks):
        body = json.dumps(pr_body(tid, 1000 + i, f"head{i}")).encode()
        enq = webhook_inbox.enqueue_event(
            P, delivery_guid=f"burst-{i}", event="pull_request",
            payload_bytes=body, headers={"X-GitHub-Event": "pull_request"})
        if not enq["enqueued"]:
            failed += 1

    depth_after_burst = webhook_inbox.inbox_depth(P)
    ok(depth_after_burst["pending"] == N and depth_after_burst["total"] == N,
       f"burst of {N} deliveries all durably enqueued despite broken applier (zero loss)")

    drain_broken = webhook_inbox.drain(P)
    ok(drain_broken["applied"] == 0 and webhook_inbox.inbox_depth(P)["pending"] == N,
       "broken applier applies nothing but LOSES nothing — all still pending for retry")

    github_sync.handle_pr = original_handle_pr  # applier recovers
    drain_ok = webhook_inbox.drain(P)
    depth_drained = webhook_inbox.inbox_depth(P)
    ok(drain_ok["applied"] == N and depth_drained["pending"] == 0
       and depth_drained["applied"] == N,
       "recovered drain applies all N with zero loss")
    ok(all(store.get_task(t, project=P)["status"] == "In Review" for t in tasks),
       "every burst task reached In Review once drained")

    # ---- 2. Redelivery dedup on delivery guid ---------------------------------
    redeliver = [webhook_inbox.enqueue_event(
        P, delivery_guid=f"burst-{i}", event="pull_request",
        payload_bytes=json.dumps(pr_body(tasks[i], 1000 + i, f"head{i}")).encode(),
        headers={}) for i in range(N)]
    ok(all(r["duplicate"] and not r["enqueued"] for r in redeliver)
       and webhook_inbox.inbox_depth(P)["total"] == N,
       "GitHub redelivery of the same guids is deduped — no double-enqueue")

    # ---- 3. Concurrent burst is race-safe & lossless --------------------------
    M = 50
    conc_tasks = [store.create_task({"workstream_id": "PERF", "title": f"conc {i}"},
                                    actor="seed", project=P)["task_id"] for i in range(M)]
    barrier = threading.Barrier(M)

    def fire(i):
        barrier.wait()
        webhook_inbox.enqueue_event(
            P, delivery_guid=f"conc-{i}", event="pull_request",
            payload_bytes=json.dumps(pr_body(conc_tasks[i], 2000 + i, f"chead{i}")).encode(),
            headers={})

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(M)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with store._conn(P) as c:
        conc_rows = c.execute(
            "SELECT COUNT(*) FROM webhook_inbox WHERE delivery_guid LIKE 'conc-%'"
        ).fetchone()[0]
    ok(conc_rows == M, f"concurrent burst of {M} enqueues stored all M distinct rows (no lost writes)")

    webhook_inbox.drain(P)
    ok(all(store.get_task(t, project=P)["status"] == "In Review" for t in conc_tasks),
       "concurrent burst tasks all applied after drain")

    # ---- 4. Idempotent replay -------------------------------------------------
    empty = webhook_inbox.drain(P)
    ok(empty["applied"] == 0 and empty["scanned"] == 0,
       "re-drain with nothing pending is a no-op (already-applied rows never re-selected)")

    sample = tasks[0]
    opened_before = activity_count(sample, "git.pr_opened")
    with store._conn(P) as c:
        c.execute("UPDATE webhook_inbox SET status='pending' WHERE delivery_guid=?",
                  ("burst-0",))
    replay = webhook_inbox.drain(P)
    ok(replay["applied"] == 1
       and activity_count(sample, "git.pr_opened") == opened_before
       and store.get_task(sample, project=P)["status"] == "In Review",
       "forced replay re-applies idempotently — no duplicate activity, no status churn")

    # ---- 5. Merge provenance flows through the inbox --------------------------
    merge_task = store.create_task({"workstream_id": "PERF", "title": "merge via inbox"},
                                   actor="seed", project=P)["task_id"]
    webhook_inbox.enqueue_event(
        P, delivery_guid="open-merge", event="pull_request",
        payload_bytes=json.dumps(pr_body(merge_task, 3001, "mhead")).encode(), headers={})
    webhook_inbox.enqueue_event(
        P, delivery_guid="close-merge", event="pull_request",
        payload_bytes=json.dumps(pr_body(merge_task, 3001, "mhead", action="closed",
                                          merged=True, merge_sha="mergesha1")).encode(),
        headers={})
    webhook_inbox.drain(P)
    merged_task = store.get_task(merge_task, project=P)
    ok(merged_task["status"] == "Done"
       and merged_task["git_state"]["merged_sha"] == "mergesha1",
       "PR open+merge deliveries drain to Done with merged_sha provenance")

    # ---- 6. Observability -----------------------------------------------------
    depth = webhook_inbox.inbox_depth(P)
    ok(depth["schema"] == "switchboard.webhook_inbox_depth.v1"
       and "by_status" in depth and "oldest_pending_age_s" in depth
       and depth["pending"] == 0,
       "inbox depth is observable: schema + by_status counts + oldest-pending age")

    print("\n%d passed, %d failed" % (passed, failed))
    raise SystemExit(1 if failed else 0)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)
