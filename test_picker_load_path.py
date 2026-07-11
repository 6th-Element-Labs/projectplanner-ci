#!/usr/bin/env python3
"""BUG-42: project and deliverable pickers stay off heavy boot paths."""
import inspect
import os
from pathlib import Path
from scripts.frontend_test_source import read_frontend_source
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="picker-load-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    import app as app_module  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  picker load proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

HOME = "picker-home"
TARGET = "picker-target"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


client = TestClient(app_module.app)
try:
    store.init_project_registry()
    store.create_project("Picker Home", project_id=HOME, actor="test")
    store.create_project("Picker Target", project_id=TARGET, actor="test")
    store.init_db(HOME)
    store.init_db(TARGET)
    deliverable = store.create_deliverable(
        {"id": "fast-picker", "title": "Fast picker", "status": "in_progress"},
        actor="test", project=HOME)
    task = store.create_task(
        {"workstream_id": "PERF", "title": "Linked task"},
        actor="test", project=TARGET)
    store.link_task_to_deliverable(
        deliverable["id"], TARGET, task["task_id"], actor="test", project=HOME)

    original_decorate = store._decorate_deliverable_task_links
    store._decorate_deliverable_task_links = lambda links: (_ for _ in ()).throw(
        AssertionError("picker path decorated task links"))
    try:
        response = client.get("/api/deliverables", params={"project": HOME, "view": "picker"})
    finally:
        store._decorate_deliverable_task_links = original_decorate
    rows = response.json().get("deliverables") or []
    ok(response.status_code == 200 and response.json().get("view") == "picker",
       "picker view returns 200 with an explicit response contract")
    ok(len(rows) == 1 and rows[0]["id"] == "fast-picker" and rows[0]["title"] == "Fast picker",
       "picker view returns the navigation metadata")
    ok(not any(key in rows[0] for key in ("task_links", "milestones", "progress")),
       "picker view omits task snapshots, links, milestones, and progress")

    full = client.get("/api/deliverables", params={"project": HOME}).json()["deliverables"][0]
    ok((full.get("task_links") or [{}])[0].get("task", {}).get("task_id") == task["task_id"],
       "default REST list preserves the full compatibility contract")
    ok(not inspect.iscoroutinefunction(app_module.list_projects),
       "/api/projects sync SQLite/auth work runs in FastAPI's threadpool")

    js = read_frontend_source(Path.cwd())
    index = Path("static/index.html").read_text(encoding="utf-8")
    picker_fetch = "fetch('api/deliverables?view=picker')"
    deliverables_start = "const initialDeliverablesReq = this.loadDeliverables()"
    board_start = "const boardReq = fetch('api/board')"
    ok(picker_fetch in js, "browser picker calls the metadata-only endpoint")
    ok(js.index(deliverables_start) < js.index(board_start),
       "deliverable picker request starts before the board critical path")
    ok("if (this._deliverablesPromise) return this._deliverablesPromise" in js,
       "concurrent header/mission picker loads share one in-flight request")
    ok("missionRefresh.addEventListener('click', () => this.refreshMissionPage(true))" in js,
       "manual refresh can still force fresh picker metadata")
    boot_idx = index.index("window.TAIKUN_PICKER_BOOT = boot")
    bootstrap_idx = index.index("bootstrap.bundle.min.js")
    ok(boot_idx < bootstrap_idx,
       "inline picker hydration starts before the blocking Bootstrap CDN script")
    ok("await boot.projects" in js and "await boot.deliverables" in js,
       "app boot reuses inline picker promises instead of issuing duplicate fetches")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
