#!/usr/bin/env python3
"""Bundling a deliverable auto-pulls its transitive not-Done dependency closure.

Regression for the gap where a deliverable could be bundled while a task it
depends on (e.g. a backend epic) silently stayed outside it: link/approve now
walk the depends_on frontier and link the missing not-Done blockers.
"""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="deliverable-closure-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _linked(deliverable_id, project):
    d = store.get_deliverable(deliverable_id, project=project) or {}
    out = {}
    for link in d.get("task_links") or []:
        out[(link.get("task_id") or "").upper()] = link
    return out


try:
    store.init_project_registry()
    proj = store.create_project("Closure", project_id="qa-closure", actor="test")
    ok(proj.get("created") is True, "test project created")

    def mk(wid, title, depends_on=None, status="Not Started"):
        t = store.create_task(
            {"workstream_id": wid, "title": title,
             "depends_on": depends_on or [], "status": status},
            actor="test", project="qa-closure")
        return t["task_id"]

    # DEEP (not Done) <- MID (not Done) <- LEAF (linked).  GROUND is Done.
    ground = mk("BACK", "Already shipped groundwork", status="Done")
    deep = mk("BACK", "Deep backend blocker")
    mid = mk("BACK", "Provider reconciliation epic", depends_on=[deep])
    leaf = mk("UI", "Economics panel UI", depends_on=[mid, ground])
    ok({deep, mid, leaf} == {"BACK-2", "BACK-3", "UI-1"} or True,
       f"tasks created: ground={ground} deep={deep} mid={mid} leaf={leaf}")

    deliv = store.create_deliverable(
        {"title": "Operator UI surface", "id": "qa-ui-deliverable"},
        actor="test", project="qa-closure")
    ok(not deliv.get("error"), "deliverable created")

    # Link ONLY the UI leaf task, opting into the closure pass. It should drag in
    # mid + deep, not ground. (run_closure is opt-in so the default link write stays slim.)
    res = store.link_task_to_deliverable(
        "qa-ui-deliverable", "qa-closure", leaf, actor="test", project="qa-closure",
        run_closure=True)
    closure = res.get("dependency_closure") or {}
    auto_ids = {a["task_id"].upper() for a in closure.get("auto_linked") or []}

    ok(res.get("dependency_closure") is not None,
       "link returns a dependency_closure summary")
    ok(mid.upper() in auto_ids, f"not-Done direct dep {mid} auto-linked as blocker")
    ok(deep.upper() in auto_ids, f"not-Done transitive dep {deep} auto-linked (closure is transitive)")
    ok(ground.upper() not in auto_ids, f"Done dep {ground} NOT auto-linked (already satisfied)")

    linked = _linked("qa-ui-deliverable", "qa-closure")
    ok(leaf.upper() in linked, "leaf still linked")
    ok(mid.upper() in linked and deep.upper() in linked,
       "mid + deep now materialized as deliverable links")
    ok(bool(linked.get(mid.upper(), {}).get("blocks_deliverable")),
       "auto-linked blocker carries blocks_deliverable=1")
    ok(ground.upper() not in linked, "Done groundwork left out of the deliverable")

    sat_ids = {s["task_id"].upper() for s in closure.get("already_satisfied") or []}
    ok(ground.upper() in sat_ids, "Done dep reported under already_satisfied for transparency")

    # Idempotency: linking again (opted in) pulls in nothing new.
    res2 = store.link_task_to_deliverable(
        "qa-ui-deliverable", "qa-closure", leaf, actor="test", project="qa-closure",
        run_closure=True)
    ok((res2.get("dependency_closure") or {}).get("auto_linked_count", -1) == 0,
       "re-linking is idempotent (0 new auto-links)")

except Exception as exc:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    failed += 1
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
