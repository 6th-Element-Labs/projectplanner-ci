#!/usr/bin/env python3
"""DISPATCH-10: Connect is a DHCP-like lease kernel with a hard boundary."""

from __future__ import annotations

import ast
from pathlib import Path
import sys

from path_setup import ROOT

from switchboard.connect import (
    Assignment,
    ConnectKernel,
    ConnectRefused,
    Discover,
    LeaseState,
    Request,
    ResourceLimits,
    RuntimeCapability,
)


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(condition)
    failed += int(not condition)


now = [1000.0]
ids = iter(("one", "two", "three", "four", "five", "six"))
kernel = ConnectKernel(
    clock=lambda: now[0],
    identifier=lambda: next(ids),
    offer_ttl_seconds=10,
    heartbeat_interval_seconds=5,
)
assignment = Assignment(
    assignment_id="assignment-1",
    principal_ref="agent/connect-1",
    work_ref="switchboard:DISPATCH-10",
    runtime="codex",
    provider="openai",
    workspace_ref="workspace:projectplanner",
    limits=ResourceLimits(max_runtime_seconds=120, spend_limit_microunits=5000),
    queued_at=999.0,
)
kernel.enqueue(assignment)

discover = Discover(
    host_id="host/one",
    nonce="nonce-1",
    capabilities=(
        RuntimeCapability(runtime="codex", provider="openai"),
        RuntimeCapability(runtime="claude", provider="anthropic"),
    ),
    available_slots=1,
    observed_at=now[0],
)
offer = kernel.discover(discover)
ok(offer is not None, "Discover receives one compatible Offer")
ok(kernel.discover(discover) == offer, "retransmitted Discover returns the same Offer")
ok(kernel.discover(Discover(
    host_id="host/one", nonce="nonce-other",
    capabilities=(RuntimeCapability(runtime="codex", provider="openai"),),
    available_slots=1, observed_at=now[0],
)) is None, "outstanding Offers consume advertised capacity")

try:
    kernel.request(Request(
        offer_id=offer.offer_id,
        host_id="host/two",
        nonce=offer.nonce,
        requested_at=now[0],
    ))
except ConnectRefused as exc:
    mismatch = exc.code
else:
    mismatch = ""
ok(mismatch == "offer_binding_mismatch", "Request is bound to the discovering host")

lease = kernel.request(Request(
    offer_id=offer.offer_id,
    host_id=offer.host_id,
    nonce=offer.nonce,
    requested_at=now[0],
))
ok(lease.active and kernel.active_count("host/one") == 1,
   "Ack activates one capacity-accounted lease")
ok(kernel.request(Request(
    offer_id=offer.offer_id, host_id=offer.host_id,
    nonce=offer.nonce, requested_at=now[0],
)) == lease, "retransmitted Request returns the same Ack")
ok(lease.assignment.work_ref == "switchboard:DISPATCH-10",
   "Connect carries the work reference without interpreting it")
ok(lease.assignment.principal_ref == "agent/connect-1",
   "Ack tells the runtime which neutral principal it is")

now[0] += 4
lease = kernel.heartbeat(lease.lease_id, lease.runner_id)
ok(lease.last_heartbeat_at == now[0], "heartbeat refreshes liveness")
now[0] += 10
expired = kernel.expire()
ok(len(expired) == 1 and expired[0].state is LeaseState.EXPIRED,
   "missed heartbeats expire the lease and release capacity")
ok(kernel.active_count() == 0, "expired leases no longer consume capacity")

second = Assignment(
    assignment_id="assignment-2",
    principal_ref="agent/connect-2",
    work_ref="switchboard:DISPATCH-11",
    runtime="codex",
    provider="openai",
    workspace_ref="workspace:projectplanner",
    limits=ResourceLimits(max_runtime_seconds=120),
    queued_at=1001.0,
)
kernel.enqueue(second)
offer2 = kernel.discover(Discover(
    host_id="host/one", nonce="nonce-2",
    capabilities=(RuntimeCapability(runtime="codex", provider="openai"),),
    available_slots=1, observed_at=now[0],
))
lease2 = kernel.request(Request(
    offer_id=offer2.offer_id, host_id=offer2.host_id,
    nonce=offer2.nonce, requested_at=now[0],
))
killed = kernel.kill(lease2.lease_id, reason="operator_stop")
ok(killed.state is LeaseState.KILLED and killed.terminal_reason == "operator_stop",
   "kill terminalizes the lease")
ok(kernel.kill(lease2.lease_id, reason="duplicate").terminal_reason == "operator_stop",
   "kill is idempotent")

# Free-slot advertisements exclude already-running processes. Only outstanding
# Offers consume the advertised headroom.
capacity_now = [2000.0]
capacity_ids = iter(("a", "b", "c", "d"))
capacity_kernel = ConnectKernel(
    clock=lambda: capacity_now[0], identifier=lambda: next(capacity_ids),
)
for suffix in ("a", "b"):
    capacity_kernel.enqueue(Assignment(
        assignment_id=f"assignment-capacity-{suffix}",
        principal_ref=f"agent/capacity-{suffix}", work_ref=f"work:{suffix}",
        runtime="codex", provider="openai", workspace_ref="workspace:test",
        limits=ResourceLimits(max_runtime_seconds=120),
        queued_at=1998.0 if suffix == "a" else 1999.0,
    ))
