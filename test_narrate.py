#!/usr/bin/env python3
"""NARRATE-2: CEO-voice task narrator — trigger queue, debounce, and stale-flag discipline.

Uses an injected fake LLM (no network). Verifies: create/status-change enqueue a marker;
the drain narrates and stores prose; get_task exposes it; an idle re-run costs zero calls;
a status transition re-enqueues and re-narrates; a stale narration is hidden with a flag."""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="narrate-test-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_NARRATE_INTERVAL"] = "0"  # no time-based debounce in tests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import narrate  # noqa: E402
import store  # noqa: E402

PROJECT = store.DEFAULT_PROJECT
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


class Counter:
    """Fake LLM that returns canned prose and counts calls."""
    def __init__(self):
        self.calls = 0

    def __call__(self, context):
        self.calls += 1
        return f"CEO narration #{self.calls} for the feature."


try:
    store.init_db(PROJECT)

    # 1. create_task enqueues a narration marker
    t = store.create_task({"workstream_id": "NAR", "title": "Ship the widget"},
                          actor="test", project=PROJECT)
    tid = t["task_id"]
    pending = store.list_pending_narrations(project=PROJECT)
    ok(any(p["task_id"] == tid and p["reason"] == "create" for p in pending),
       "create_task enqueues a 'create' narration marker")

    # 2. drain narrates and clears the marker; get_task exposes narration
    llm = Counter()
    res = narrate.run_pending(project=PROJECT, _llm_fn=llm)
    ok(len(res) == 1 and llm.calls == 1, "drain narrates the pending task (1 LLM call)")
    ok(store.list_pending_narrations(project=PROJECT) == [], "marker cleared after narration")
    got = store.get_task(tid, project=PROJECT)
    ok(got.get("narration", "").startswith("CEO narration #1"),
       "get_task exposes fresh narration")
    ok((got.get("narration_state") or {}).get("stale") is False,
       "fresh narration is not stale")

    # 3. idle re-run makes ZERO LLM calls (fingerprint unchanged) — the cost guarantee
    res2 = narrate.run_pending(project=PROJECT, _llm_fn=llm)
    ok(llm.calls == 1 and res2 == [], "idle re-run makes zero LLM calls")

    # 4. cosmetic edit (no status change) does NOT enqueue
    store.update_task(tid, {"description": "tweaked copy"}, actor="test", project=PROJECT)
    ok(store.list_pending_narrations(project=PROJECT) == [],
       "cosmetic (non-status) edit does not enqueue")

    # 5. a status transition re-enqueues and re-narrates
    store.update_task(tid, {"status": "In Review"}, actor="test", project=PROJECT)
    pend = store.list_pending_narrations(project=PROJECT)
    ok(any(p["task_id"] == tid and p["reason"] == "status_change" for p in pend),
       "status change enqueues a 'status_change' marker")
    res3 = narrate.run_pending(project=PROJECT, _llm_fn=llm)
    ok(llm.calls == 2 and len(res3) == 1, "status change triggers a fresh narration")
    got2 = store.get_task(tid, project=PROJECT)
    ok(got2.get("narration", "").startswith("CEO narration #2"), "narration updated after transition")

    # 6. trigger-status filter: a non-listed status is dropped without an LLM call
    os.environ["PM_NARRATE_TRIGGERS"] = "Done"  # only Done qualifies
    store.enqueue_narration(tid, status="In Progress", reason="status_change", project=PROJECT)
    res4 = narrate.run_pending(project=PROJECT, _llm_fn=llm)
    ok(llm.calls == 2 and res4 == [], "non-trigger status dropped without an LLM call")
    ok(store.list_pending_narrations(project=PROJECT) == [], "dropped marker is cleared")
    del os.environ["PM_NARRATE_TRIGGERS"]

    # 7. stale discipline: mutate stored fingerprint so it no longer matches current state
    store.set_task_narration(tid, "outdated prose", activity_cursor=1,
                             source_fingerprint="deadbeef", model="test", project=PROJECT)
    got3 = store.get_task(tid, project=PROJECT)
    ok((got3.get("narration_state") or {}).get("stale") is True,
       "narration flagged stale when fingerprint no longer matches")
    ok(got3.get("narration") is None and got3.get("narration_raw") == "outdated prose",
       "stale prose hidden from `narration`, preserved in `narration_raw`")

finally:
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
