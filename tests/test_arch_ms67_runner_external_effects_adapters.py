#!/usr/bin/env python3
"""Focused proof for ARCH-MS-67 runner + external CI/effects adapter drain."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="arch-ms67-adapters-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from path_setup import ROOT, entrypoint_source  # noqa: E402
from switchboard.application.commands import claim_external_effect as effect_command  # noqa: E402
from switchboard.application.commands import merge_gate as merge_gate_command  # noqa: E402
from switchboard.application.commands import runner_control as runner_command  # noqa: E402
from switchboard.mcp.tools import external_effects as external_effects_tools  # noqa: E402
from switchboard.mcp.tools import runner as runner_tools  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


EXTRACTED_MCP = {
    "list_runner_sessions": runner_tools,
    "register_runner_session": runner_tools,
    "claim_runner_control": runner_tools,
    "complete_runner_control": runner_tools,
    "claim_external_effect": external_effects_tools,
    "list_external_effects": external_effects_tools,
    "merge_gate": external_effects_tools,
    "request_external_ci_mirror_run": external_effects_tools,
    "poll_external_ci_mirror_run": external_effects_tools,
    "record_publication_evidence": external_effects_tools,
    "verify_ci": external_effects_tools,
}

try:
    server_source = entrypoint_source("mcp_server")
    ok("runner_tools.register_runner_tools(" in server_source
       and "external_effects_tools.register_external_effects_tools(" in server_source,
       "mcp_server registers runner + external_effects tool modules")

    impl_source = (ROOT / "mcp_server_impl.py").read_text(encoding="utf-8")
    for name in EXTRACTED_MCP:
        ok(f"def {name}(" not in impl_source,
           f"{name} implementation left the MCP residual")

    app_source = (ROOT / "app_impl.py").read_text(encoding="utf-8")
    ok("_create_runner_router(" in app_source
       and "_create_external_effects_router(" in app_source,
       "app_impl mounts runner + external_effects routers")
    for needle in (
        "async def ixp_runner_sessions",
        "async def ixp_claim_external_effect",
        "async def ixp_merge_gate",
        "async def ixp_complete_runner_control",
        "async def ixp_request_external_ci_mirror",
    ):
        ok(needle not in app_source, f"{needle} left the REST residual")

    router_runner = (ROOT / "src/switchboard/api/routers/runner.py").read_text(encoding="utf-8")
    router_effects = (
        ROOT / "src/switchboard/api/routers/external_effects.py").read_text(encoding="utf-8")
    ok("runner_control_command" in router_runner, "REST runner routes use shared command")
    ok("effect_command" in router_effects and "merge_gate_command" in router_effects
       and "verify_ci_command" in router_effects,
       "REST effects/merge_gate/verify_ci routes use shared commands")
    ok("repositories" not in router_effects.split("claim_external_effect")[0]
       or "effect_command.claim_mapping_result" in router_effects,
       "effects claim path goes through command (not duplicated store branching)")

    cmd_source = (
        ROOT / "src/switchboard/application/commands/claim_external_effect.py"
    ).read_text(encoding="utf-8")
    ok("external_effects as effects_repo" in cmd_source
       or "repositories.external_effects" in cmd_source
       or "repositories import external_effects" in cmd_source,
       "claim_external_effect command uses repositories/external_effects")

    import mcp_server  # noqa: E402
    import store  # noqa: E402

    store.init_db("switchboard")
    registered = set(mcp_server.mcp._tool_manager._tools)
    ok(set(EXTRACTED_MCP).issubset(registered),
       "FastMCP exposes every extracted runner/effects tool")
    ok(all(getattr(mcp_server, name) is getattr(mod, name)
           for name, mod in EXTRACTED_MCP.items()),
       "mcp_server retains direct-call compatibility aliases")

    listed = json.loads(mcp_server.list_runner_sessions(project="switchboard"))
    ok(isinstance(listed, list), "list_runner_sessions returns a JSON list")

    effects = json.loads(mcp_server.list_external_effects(project="switchboard"))
    ok(isinstance(effects, list), "list_external_effects returns a JSON list")

    # Shared command path: claim effect via repository-backed command.
    claimed = effect_command.claim_mapping_result(
        {
            "effect_type": "test.effect",
            "target": "arch-ms-67",
            "resource": "unit",
            "payload": {"n": 1},
            "project": "switchboard",
            "agent_id": "arch-ms-67-test",
            "idem_key": "arch-ms-67-effect-1",
        },
        actor="arch-ms-67-test",
        principal_id="arch-ms-67-test",
    )
    ok(bool(claimed.get("effect_key")) and not claimed.get("error"),
       "claim_external_effect command persists via repository")

    replay = effect_command.claim_mapping_result(
        {
            "effect_type": "test.effect",
            "target": "arch-ms-67",
            "resource": "unit",
            "payload": {"n": 1},
            "project": "switchboard",
            "agent_id": "arch-ms-67-test",
            "idem_key": "arch-ms-67-effect-1",
        },
        actor="arch-ms-67-test",
        principal_id="arch-ms-67-test",
    )
    ok(replay.get("effect_key") == claimed.get("effect_key"),
       "effect claim replays the same effect_key")

    # Runner control shared helpers are importable and list-clean.
    ok(runner_command.list_sessions(project="switchboard") == [],
       "runner_control.list_sessions empty on fresh db")
    ok(callable(merge_gate_command.execute_mapping_result),
       "merge_gate command execute_mapping_result is shared")

    app_lines = sum(1 for _ in (ROOT / "app_impl.py").open(encoding="utf-8"))
    mcp_lines = sum(1 for _ in (ROOT / "mcp_server_impl.py").open(encoding="utf-8"))
    ok(app_lines < 2613, f"app_impl residual shrank this PR ({app_lines} < 2613)")
    ok(mcp_lines < 2428, f"mcp_server_impl residual shrank this PR ({mcp_lines} < 2428)")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