capacity_discover = lambda nonce: Discover(
    host_id="host/capacity", nonce=nonce,
    capabilities=(RuntimeCapability(runtime="codex", provider="openai"),),
    available_slots=1, observed_at=capacity_now[0],
)
capacity_offer = capacity_kernel.discover(capacity_discover("capacity-1"))
capacity_kernel.request(Request(
    offer_id=capacity_offer.offer_id, host_id=capacity_offer.host_id,
    nonce=capacity_offer.nonce, requested_at=capacity_now[0],
))
ok(capacity_kernel.discover(capacity_discover("capacity-2")) is not None,
   "one advertised free slot remains usable beside an active lease")

provider_kernel = ConnectKernel(clock=lambda: 3000.0, identifier=lambda: "provider")
provider_kernel.enqueue(Assignment(
    assignment_id="assignment-provider", principal_ref="agent/provider",
    work_ref="work:provider", runtime="codex", provider="openai",
    workspace_ref="workspace:test", limits=ResourceLimits(max_runtime_seconds=120),
    queued_at=2999.0,
))
ok(provider_kernel.discover(Discover(
    host_id="host/provider", nonce="provider-mismatch",
    capabilities=(RuntimeCapability(runtime="codex", provider="other"),),
    available_slots=1, observed_at=3000.0,
)) is None, "Offer requires an exact runtime and provider capability")

expiry_now = [4000.0]
expiry_kernel = ConnectKernel(
    clock=lambda: expiry_now[0], identifier=lambda: "expiry",
    offer_ttl_seconds=10,
)
expiry_kernel.enqueue(Assignment(
    assignment_id="assignment-expiry", principal_ref="agent/expiry",
    work_ref="work:expiry", runtime="codex", provider="openai",
    workspace_ref="workspace:test", limits=ResourceLimits(max_runtime_seconds=120),
    queued_at=3999.0,
))
expiry_offer = expiry_kernel.discover(Discover(
    host_id="host/expiry", nonce="expiry-1",
    capabilities=(RuntimeCapability(runtime="codex", provider="openai"),),
    available_slots=1, observed_at=expiry_now[0],
))
expiry_now[0] += 11
try:
    expiry_kernel.request(Request(
        offer_id=expiry_offer.offer_id, host_id=expiry_offer.host_id,
        nonce=expiry_offer.nonce, requested_at=expiry_now[0],
    ))
except ConnectRefused as exc:
    expiry_code = exc.code
else:
    expiry_code = ""
ok(expiry_code == "offer_expired", "expired Offer returns a truthful refusal")

retention_now = [5000.0]
retention_ids = iter(("offer", "lease", "runner"))
retention_kernel = ConnectKernel(
    clock=lambda: retention_now[0], identifier=lambda: next(retention_ids),
    terminal_retention_seconds=5,
)
retention_kernel.enqueue(Assignment(
    assignment_id="assignment-retention", principal_ref="agent/retention",
    work_ref="work:retention", runtime="codex", provider="openai",
    workspace_ref="workspace:test", limits=ResourceLimits(max_runtime_seconds=120),
    queued_at=4999.0,
))
retention_offer = retention_kernel.discover(Discover(
    host_id="host/retention", nonce="retention-1",
    capabilities=(RuntimeCapability(runtime="codex", provider="openai"),),
    available_slots=1, observed_at=retention_now[0],
))
retention_lease = retention_kernel.request(Request(
    offer_id=retention_offer.offer_id, host_id=retention_offer.host_id,
    nonce=retention_offer.nonce, requested_at=retention_now[0],
))
retention_kernel.release(retention_lease.lease_id, retention_lease.runner_id)
retention_now[0] += 6
retention_kernel.expire()
ok(retention_kernel.get(retention_lease.lease_id) is None,
   "terminal lease records are pruned after bounded retention")

# Architecture ratchet: Connect may use only its own package and the standard library.
connect_root = ROOT / "src" / "switchboard" / "connect"
forbidden_import_roots = {
    "dispatch", "store", "work_sessions_store", "runner_store",
    "switchboard.mcp", "switchboard.application", "switchboard.storage",
}
forbidden_vocabulary = {
    "mcp", "claim_id", "work_session", "review_verdict", "evidence",
    "pr_url", "head_sha", "lifecycle_role", "complete_claim",
}
forbidden_content_fields = {
    "prompt", "instruction", "message", "content", "transcript", "tool",
    "tool_call", "result", "completion", "review", "evidence",
}
boundary_clean = True
violations: list[str] = []
for source_path in sorted(connect_root.glob("*.py")):
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    for node in ast.walk(tree):
        imported: list[str] = []
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported = [node.module]
        for module in imported:
            if any(module == root or module.startswith(root + ".")
                   for root in forbidden_import_roots):
                violations.append(f"{source_path.name}: imports {module}")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            lowered = node.name.lower()
            for word in forbidden_vocabulary:
                if word in lowered:
                    violations.append(f"{source_path.name}: symbol {node.name}")
    boundary_clean = boundary_clean and not violations
ok(boundary_clean, "Connect has no imports or symbols from the Communicate/workflow layer")
for violation in violations:
    print(f"         {violation}")

wire_keys: set[str] = set()


def collect_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            wire_keys.add(str(key).lower())
            collect_keys(item)
    elif isinstance(value, list):
        for item in value:
            collect_keys(item)


collect_keys(discover.to_dict())
collect_keys(offer.to_dict())
collect_keys(lease.to_dict())
ok(not (wire_keys & forbidden_vocabulary),
   "Connect wire messages contain no Communicate/workflow fields")
ok(not (wire_keys & forbidden_content_fields),
   "Connect wire messages contain no call content or inspection fields")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
