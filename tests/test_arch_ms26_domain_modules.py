#!/usr/bin/env python3
"""ARCH-MS-26: domain modules proof gate — access, board, coordination, deliverables, provenance."""
from __future__ import annotations

import importlib

from path_setup import ROOT

from switchboard.domain.access.identity import (
    binding_for_principal,
    binding_for_registered_agent,
    binding_for_system_actor,
    is_unbound_system_actor,
    shared_token_binding_error,
    validate_system_actor_fields,
    write_binding_activity_payload,
)
from switchboard.domain.board.tasks import (
    EDITABLE_TASK_FIELDS,
    READY_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    apply_terminal_done_view,
    block_done_without_provenance,
    build_dependency_state,
    dependency_rows_from_lookup,
    is_terminal_done_task,
    normalize_depends_on,
    rationale_state,
)
from switchboard.domain.coordination.delivery import classify_agent_delivery
from switchboard.domain.coordination.terminal import (
    TERMINAL_RECEIPT_STATUSES,
    TERMINAL_RUNNER_STATUSES,
    TERMINAL_WAKE_STATUSES,
)
from switchboard.domain.deliverables.lifecycle import (
    DELIVERABLE_STATUSES,
    done_requires_closure_grade,
    normalize_deliverable_id,
    normalize_project_board_id,
    validate_deliverable_status,
)
from switchboard.domain.provenance.git import (
    EVIDENCE_HASH_RE,
    has_done_provenance,
    offline_evidence_from_state,
    provenance_summary,
    valid_evidence_hash,
)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# --- package skeleton --------------------------------------------------------
for name in (
    "switchboard.domain.access",
    "switchboard.domain.board",
    "switchboard.domain.coordination",
    "switchboard.domain.deliverables",
    "switchboard.domain.provenance",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

for subpath in (
    "src/switchboard/domain/access/__init__.py",
    "src/switchboard/domain/board/__init__.py",
    "src/switchboard/domain/coordination/__init__.py",
    "src/switchboard/domain/deliverables/__init__.py",
    "src/switchboard/domain/provenance/__init__.py",
):
    ok((ROOT / subpath).is_file(), f"{subpath} exists on disk")

# --- access ------------------------------------------------------------------
ok(is_unbound_system_actor("env-mcp-token"), "shared env token is unbound")
ok(not is_unbound_system_actor("cursor-agent"), "registered agent id is bound")
ok(binding_for_principal("human")["binding"] == "principal", "principal binding shape")
system_err = validate_system_actor_fields(
    "env-mcp-token", "reason", principal_actor="env-mcp-token")
ok(system_err and system_err["error"] == "system_actor_must_be_explicit",
   "system_actor cannot be another env token")
ok(binding_for_system_actor(
    principal_actor="env-mcp-token",
    principal_id="p-1",
    system_actor="reconcile-bot",
    system_reason="nightly",
)["binding"] == "explicit_system_actor", "explicit system actor binding")
ok(binding_for_registered_agent(
    agent_id="cursor/a",
    principal_actor="env-mcp-token",
    principal_id="p-1",
    binding="registered_agent",
)["agent_id"] == "cursor/a", "registered agent binding")
payload = write_binding_activity_payload({"binding": "principal", "actor": "human"})
ok(payload["binding"] == "principal" and payload["actor"] == "human",
   "write_binding_activity_payload projects audit fields")
ok(shared_token_binding_error(actor="env-mcp-token")["failure_class"] == "unbound_identity",
   "shared token binding error is structured")

# --- board -------------------------------------------------------------------
ok("depends_on" in EDITABLE_TASK_FIELDS, "editable fields include depends_on")
ok(normalize_depends_on("a-1, A-1, b-2") == ["A-1", "B-2"], "depends_on canonicalized")
rows = dependency_rows_from_lookup(["T-1"], {"T-1": {"title": "dep", "status": "Done"}})
state = build_dependency_state({"status": "Not Started"}, rows)
ok(state["satisfied"] and state["ready"], "satisfied deps mark task ready")
stale = rationale_state(
    "blocked on dependencies",
    {"status": "In Progress"},
    {"satisfied": True},
)
ok(stale["stale"] and "says_blocked_but_dependencies_satisfied" in stale["flags"],
   "rationale_state flags stale blocked language")
ok(is_terminal_done_task({"status": "Done", "git_state": {"merged_sha": "abc"}}),
   "terminal done requires provenance")
task = {
    "status": "Done",
    "git_state": {"merged_sha": "abc"},
    "agent_state": {"a": {}},
    "active_claims": [{"claim_id": "c1"}],
    "identity": {"active_agents": ["a"]},
}
apply_terminal_done_view(task)
ok(task.get("terminal_state", {}).get("terminal") and not task.get("active_claims"),
   "terminal done view suppresses stale derived fields")
ok(block_done_without_provenance()["reason"] == "done_requires_merge_provenance",
   "done without provenance is blocked with stable reason")
ok("Done" in TERMINAL_TASK_STATUSES and "Ready" in READY_TASK_STATUSES,
   "task status sets exported")

# --- coordination ------------------------------------------------------------
ok(callable(classify_agent_delivery), "delivery classifier is callable")
ok("completed" in TERMINAL_WAKE_STATUSES and "done" in TERMINAL_RECEIPT_STATUSES,
   "terminal coordination status sets exported")

# --- deliverables ------------------------------------------------------------
ok(normalize_deliverable_id("Outcome-Alpha") == "Outcome-Alpha",
   "deliverable id accepts explicit ids")
ok(normalize_project_board_id("", title="Helm Mission").startswith("mission-"),
   "project board id slugifies title")
ok(validate_deliverable_status("proposed") is None, "valid deliverable status accepted")
bad = validate_deliverable_status("bogus")
ok(bad and bad["error"] == "invalid status", "invalid deliverable status rejected")
closure = done_requires_closure_grade(
    deliverable_id="d-1", requested_status="done", last_closure_grade=None)
ok(closure and closure["error"] == "deliverable closure grade required",
   "done deliverable requires closure grade")
ok("done" in DELIVERABLE_STATUSES, "deliverable lifecycle statuses exported")

# --- provenance --------------------------------------------------------------
ok(has_done_provenance({"merged_sha": "deadbeef"}), "merged sha counts as done provenance")
offline_state = {"evidence": {"offline_evidence": {"verifier": "human", "evidence_hash": "x"}}}
ok(has_done_provenance(offline_state), "offline evidence counts as done provenance")
ok(offline_evidence_from_state(offline_state)["verifier"] == "human",
   "offline evidence extracted from git state")
summary = provenance_summary({"merged_sha": "abc", "pr_number": 12})
ok(summary["type"] == "github_pr_merged" and summary["terminal"], "provenance summary for merge")
ok(valid_evidence_hash("sha256:" + "a" * 64), "evidence hash accepts sha256 prefix")
ok(EVIDENCE_HASH_RE.pattern.startswith("^"), "evidence hash regex exported")

# --- store wiring ------------------------------------------------------------
import store

ok(store.EDITABLE == list(EDITABLE_TASK_FIELDS),
   "store.EDITABLE delegates to domain board tasks")
ok(store._normalize_depends_on("x-1, X-1") == ["X-1"],
   "store depends_on normalization delegates to domain")
ok(store._has_done_provenance({"merged_sha": "x"}), "store provenance helper delegates to domain")
ok(store.is_unbound_system_actor("env-auth-token"), "store identity helper delegates to domain")
ok(store.write_binding_activity_payload is write_binding_activity_payload,
   "store re-exports write_binding_activity_payload from domain")

print(f"\nARCH-MS-26 domain modules: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
