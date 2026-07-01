#!/usr/bin/env python3
"""Focused tests for plan health signal derivation.

Run:
    python3 test_signals.py
"""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="switchboard-signals-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

import store  # noqa: E402
import signals  # noqa: E402
import agent  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_project_registry()
    store.create_project("Vulkan", actor="test", github_repo="StevenRidder/OpenCPN")
    root = store.create_task({"workstream_id": "SEAM", "title": "done root", "status": "Done"},
                             project="vulkan")
    child = store.create_task(
        {"workstream_id": "PRESENT", "title": "ready child", "depends_on": [root["task_id"]]},
        project="vulkan",
    )
    store.set_task_summary(
        child["task_id"],
        "The task is blocked on dependencies and cannot start yet.",
        activity_cursor=1,
        project="vulkan",
    )
    blocked = store.create_task(
        {"workstream_id": "BACKEND", "title": "blocked backend", "status": "Blocked"},
        project="vulkan",
    )

    store.set_meta("critical_path", [blocked["task_id"], {"task_id": child["task_id"]}, 7],
                   project="vulkan")
    store.set_meta("consolidated_decisions", ["legacy scalar decision"], project="vulkan")
    store.set_meta("people", "legacy scalar people", project="vulkan")

    result = signals.compute_plan_signals(project="vulkan")
    ready_ids = {t["task_id"] for t in result["ready"]}
    critical_ids = {t["task_id"] for t in result["critical_slip"]}

    ok(result["counts"]["ready"] == 1 and child["task_id"] in ready_ids,
       "dynamic project health handles dependency lists and reports ready tasks")
    ok(result["counts"]["blocked"] == 1,
       "dynamic project health reports blocked tasks")
    ok(blocked["task_id"] in critical_ids,
       "critical_path supports legacy string task ids")
    ok(result["past_due_decisions"] == [],
       "malformed decision metadata is ignored instead of crashing")
    ok("Unassigned" in result["by_owner_next"],
       "malformed people metadata falls back to default owners")

    child_detail = store.get_task(child["task_id"], project="vulkan")
    ok(child_detail["dependency_state"]["satisfied"] and child_detail["dependency_state"]["ready"],
       "task detail reports dependency truth when all dependencies are Done")
    ok(child_detail["rationale_state"]["stale"] and
       child_detail["rationale_state"]["flags"] == ["says_blocked_but_dependencies_satisfied"],
       "task detail flags stale rationale that contradicts dependency truth")
    ok(child_detail.get("rationale") is None and "blocked on dependencies" in child_detail["rationale_raw"],
       "task detail suppresses stale primary rationale while preserving raw text")
    child_brief = agent._task_brief(child_detail, full=True)
    ok(child_brief["dependency_state"]["ready"] and child_brief["rationale_state"]["stale"]
       and child_brief["rationale"] is None and "blocked on dependencies" in child_brief["rationale_raw"],
       "MCP task brief exposes dependency_state/rationale_state without stale primary rationale")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
