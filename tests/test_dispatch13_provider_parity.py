#!/usr/bin/env python3
"""DISPATCH-13: provider parity and Connect-only lifecycle integration proof."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from path_setup import ROOT

from switchboard.connect import (
    Assignment,
    ConnectKernel,
    Discover,
    HostRuntimeConfig,
    LeaseState,
    Request,
    ResourceLimits,
    RuntimeCapability,
    build_launch_spec,
)


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(condition)
    failed += int(not condition)


now = [10_000.0]
sequence = iter(f"id-{index}" for index in range(40))
kernel = ConnectKernel(
    clock=lambda: now[0],
    identifier=lambda: next(sequence),
    offer_ttl_seconds=10,
    heartbeat_interval_seconds=5,
)
providers = (
    ("codex", "openai"),
    ("claude-code", "anthropic"),
    ("cursor", "cursor"),
)
limits = ResourceLimits(
    max_runtime_seconds=120,
    spend_limit_microunits=25_000,
    memory_limit_bytes=512 * 1024 * 1024,
)
for index, (runtime, provider) in enumerate(providers):
    kernel.enqueue(Assignment(
        assignment_id=f"assignment-{runtime}",
        principal_ref=f"agent/{runtime}/dispatch-13",
        work_ref="task:switchboard:DISPATCH-13",
        runtime=runtime,
        provider=provider,
        workspace_ref="repo:canonical",
        limits=limits,
        queued_at=now[0] + index,
    ))

capabilities = tuple(
    RuntimeCapability(runtime=runtime, provider=provider)
    for runtime, provider in providers
)
leases = []
for index, (runtime, _provider) in enumerate(providers):
    discover = Discover(
        host_id="host/provider-parity",
        nonce=f"provider-{index}",
        capabilities=capabilities,
        available_slots=len(providers) - index,
        observed_at=now[0],
    )
    offer = kernel.discover(discover)
    ok(offer is not None and offer.assignment.runtime == runtime,
       f"{runtime} receives the oldest compatible assignment through Discover/Offer")
    leases.append(kernel.request(Request(
        offer_id=offer.offer_id,
        host_id=offer.host_id,
        nonce=offer.nonce,
        requested_at=now[0],
    )))

ok(kernel.active_count("host/provider-parity") == 3,
   "three provider processes consume exactly three Connect capacity slots")

fixture = ROOT / "tests" / "fixtures" / "connect_provider_probe.py"
with tempfile.TemporaryDirectory(prefix="dispatch13-provider-parity-") as temp:
    temp_path = Path(temp)
    home = temp_path / "host-home"
    config_dir = home / ".switchboard"
    workspace = temp_path / "workspace"
    message_board = temp_path / "mcp-message-board.jsonl"
    config_dir.mkdir(parents=True)
    workspace.mkdir()
    (config_dir / "mcp.json").write_text(
        json.dumps({"message_board": str(message_board)}), encoding="utf-8")

    for lease in leases:
        runtime = lease.assignment.runtime
        config = HostRuntimeConfig(
            runtime=runtime,
            provider=lease.assignment.provider,
            executable=sys.executable,
            arguments_before_note=(str(fixture),),
        )
        spec = build_launch_spec(lease, config, workspace_path=str(workspace))
        env = {
            **os.environ,
            **spec.env_dict(),
            "HOME": str(home),
            "PROVIDER_RUNTIME": runtime,
        }
        completed = subprocess.run(
            spec.argv,
            cwd=spec.cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        ok(completed.returncode == 0,
           f"{runtime} boots and completes through its host-configured MCP")

    receipts = [
        json.loads(line) for line in message_board.read_text(encoding="utf-8").splitlines()
    ]

ok({row["provider_runtime"] for row in receipts} == {item[0] for item in providers},
   "Codex, Claude, and Cursor independently reach the same communication plane")
ok(all(row["used_host_mcp"] for row in receipts)
   and all("MCP" not in key for lease in leases
           for key in build_launch_spec(
               lease,
               HostRuntimeConfig(
                   runtime=lease.assignment.runtime,
                   provider=lease.assignment.provider,
                   executable=sys.executable,
                   arguments_before_note=(str(fixture),),
               ),
               workspace_path=str(ROOT),
           ).env_dict()),
   "MCP discovery belongs to each provider host, never the Connect assignment")
ok(len({row["note"].replace(row["principal_ref"], "<principal>")
        .replace(row["assignment_id"], "<assignment>") for row in receipts}) == 1,
   "all providers receive the same minimal assignment-note shape")

# Lifecycle authority remains entirely in Connect: a heartbeat keeps one lease
# live, expiry frees another slot, and kill frees the third without inspecting
# provider output or MCP traffic.
now[0] += 4
codex = kernel.heartbeat(leases[0].lease_id, leases[0].runner_id)
cursor = kernel.kill(leases[2].lease_id, reason="operator_stop")
now[0] += 6
expired = kernel.expire()
claude = kernel.get(leases[1].lease_id)
ok(codex.state is LeaseState.ACTIVE and kernel.get(codex.lease_id).active,
   "heartbeat alone keeps the Codex lease observable and active")
ok(claude is not None and claude.state is LeaseState.EXPIRED
   and any(item.lease_id == claude.lease_id for item in expired),
   "missed heartbeat expires Claude and releases its capacity")
ok(cursor.state is LeaseState.KILLED and cursor.terminal_reason == "operator_stop",
   "kill terminalizes Cursor without reading its work or communication")
ok(kernel.active_count("host/provider-parity") == 1,
   "expiry and kill release capacity while the heartbeating lease remains")

released = kernel.release(codex.lease_id, codex.runner_id)
ok(released.state is LeaseState.RELEASED and kernel.active_count() == 0,
   "normal provider exit releases the final lease and restores all capacity")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
