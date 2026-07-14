#!/usr/bin/env python3
"""ARCH-MS-50: claims leases + agents/hosts drained into existing repositories."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms50-")
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
    "switchboard.storage.repositories.coordination",
    "claims_store",
    "coordination_store",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

import claims_store  # noqa: E402
import coordination_store  # noqa: E402
import store  # noqa: E402
from switchboard.storage.repositories import claims as claims_repo  # noqa: E402
from switchboard.storage.repositories import coordination as coord_repo  # noqa: E402

ok(claims_store.claim_files is claims_repo.claim_files,
   "claims_store shim re-exports claim_files")
ok(claims_store.claim_resources is claims_repo.claim_resources,
   "claims_store shim re-exports claim_resources")
ok(coordination_store.register_agent is coord_repo.register_agent,
   "coordination_store shim re-exports register_agent")
ok(coordination_store.register_host is coord_repo.register_host,
   "coordination_store shim re-exports register_host")

ok(store.claim_files is claims_repo.claim_files,
   "store facade delegates claim_files to claims")
ok(store.claim_resources is claims_repo.claim_resources,
   "store facade delegates claim_resources to claims")
ok(store.release_files is claims_repo.release_files,
   "store facade delegates release_files to claims")
ok(store.list_active_leases is claims_repo.list_active_leases,
   "store facade delegates list_active_leases to claims")
ok(store._risk_value is claims_repo._risk_value,
   "store facade delegates _risk_value to claims")
ok(store._executed_test_run_gate is claims_repo._executed_test_run_gate,
   "store facade delegates _executed_test_run_gate to claims")
ok(store.register_agent is coord_repo.register_agent,
   "store facade delegates register_agent to coordination")
ok(store.heartbeat is coord_repo.heartbeat,
   "store facade delegates heartbeat to coordination")
ok(store.register_host is coord_repo.register_host,
   "store facade delegates register_host to coordination")
ok(store.list_agent_hosts is coord_repo.list_agent_hosts,
   "store facade delegates list_agent_hosts to coordination")
ok(store.set_agent_state is coord_repo.set_agent_state,
   "store facade delegates set_agent_state to coordination")
ok(store.get_agent_state is coord_repo.get_agent_state,
   "store facade delegates get_agent_state to coordination")

ok(store.claim_files.__module__ == "switchboard.storage.repositories.claims",
   "claim_files lives under claims repository")
ok(store.register_agent.__module__ == "switchboard.storage.repositories.coordination",
   "register_agent lives under coordination repository")

shell_src = (ROOT / "src/switchboard/storage/repositories/shell.py").read_text()
claims_src = (ROOT / "src/switchboard/storage/repositories/claims.py").read_text()
coord_src = (ROOT / "src/switchboard/storage/repositories/coordination.py").read_text()

for name in (
    "def claim_files(",
    "def claim_resources(",
    "def list_active_leases(",
    "def _executed_test_run_gate(",
    "def _risk_value(",
):
    ok(name not in shell_src, f"shell residual no longer defines {name[4:].rstrip('(')}")
    ok(name in claims_src, f"claims repository owns {name[4:].rstrip('(')}")

for name in (
    "def register_agent(",
    "def register_host(",
    "def list_active_agents(",
    "def set_agent_state(",
    "def _host_row(",
):
    ok(name not in shell_src, f"shell residual no longer defines {name[4:].rstrip('(')}")
    ok(name in coord_src, f"coordination repository owns {name[4:].rstrip('(')}")

ok("def repo_preflight(" in shell_src, "repo_preflight remains in shell residual")
ok("def pre_tool_check(" in shell_src, "pre_tool_check remains in shell residual")
ok("def control_plane_probe(" in shell_src, "control_plane_probe remains in shell residual")
ok(len(shell_src.splitlines()) < 3500, "shell residual shrunk after ARCH-MS-50 extract")
ok(len(claims_src.splitlines()) > 1500, "claims repository grew with leases/evidence")
ok(len(coord_src.splitlines()) > 1400, "coordination repository grew with agents/hosts")

try:
    store.init_project_registry()
    store.init_db("switchboard")
    reg = store.register_agent(
        "ms50/arch-proof", "cursor", lane="ARCH-MS", project="switchboard",
    )
    ok(reg.get("agent_id") == "ms50/arch-proof", "register_agent via store façade")
    agents = store.list_active_agents(project="switchboard")
    ok(any(a.get("agent_id") == "ms50/arch-proof" for a in agents),
       "list_active_agents includes registered agent")
    lease = store.claim_files(
        "ms50/arch-proof", ["src/switchboard/storage/repositories/shell.py"],
        task_id="ARCH-MS-50", project="switchboard",
    )
    ok(bool(lease.get("lease_id")), f"claim_files via store façade ({lease.get('error')})")
    released = store.release_files(lease["lease_id"], project="switchboard")
    ok(released.get("released") is True, "release_files via store façade")
    rlease = store.claim_resources(
        "ms50/arch-proof", "task", ["ARCH-MS-50"], project="switchboard",
    )
    ok(bool(rlease.get("lease_id")), f"claim_resources via store façade ({rlease.get('error')})")
    host = store.register_host(
        {
            "host_id": "host-ms50-proof",
            "hostname": "proof",
            "runtimes": [{"runtime": "cursor"}],
            "limits": {"max_sessions": 2},
            "capacity": {"active_sessions": 0},
        },
        project="switchboard",
    )
    ok(host.get("host_id") == "host-ms50-proof",
       f"register_host via store façade ({host.get('error')})")
    created = store.create_task(
        {"workstream_id": "ARCH-MS", "title": "ms50 proof", "description": "x"},
        actor="arch-ms50", project="switchboard",
    )
    task_id = created["task_id"]
    state = store.set_agent_state(task_id, "ms50/arch-proof", {"note": "ok"},
                                  project="switchboard")
    ok(state.get("ms50/arch-proof", {}).get("note") == "ok",
       "set_agent_state via store façade")
    got = store.get_agent_state(task_id, project="switchboard")
    ok(got.get("ms50/arch-proof", {}).get("note") == "ok",
       "get_agent_state via store façade")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nARCH-MS-50 claims/leases/agents: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
