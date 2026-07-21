#!/usr/bin/env python3
"""DISPATCH-11: provider launchers are thin, equal, and content-blind."""

from __future__ import annotations

import ast
from dataclasses import replace

from path_setup import ROOT

from switchboard.connect import (
    Ack,
    Assignment,
    HostRuntimeConfig,
    LaunchRefused,
    LeaseState,
    ResourceLimits,
    build_launch_spec,
)


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(condition)
    failed += int(not condition)


def ack_for(runtime: str, provider: str) -> Ack:
    assignment = Assignment(
        assignment_id=f"assignment-{runtime}",
        principal_ref=f"agent/{runtime}",
        work_ref="switchboard:DISPATCH-11",
        runtime=runtime,
        provider=provider,
        workspace_ref="workspace:projectplanner",
        limits=ResourceLimits(
            max_runtime_seconds=3600,
            spend_limit_microunits=5000,
            memory_limit_bytes=1024,
        ),
        queued_at=1000.0,
    )
    return Ack(
        lease_id=f"lease-{runtime}",
        runner_id=f"runner-{runtime}",
        assignment=assignment,
        host_id="host/one",
        issued_at=1001.0,
        expires_at=4601.0,
        heartbeat_interval_seconds=30,
        last_heartbeat_at=1001.0,
    )


profiles = (
    HostRuntimeConfig(
        runtime="codex", provider="openai", executable="/opt/bin/codex",
        arguments_before_note=("exec",),
    ),
    HostRuntimeConfig(
        runtime="claude", provider="anthropic", executable="/opt/bin/claude",
        arguments_before_note=("-p",), arguments_after_note=("--output-format", "json"),
    ),
    HostRuntimeConfig(
        runtime="cursor", provider="cursor", executable="/opt/bin/cursor-agent",
        arguments_before_note=("-p",),
    ),
)

specs = []
for profile in profiles:
    ack = ack_for(profile.runtime, profile.provider)
    spec = build_launch_spec(ack, profile, workspace_path="/work/projectplanner")
    specs.append(spec)
    ok(spec.argv[0] == profile.executable,
       f"{profile.runtime} uses its host-local executable")
    ok(ack.assignment.assignment_id in spec.argv[-1 if not profile.arguments_after_note else -3],
       f"{profile.runtime} receives only the minimal assignment note")
    ok(spec.cwd == "/work/projectplanner" and spec.limits == ack.assignment.limits,
       f"{profile.runtime} carries host-resolved cwd and hard limits")

expected_environment_keys = {
    "SWITCHBOARD_CONNECT_ASSIGNMENT_ID",
    "SWITCHBOARD_CONNECT_LEASE_ID",
    "SWITCHBOARD_CONNECT_PRINCIPAL_REF",
    "SWITCHBOARD_CONNECT_RUNNER_ID",
    "SWITCHBOARD_CONNECT_WORK_REF",
    "SWITCHBOARD_CONNECT_WORKSPACE_REF",
}
ok(all(set(spec.env_dict()) == expected_environment_keys for spec in specs),
   "all providers receive the same metadata-only environment")
ok(all("TOKEN" not in key and "SECRET" not in key and "MCP" not in key
       for spec in specs for key in spec.env_dict()),
   "launch specs never mint credentials or communication configuration")

mismatch_code = ""
try:
    build_launch_spec(
        ack_for("codex", "openai"), profiles[1], workspace_path="/work/projectplanner",
    )
except LaunchRefused as exc:
    mismatch_code = exc.code
ok(mismatch_code == "runtime_mismatch", "provider configurations cannot cross runtimes")

terminal = replace(ack_for("codex", "openai"), state=LeaseState.KILLED)
try:
    build_launch_spec(terminal, profiles[0], workspace_path="/work/projectplanner")
except LaunchRefused as exc:
    terminal_code = exc.code
else:
    terminal_code = ""
ok(terminal_code == "lease_not_active", "terminal leases cannot launch processes")

launcher_path = ROOT / "src" / "switchboard" / "connect" / "launcher.py"
tree = ast.parse(launcher_path.read_text(encoding="utf-8"), filename=str(launcher_path))
forbidden_import_roots = {
    "dispatch", "store", "work_sessions_store", "runner_store",
    "switchboard.mcp", "switchboard.application", "switchboard.storage",
    "subprocess",
}
forbidden_symbols = {
    "claim", "work_session", "review", "evidence", "complete", "git", "mcp",
    "token", "credential", "transcript", "result",
}
violations: list[str] = []
for node in ast.walk(tree):
    modules: list[str] = []
    if isinstance(node, ast.Import):
        modules = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom) and node.module:
        modules = [node.module]
    for module in modules:
        if any(module == root or module.startswith(root + ".")
               for root in forbidden_import_roots):
            violations.append(f"import:{module}")
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        lowered = node.name.lower()
        if any(word in lowered for word in forbidden_symbols):
            violations.append(f"symbol:{node.name}")
ok(not violations, "launcher imports and symbols contain no workflow or process ownership")
for violation in violations:
    print(f"         {violation}")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
