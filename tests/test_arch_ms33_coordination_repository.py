#!/usr/bin/env python3
"""ARCH-MS-33: coordination under switchboard.storage.repositories.coordination."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms33-coord-")
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
    "switchboard.storage.repositories.coordination",
    "coordination_store",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

ok((ROOT / "src/switchboard/storage/repositories/coordination.py").is_file(),
   "coordination.py exists under storage/repositories")
ok((ROOT / "coordination_store.py").is_file(),
   "coordination_store.py shim exists at repo root")

from switchboard.storage.repositories import coordination as coord_repo  # noqa: E402
import coordination_store  # noqa: E402
import store  # noqa: E402

ok(coordination_store.request_wake is coord_repo.request_wake,
   "coordination_store shim re-exports package request_wake")
ok(coordination_store.send_agent_message is coord_repo.send_agent_message,
   "coordination_store shim re-exports package send_agent_message")
ok(store.request_wake is coord_repo.request_wake,
   "store facade delegates request_wake to package module")
ok(store.claim_wake is coord_repo.claim_wake,
   "store facade delegates claim_wake to package module")
ok(store.complete_wake is coord_repo.complete_wake,
   "store facade delegates complete_wake to package module")
ok(store.send_agent_message is coord_repo.send_agent_message,
   "store facade delegates send_agent_message to package module")
ok(store.ack_message is coord_repo.ack_message,
   "store facade delegates ack_message to package module")
ok(store.request_unblock is coord_repo.request_unblock,
   "store facade delegates request_unblock to package module")
ok(store.sweep_coordination_monitors is coord_repo.sweep_coordination_monitors,
   "store facade delegates sweep_coordination_monitors to package module")
ok(store.request_wake.__module__ == "switchboard.storage.repositories.coordination",
   "request_wake lives under switchboard.storage.repositories.coordination")
ok(isinstance(store.coordination_repository, coord_repo.StoreCoordinationRepository),
   "store.coordination_repository is StoreCoordinationRepository")

try:
    store.init_project_registry()
    store.init_db("switchboard")
    # Smoke: messaging + monitor create/ack path
    sent = store.send_agent_message(
        "cursor/ms33-a", "cursor/ms33-b", "ms33 coordination proof",
        project="switchboard", requires_ack=True, ack_deadline_minutes=5)
    ok(bool(sent and (sent.get("message_id") or sent.get("id"))),
       "send_agent_message persists a directed message")
    mid = sent.get("message_id") or sent.get("id")
    inbox = store.list_unacked_messages("cursor/ms33-b", project="switchboard")
    ok(any((m.get("id") or m.get("message_id")) == mid for m in inbox),
       "list_unacked_messages returns the sent message")
    acked = store.ack_message(mid, response="ms33 ack", project="switchboard")
    ok(bool(acked and acked.get("acked_at")),
       "ack_message acknowledges the message")
    sweeps = store.sweep_coordination_monitors(project="switchboard")
    ok(isinstance(sweeps, dict),
       "sweep_coordination_monitors returns a summary dict")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nARCH-MS-33 coordination repository: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
