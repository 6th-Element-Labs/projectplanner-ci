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

    # --- NARRATE-3: deliverable CEO-voice header ---
    board = store.create_project_board(
        {"id": "nar-mission", "title": "Narrator Mission", "kind": "mission", "status": "active",
         "end_state": "Deliverables show CEO narration."},
        actor="test", project=PROJECT)
    deliv = store.create_deliverable(
        {"id": "nar-deliverable", "board_id": board["id"], "title": "CEO narrator",
         "status": "in_progress", "end_state": "Every deliverable shows a CEO header.",
         "why_it_matters": "Keeps the CEO up to speed at a glance."},
        actor="test", project=PROJECT)
    dtask = store.create_task(
        {"workstream_id": "DLV", "title": "Wire the header", "status": "Not Started"},
        actor="test", project=PROJECT)
    store.link_task_to_deliverable(deliv["id"], PROJECT, dtask["task_id"],
                                   actor="test", project=PROJECT)

    dllm = Counter()
    dres = narrate.run_deliverables(project=PROJECT, _llm_fn=dllm)
    ok(dllm.calls == 1 and any(r["deliverable_id"] == "nar-deliverable" for r in dres),
       "run_deliverables narrates the deliverable header (1 LLM call)")
    ms = store.get_mission_status(project=PROJECT, deliverable_id="nar-deliverable")
    ok((ms.get("ceo_narrative") or "").startswith("CEO narration #1"),
       "mission_status exposes the CEO narrative header")
    ok((ms.get("ceo_narrative_state") or {}).get("stale") is False, "fresh header is not stale")

    # idle re-run: fingerprint unchanged -> zero LLM calls (the cost guarantee)
    narrate.run_deliverables(project=PROJECT, _llm_fn=dllm)
    ok(dllm.calls == 1, "idle deliverable re-run makes zero LLM calls")

    # a linked-task transition moves the fingerprint -> header regenerates and old text is stale
    store.update_task(dtask["task_id"], {"status": "Blocked"}, actor="test", project=PROJECT)
    ms_stale = store.get_mission_status(project=PROJECT, deliverable_id="nar-deliverable")
    ok((ms_stale.get("ceo_narrative_state") or {}).get("stale") is True,
       "header flagged stale after a linked-task transition")
    dres2 = narrate.run_deliverables(project=PROJECT, _llm_fn=dllm)
    ok(dllm.calls == 2 and len(dres2) == 1, "changed fingerprint triggers one header regeneration")
    ms_fresh = store.get_mission_status(project=PROJECT, deliverable_id="nar-deliverable")
    ok((ms_fresh.get("ceo_narrative") or "").startswith("CEO narration #2"),
       "header updated after regeneration and no longer stale")

    # NARRATE-6: enriched task context pulls the plan fields a bare title lacks.
    ctx = narrate._task_context({
        "title": "Wire the widget", "status": "Done",
        "description": "Adds the widget wiring.",
        "deliverable": "A working widget behind the flag.",
        "exit_criteria": "flag on, tests green",
        "depends_on": ["FOO-1"],
        "dependency_state": {"dependencies": [
            {"task_id": "FOO-1", "title": "Build the widget base", "status": "Done"}]},
        "provenance": {"label": "Merged code", "pr_url": "https://x/pr/42"},
        "git_state": {"evidence": {"subject": "feat: widget wiring behind PM_WIDGET flag"}},
        "activity": [],
    })
    ok("A working widget behind the flag." in ctx, "context includes the deliverable / definition of done")
    ok("flag on, tests green" in ctx, "context includes exit criteria")
    ok("Build the widget base" in ctx, "context includes dependency titles, not just ids")
    ok("feat: widget wiring behind PM_WIDGET flag" in ctx, "context includes the merged PR/commit summary")
    thin = narrate._task_context({"title": "x", "status": "Not Started", "activity": []})
    ok("Deliverable" not in thin and "Exit criteria" not in thin,
       "absent plan fields are omitted cleanly (no empty labels)")

finally:
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
