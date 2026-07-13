#!/usr/bin/env python3
"""ARCH-MS-32: claim lifecycle under switchboard.storage.repositories.claims."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms32-claims-")
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
    "switchboard.storage.repositories.claims",
    "claims_store",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

ok((ROOT / "src/switchboard/storage/repositories/claims.py").is_file(),
   "claims.py exists under storage/repositories")
ok((ROOT / "claims_store.py").is_file(),
   "claims_store.py shim exists at repo root")

from switchboard.storage.repositories import claims as claims_repo  # noqa: E402
import claims_store  # noqa: E402
import store  # noqa: E402

ok(claims_store.claim_task is claims_repo.claim_task,
   "claims_store shim re-exports package claim_task")
ok(claims_store.complete_claim is claims_repo.complete_claim,
   "claims_store shim re-exports package complete_claim")
ok(store.claim_task is claims_repo.claim_task,
   "store facade delegates claim_task to package module")
ok(store.claim_next is claims_repo.claim_next,
   "store facade delegates claim_next to package module")
ok(store.complete_claim is claims_repo.complete_claim,
   "store facade delegates complete_claim to package module")
ok(store.abandon_claim is claims_repo.abandon_claim,
   "store facade delegates abandon_claim to package module")
ok(store.revoke_claim is claims_repo.revoke_claim,
   "store facade delegates revoke_claim to package module")
ok(store.claim_task.__module__ == "switchboard.storage.repositories.claims",
   "claim_task lives under switchboard.storage.repositories.claims")
ok(isinstance(store.claims_repository, claims_repo.StoreClaimsRepository),
   "store.claims_repository is StoreClaimsRepository")

try:
    store.init_project_registry()
    store.init_db("switchboard")
    created = store.create_task(
        {"workstream_id": "ARCH-MS", "title": "ms32 claim proof",
         "description": "claims repository extract"},
        actor="arch-ms32",
        project="switchboard",
    )
    ok(bool(created and created.get("task_id")),
       "create_task persists a task for claim proof")
    tid = created["task_id"]
    agent = "cursor/arch-ms32-proof"
    store.register_agent(agent, runtime="cursor", lane="ARCH-MS",
                         task_id=tid, project="switchboard", ttl_s=600)
    claimed = store.claim_task(tid, agent, project="switchboard", ttl_seconds=600)
    ok(bool(claimed and claimed.get("claimed") and claimed.get("claim_id")),
       "claim_task succeeds via package SQL")
    claim_id = claimed["claim_id"]
    active = store._active_task_claims_in
    # Enrichment helper is on the package and still reachabl via store star-import.
    with store._conn("switchboard") as c:
        rows = store._active_task_claims_in(c, tid)
    ok(any(r.get("id") == claim_id or r.get("claim_id") == claim_id
           or r.get("agent_id") == agent for r in (rows or [])),
       "_active_task_claims_in reads the active claim")
    abandoned = store.abandon_claim(claim_id, reason="ms32-proof",
                                    project="switchboard")
    ok(bool(abandoned and (abandoned.get("abandoned") or abandoned.get("ok")
                           or abandoned.get("status") in ("abandoned", "released"))),
       "abandon_claim releases the claim")

    # PERF-2: monkeypatches on store._claim_task_impl must be honored via facade.
    calls = []
    real_impl = store._claim_task_impl

    def patched_impl(*_a, **_k):
        calls.append(1)
        return {"claimed": True, "claim_id": "taskclaim-patched",
                "task": {"task_id": tid}}

    store._claim_task_impl = patched_impl
    try:
        patched = store.claim_task(tid, agent, project="switchboard")
        ok(len(calls) == 1 and patched and patched.get("claim_id") == "taskclaim-patched",
           "claim_task honors store._claim_task_impl monkeypatches via facade")
    finally:
        store._claim_task_impl = real_impl
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nARCH-MS-32 claims repository: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
