#!/usr/bin/env python3
"""Self-contained tests for the deliverable closure-gate registry (DELIVERABLES-14).

Covers manifest validity, that no registered check target dangles (pending
targets must be named), and the three ways a proof_requirements.gates entry
binds to a check: reference, override, and inline — plus fail-closed behaviour
on dangling/duplicate/malformed input.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import deliverable_gates as gates  # noqa: E402
from deliverable_gates import GateRegistryError, GateResolutionError  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def raises(exc_type, fn, message):
    try:
        fn()
    except exc_type:
        ok(True, message)
    except Exception as other:  # noqa: BLE001 - surface the wrong exception type
        ok(False, f"{message} (raised {type(other).__name__}: {other})")
    else:
        ok(False, f"{message} (no exception raised)")


def _write_manifest(tmpdir, obj):
    path = Path(tmpdir) / "manifest.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


# --- 1. committed manifest is valid ----------------------------------------
manifest = gates.load_manifest()
ok(manifest["schema"] == gates.REGISTRY_SCHEMA, "manifest declares the registry schema")
registry = gates.registry_gates()
ok(len(registry) >= 3, f"registry has the harness gates ({len(registry)} found)")

expected_ids = {
    "harness:concurrent_load_gate",
    "harness:test_concurrent_load_ratchet",
    "harness:test_mcp_observability",
    "harness:test_deliverable_closure_gate",
}
ok(expected_ids <= set(registry), f"registry exposes the spec/seed gate ids: {sorted(expected_ids)}")
ok(gates.gate_ids() == sorted(registry), "gate_ids() returns the sorted registry ids")

# --- 2. no dangling gate targets (pending must be named) --------------------
for gid, gate in sorted(registry.items()):
    if gate["kind"] != "script":
        continue
    target = REPO_ROOT / gate["command"][1]
    if gate.get("pending"):
        ok(bool(gate.get("pending_task")),
           f"{gid}: pending target is attributed to a task ({gate.get('pending_task')})")
    else:
        ok(target.exists(), f"{gid}: script target {gate['command'][1]} exists in the repo")

# --- 3. reference resolution ------------------------------------------------
ref = gates.resolve_gates({"gates": [{"id": "harness:concurrent_load_gate"}]})
ok(len(ref) == 1 and ref[0]["source"] == "registry", "referenced gate resolves from the registry")
ok(ref[0]["command"] == ["python3", "scripts/concurrent_load_gate.py"],
   "referenced gate keeps its registry command")
ok(ref[0]["required"] is True, "referenced gate defaults required=True from the manifest")

# --- 4. built-in scope gate -------------------------------------------------
scope = gates.resolve_gates({"gates": [{"id": "scope", "required": True}]})
ok(scope[0]["kind"] == "scope" and scope[0]["source"] == "builtin",
   "'scope' resolves to the built-in Gate 1 without a manifest entry")

# --- 5. override on a referenced gate ---------------------------------------
overridden = gates.resolve_gates({"gates": [
    {"id": "harness:test_mcp_observability", "required": True, "timeout_s": 120},
]})[0]
base_required = registry["harness:test_mcp_observability"]["required"]
ok(base_required is False and overridden["required"] is True,
   "proof_requirements can promote an optional registry gate to required")
ok(overridden["timeout_s"] == 120, "override replaces timeout_s")
ok(overridden["command"] == registry["harness:test_mcp_observability"]["command"],
   "override does not touch identity fields (command)")
ok(registry["harness:test_mcp_observability"]["required"] is False,
   "override does not mutate the shared registry entry")

# --- 6. inline per-deliverable gate definitions -----------------------------
inline = gates.resolve_gates({"gates": [
    {"id": "store:links_terminal", "kind": "store_check", "check": "links_terminal",
     "params": {"min_ratio": 1.0}, "required": True},
    {"id": "offline:runbook", "kind": "offline_evidence", "task_id": "DELIVERABLES-22"},
    {"id": "pytest:model", "kind": "pytest", "target": "test_deliverables_model.py"},
]})
ok(all(g["source"] == "inline" for g in inline), "inline gates report source=inline")
ok(inline[0]["check"] == "links_terminal" and inline[0]["params"] == {"min_ratio": 1.0},
   "inline store_check keeps check + params")
ok(inline[1]["task_id"] == "DELIVERABLES-22", "inline offline_evidence keeps task_id")
ok(inline[2]["target"] == "test_deliverables_model.py", "inline pytest keeps target")

# --- 7. fail-closed on dangling / duplicate / bad input ---------------------
raises(GateResolutionError,
       lambda: gates.resolve_gates({"gates": [{"id": "harness:does_not_exist", "required": True}]}),
       "unknown gate id with no inline kind fails closed")
raises(GateResolutionError,
       lambda: gates.resolve_gates({"gates": [{"id": "harness:concurrent_load_gate"},
                                              {"id": "harness:concurrent_load_gate"}]}),
       "duplicate gate id in proof_requirements fails closed")
raises(GateResolutionError,
       lambda: gates.resolve_gates({"gates": [{"kind": "script", "command": ["true"]}]}),
       "gate reference missing 'id' fails closed")
raises(GateResolutionError,
       lambda: gates.resolve_gates({"gates": "not-a-list"}),
       "non-list proof_requirements.gates fails closed")
raises(GateResolutionError,
       lambda: gates.resolve_gates({"gates": [
           {"id": "harness:concurrent_load_gate", "timeout_s": -5}]}),
       "an override that violates the gate schema fails closed")
raises(GateResolutionError,
       lambda: gates.resolve_gates({"gates": [
           {"id": "bad:inline", "kind": "store_check"}]}),
       "inline gate missing its kind's required field fails closed")

# --- 8. empty / scope-injection behaviour -----------------------------------
ok(gates.resolve_gates(None) == [], "None proof_requirements resolves to no gates")
ok(gates.resolve_gates({}) == [], "proof_requirements without gates resolves to no gates")
injected = gates.resolve_gates({"gates": [{"id": "harness:concurrent_load_gate"}]}, include_scope=True)
ok(injected[0]["id"] == "scope" and len(injected) == 2,
   "include_scope prepends the built-in scope gate when absent")
not_injected = gates.resolve_gates({"gates": [{"id": "scope"}]}, include_scope=True)
ok(len(not_injected) == 1, "include_scope does not double-add scope when already declared")

# --- 9. partition into scope vs functional (closure report shape) -----------
resolved = gates.resolve_gates({"gates": [
    {"id": "scope", "required": True},
    {"id": "harness:concurrent_load_gate", "required": True},
]})
scope_gates, functional_gates = gates.partition_gates(resolved)
ok(len(scope_gates) == 1 and len(functional_gates) == 1,
   "partition_gates splits scope from functional gates")

# --- 10. the real seed + dogfood proof_requirements actually resolve --------
seed_pr = {
    "schema": "switchboard.deliverable_proof_requirements.v1",
    "gates": [
        {"id": "scope", "required": True},
        {"id": "harness:test_deliverable_closure_gate", "required": True},
    ],
}
ok(len(gates.resolve_deliverable_gates({"proof_requirements": seed_pr})) == 2,
   "deliverable-closure-gate seed proof_requirements resolve against the registry")
dogfood_pr = {"gates": [
    {"id": "scope", "required": True},
    {"id": "harness:concurrent_load_gate", "required": True},
    {"id": "harness:test_concurrent_load_ratchet", "required": True},
    {"id": "harness:test_mcp_observability", "required": False},
]}
ok(len(gates.resolve_gates(dogfood_pr)) == 4,
   "mcp-agent-path-performance dogfood proof_requirements resolve against the registry")

# --- 11. malformed manifests are rejected (fail-closed loader) --------------
with tempfile.TemporaryDirectory() as tmp:
    raises(GateRegistryError,
           lambda: gates.load_manifest(_write_manifest(tmp, {"schema": "wrong", "gates": []}),
                                       use_cache=False),
           "wrong manifest schema is rejected")
    raises(GateRegistryError,
           lambda: gates.load_manifest(_write_manifest(tmp, {"schema": gates.REGISTRY_SCHEMA,
                                                              "gates": {}}), use_cache=False),
           "non-list gates is rejected")
    raises(GateRegistryError,
           lambda: gates.load_manifest(_write_manifest(tmp, {"schema": gates.REGISTRY_SCHEMA, "gates": [
               {"id": "dup", "kind": "script", "command": ["true"]},
               {"id": "dup", "kind": "script", "command": ["true"]}]}), use_cache=False),
           "duplicate manifest gate id is rejected")
    raises(GateRegistryError,
           lambda: gates.load_manifest(_write_manifest(tmp, {"schema": gates.REGISTRY_SCHEMA, "gates": [
               {"id": "x", "kind": "script"}]}), use_cache=False),
           "script gate without a command is rejected")
    raises(GateRegistryError,
           lambda: gates.load_manifest(_write_manifest(tmp, {"schema": gates.REGISTRY_SCHEMA, "gates": [
               {"id": "scope", "kind": "script", "command": ["true"]}]}), use_cache=False),
           "reserved id 'scope' is rejected in the manifest")
    raises(GateRegistryError,
           lambda: gates.load_manifest(_write_manifest(tmp, {"schema": gates.REGISTRY_SCHEMA, "gates": [
               {"id": "x", "kind": "bogus"}]}), use_cache=False),
           "unknown gate kind is rejected")
    # mtime cache returns the validated object without re-reading a changed-then-restored file
    good = _write_manifest(tmp, {"schema": gates.REGISTRY_SCHEMA, "gates": [
        {"id": "harness:x", "kind": "script", "command": ["python3", "x.py"]}]})
    first = gates.load_manifest(good)
    ok(gates.load_manifest(good) is first, "manifest load is mtime-cached")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
