#!/usr/bin/env python3
"""ARCH-MS-34: provenance under switchboard.storage.repositories.provenance."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms34-prov-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


for name in (
    "switchboard.storage.repositories.provenance",
    "provenance_store",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

ok((ROOT / "src/switchboard/storage/repositories/provenance.py").is_file(),
   "provenance.py exists under storage/repositories")
ok((ROOT / "provenance_store.py").is_file(),
   "provenance_store.py shim exists at repo root")

from switchboard.storage.repositories import provenance as prov_repo  # noqa: E402
import provenance_store  # noqa: E402
import store  # noqa: E402

ok(provenance_store.mark_task_merged is prov_repo.mark_task_merged,
   "provenance_store shim re-exports package mark_task_merged")
ok(provenance_store.reconcile is prov_repo.reconcile,
   "provenance_store shim re-exports package reconcile")
ok(store.mark_task_merged is prov_repo.mark_task_merged,
   "store facade delegates mark_task_merged to package module")
ok(store.mark_task_pr_opened is prov_repo.mark_task_pr_opened,
   "store facade delegates mark_task_pr_opened to package module")
ok(store.mark_task_offline_done is prov_repo.mark_task_offline_done,
   "store facade delegates mark_task_offline_done to package module")
ok(store.reconcile is prov_repo.reconcile,
   "store facade delegates reconcile to package module")
ok(store.github_webhook_deliveries is prov_repo.github_webhook_deliveries,
   "store facade delegates github_webhook_deliveries to package module")
ok(store.mark_task_merged.__module__ == "switchboard.storage.repositories.provenance",
   "mark_task_merged lives under switchboard.storage.repositories.provenance")
ok(isinstance(store.provenance_repository, prov_repo.StoreProvenanceRepository),
   "store.provenance_repository is StoreProvenanceRepository")

try:
    store.init_project_registry()
    store.init_db("switchboard")
    created = store.create_task(
        {"workstream_id": "ARCH-MS", "title": "ms34 provenance proof",
         "description": "provenance repository extract"},
        actor="arch-ms34",
        project="switchboard",
    )
    ok(bool(created and created.get("task_id")),
       "create_task persists a task for provenance proof")
    tid = created["task_id"]

    opened = store.mark_task_pr_opened(
        tid, pr_number=425, pr_url="https://github.com/6th-Element-Labs/projectplanner/pull/425",
        branch="cursor/ARCH-MS-34-extract-provenance",
        head_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        project="switchboard",
    )
    task_after_open = store.get_task(tid, project="switchboard")
    ok(bool(opened) and (task_after_open or {}).get("status") == "In Review",
       "mark_task_pr_opened moves task to In Review")
    git_state = (task_after_open or {}).get("git_state") or {}
    ok(int(git_state.get("pr_number") or 0) == 425,
       "mark_task_pr_opened persists task_git_state.pr_number")

    active = store.create_task(
        {"workstream_id": "ARCH-MS", "title": "active claim PR proof",
         "description": "PR evidence must not cross the hard handoff boundary"},
        actor="arch-ms34",
        project="switchboard",
    )
    active_tid = active["task_id"]
    claim = store.claim_task(
        active_tid, "codex/ARCH-MS-active-pr", actor="arch-ms34",
        project="switchboard",
    )
    ok(bool(claim and claim.get("claimed")),
       "active PR proof has an implementation claim")
    active_opened = store.mark_task_pr_opened(
        active_tid, pr_number=426,
        pr_url="https://github.com/6th-Element-Labs/projectplanner/pull/426",
        branch="codex/ARCH-MS-active-pr",
        head_sha="cccccccccccccccccccccccccccccccccccccccc",
        project="switchboard",
    )
    active_after_open = store.get_task(active_tid, project="switchboard")
    ok(
        bool(active_opened)
        and active_opened.get("review_transition_deferred") is True
        and (active_after_open or {}).get("status") == "In Progress",
        "mark_task_pr_opened records provenance without exposing review during active ownership",
    )
    active_git = (active_after_open or {}).get("git_state") or {}
    ok(int(active_git.get("pr_number") or 0) == 426,
       "deferred review transition still persists PR provenance")

    merged = store.mark_task_merged(
        tid, merged_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        pr_number=425,
        pr_url="https://github.com/6th-Element-Labs/projectplanner/pull/425",
        project="switchboard",
    )
    task_after_merge = store.get_task(tid, project="switchboard")
    ok(bool(merged) and (task_after_merge or {}).get("status") == "Done",
       "mark_task_merged moves task to Done")
    ok(((task_after_merge or {}).get("git_state") or {}).get("merged_sha")
       == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
       "mark_task_merged stamps merged_sha")

    report = store.reconcile(project="switchboard")
    ok(isinstance(report, dict) and "findings" in report and "external_checks" in report,
       "reconcile returns findings + external_checks")

    deliveries = store.github_webhook_deliveries(project="switchboard")
    ok(isinstance(deliveries, dict) and "delivered" in deliveries,
       "github_webhook_deliveries returns delivered probe")

    # PERF-2: monkeypatches on store._mark_task_pr_opened_impl must be honored.
    calls = []
    real_impl = store._mark_task_pr_opened_impl

    def patched_impl(*_a, **_k):
        calls.append(1)
        return {"ok": True, "status": "In Review", "patched": True}

    store._mark_task_pr_opened_impl = patched_impl
    try:
        created2 = store.create_task(
            {"workstream_id": "ARCH-MS", "title": "ms34 patch proof"},
            actor="arch-ms34", project="switchboard")
        tid2 = created2["task_id"]
        patched = store.mark_task_pr_opened(
            tid2, pr_number=1, pr_url="https://example.com/p/1",
            project="switchboard")
        ok(len(calls) == 1 and patched and patched.get("patched") is True,
           "mark_task_pr_opened honors store._mark_task_pr_opened_impl monkeypatches")
    finally:
        store._mark_task_pr_opened_impl = real_impl
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nARCH-MS-34 provenance repository: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
