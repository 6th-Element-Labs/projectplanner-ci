#!/usr/bin/env python3
"""ARCH-MS-57: move comments + plan_chat; delete dead dispatch."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms57-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

passed = failed = 0
SHELL_BEFORE = 2346


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    for name in (
        "switchboard.storage.repositories.plan_chat",
        "plan_chat_store",
    ):
        try:
            importlib.import_module(name)
            ok(True, f"{name} imports cleanly")
        except Exception as exc:  # noqa: BLE001
            ok(False, f"{name} import failed: {exc!r}")

    ok((ROOT / "src/switchboard/storage/repositories/plan_chat.py").is_file(),
       "plan_chat.py exists under storage/repositories")
    ok((ROOT / "plan_chat_store.py").is_file(),
       "plan_chat_store.py shim exists at repo root")

    from switchboard.storage.repositories import plan_chat as chat_repo  # noqa: E402
    from switchboard.storage.repositories import tasks as tasks_repo  # noqa: E402
    import plan_chat_store  # noqa: E402
    import store  # noqa: E402
    from switchboard.api.routers import plan_chat as plan_chat_router  # noqa: E402

    ok(plan_chat_store.add_chat is chat_repo.add_chat,
       "plan_chat_store shim re-exports add_chat")
    ok(store.add_chat is chat_repo.add_chat,
       "store facade delegates add_chat")
    ok(store.clear_chat is chat_repo.clear_chat,
       "store facade delegates clear_chat")
    ok(store.recent_chat is chat_repo.recent_chat,
       "store facade delegates recent_chat")
    ok(store.add_comment is tasks_repo.add_comment,
       "store facade delegates add_comment from tasks repo")
    ok(store.add_comment.__module__
       == "switchboard.storage.repositories.tasks",
       "add_comment lives under switchboard.storage.repositories.tasks")
    ok(store.add_chat.__module__
       == "switchboard.storage.repositories.plan_chat",
       "add_chat lives under switchboard.storage.repositories.plan_chat")
    ok(isinstance(store.plan_chat_repository, chat_repo.StorePlanChatRepository),
       "store.plan_chat_repository is StorePlanChatRepository")

    ok(not (ROOT / "src/switchboard/storage/repositories/shell.py").is_file(),
       "shell residual deleted (ARCH-MS-64)")
    tasks_src = (ROOT / "src/switchboard/storage/repositories/tasks.py").read_text()
    chat_src = (ROOT / "src/switchboard/storage/repositories/plan_chat.py").read_text()
    router_src = (ROOT / "src/switchboard/api/routers/plan_chat.py").read_text()

    ok("def add_comment(" in tasks_src
       and "INSERT INTO activity" in tasks_src,
       "add_comment SQL lives in tasks.py")
    ok("def add_chat(" in chat_src
       and "INSERT INTO chat" in chat_src
       and "def recent_chat(" in chat_src,
       "plan chat SQL helpers live in plan_chat.py")
    ok("from switchboard.storage.repositories import plan_chat as plan_chat_repo" in router_src
       and "plan_chat_repo.recent_chat" in router_src
       and "plan_chat_repo.add_chat" in router_src
       and "store.recent_chat" not in router_src
       and "store.add_chat" not in router_src
       and "store.clear_chat" not in router_src,
       "plan_chat router imports repo directly (no store chat helpers)")

    # Dead dispatch: no defs or callers remain in product/test code (exclude this file).
    repo_py = (
        list((ROOT / "src").rglob("*.py"))
        + list(ROOT.glob("*.py"))
        + [p for p in (ROOT / "tests").rglob("*.py")
           if p.name != "test_arch_ms57_comments_plan_chat.py"]
    )
    dispatch_hits = []
    for path in repo_py:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "add_dispatch(" in text or "latest_dispatch(" in text:
            dispatch_hits.append(str(path.relative_to(ROOT)))
    ok(not dispatch_hits,
       f"no add_dispatch/latest_dispatch defs/callers remain ({dispatch_hits})")

    store.init_db("switchboard")
    created = store.create_task({
        "title": "comment smoke",
        "status": "Not Started",
        "workstream_id": "ARCH-MS",
        "depends_on": [],
    }, actor="cursor/test", project="switchboard")
    tid = created["task_id"]
    hydrated = store.add_comment(tid, "cursor/test", "hello from MS-57",
                                 project="switchboard")
    ok(hydrated and hydrated.get("task_id") == tid,
       "add_comment hydrates task via tasks repository")
    activity = hydrated.get("activity") or []
    ok(any((a.get("payload") or {}).get("text") == "hello from MS-57"
           or (isinstance(a.get("payload"), str) and "hello from MS-57" in a.get("payload", ""))
           for a in activity)
       or any(a.get("kind") == "comment" for a in activity),
       "comment appears on hydrated task activity")

    lean = store.add_comment(tid, "cursor/test", "lean",
                             project="switchboard", hydrate_task=False)
    ok(lean == {"task_id": tid},
       "add_comment hydrate_task=False returns lean payload")

    store.add_chat("plan", "user", "ping", project="switchboard")
    store.add_chat("plan", "assistant", "pong", project="switchboard")
    msgs = store.recent_chat("plan", 10, project="switchboard")
    ok(len(msgs) >= 2 and msgs[-1]["content"] == "pong",
       "plan_chat recent_chat round-trip via store facade")
    store.clear_chat("plan", project="switchboard")
    ok(store.recent_chat("plan", 10, project="switchboard") == [],
       "plan_chat clear_chat empties session")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
