#!/usr/bin/env python3
"""Fail-closed ARCH-MS Phase 0 exit audit (ADR-0009 Decision 5).

Unlike the retired moving size ratchet, this gate compares the current tree to
one immutable Phase 0 baseline. It therefore never asks concurrent PRs to edit a
shared ceiling while still proving real extraction and no net monolith growth.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List


ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA = os.environ.get("ARCH_MS_PHASE0_BASELINE", "5305090")
MONOLITHS = ("store.py", "app.py", "mcp_server.py")
STORE_REDUCTION_TARGET = 500
STORE_FACADE_MAX = 14_000

MOVED_ACCESS_FUNCTIONS = (
    "normalize_project_id", "project_ids", "has_project", "is_global_project_binding",
    "principal_registry_project", "projects", "role_scopes",
    "principal_scope_definitions", "validate_principal_kind",
    "validate_principal_scopes", "resolve_principal_scopes", "ensure_org",
    "ensure_user", "add_org_member", "set_project_access", "project_access",
    "grant_project_role", "revoke_project_role", "list_project_role_grants",
    "principal_project_roles", "effective_principal_scopes", "project_access_model",
    "ensure_bootstrap_project_owner",
)

# Phase 0 proved these functions were moved verbatim out of ``store.py``.  Once
# the extraction landed, the repository became their source of truth and may
# evolve deliberately.  Keep such exceptions explicit so the audit continues to
# catch accidental changes to every other moved function without forcing fixes
# back into the monolith or pretending the extracted repository is immutable.
EVOLVED_ACCESS_FUNCTIONS = ("projects", "set_project_access")

REQUIRED_ARTIFACTS = (
    "pyproject.toml", ".python-version", "uv.lock",
    "src/switchboard/api/routers/tasks.py",
    "src/switchboard/application/commands/create_task.py",
    "src/switchboard/application/commands/update_task.py",
    "src/switchboard/application/queries/get_task.py",
    "src/switchboard/mcp/tools/tasks.py",
    "src/switchboard/mcp/tools/board.py",
    "src/switchboard/storage/repositories/access.py",
    "src/switchboard/storage/repositories/claims.py",
    "src/switchboard/storage/repositories/runner.py",
    "src/switchboard/storage/repositories/tasks.py",
    "auth_store.py",
    "claims_store.py",
    "runner_store.py",
    "tasks_store.py",
    "tests/path_setup.py",
    "tests/test_arch_ms0_scaffold.py",
    "tests/test_arch_ms14_test_layout.py",
    "tests/test_arch_ms16_task_router.py",
    "tests/test_arch_ms17_mcp_task_tools.py",
    "tests/test_arch_ms19_mcp_board_tools.py",
    "tests/test_arch_ms29_runner_repository.py",
    "tests/test_arch_ms30_access_repository.py",
    "tests/test_arch_ms31_tasks_repository.py",
    "tests/test_arch_ms32_claims_repository.py",
    "tests/test_pm_env_flag_census.py",
    "test_mcp_read_auth.py",
    "test_plan_health.py",
    "test_schema_migrations.py",
)


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), *args], text=True, stderr=subprocess.STDOUT)


def _baseline_source(path: str) -> str:
    return _git("show", f"{BASELINE_SHA}:{path}")


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _functions(source: str) -> Dict[str, str]:
    tree = ast.parse(source)
    return {
        node.name: ast.dump(node, include_attributes=False)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def build_report() -> Dict[str, object]:
    baseline_counts = {
        path: _line_count(_baseline_source(path)) for path in MONOLITHS
    }
    current_counts = {
        path: _line_count((ROOT / path).read_text(encoding="utf-8"))
        for path in MONOLITHS
    }
    deltas = {
        path: current_counts[path] - baseline_counts[path] for path in MONOLITHS
    }
    store_reduction = baseline_counts["store.py"] - current_counts["store.py"]

    current_access_source = (
        ROOT / "src/switchboard/storage/repositories/access.py"
    ).read_text(encoding="utf-8")
    baseline_functions = _functions(_baseline_source("store.py"))
    current_store_functions = _functions(
        (ROOT / "store.py").read_text(encoding="utf-8")
    )
    access_functions = _functions(current_access_source)
    moved_matches = {
        name: (
            name in baseline_functions
            and name in access_functions
            and name not in current_store_functions
            and (
                name in EVOLVED_ACCESS_FUNCTIONS
                or baseline_functions[name] == access_functions[name]
            )
        )
        for name in MOVED_ACCESS_FUNCTIONS
    }

    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    rest_source = (
        ROOT / "src/switchboard/api/routers/tasks.py"
    ).read_text(encoding="utf-8")
    mcp_source = (
        ROOT / "src/switchboard/mcp/tools/tasks.py"
    ).read_text(encoding="utf-8")
    shared_handlers = (
        "create_task_command.execute_mapping_result",
        "update_task_command.execute_mapping_result",
        "get_task_query.execute_for",
    )
    application_seam = {
        "rest_router_mounted": "_create_task_router" in app_source,
        "rest_handlers_shared": all(token in rest_source for token in shared_handlers),
        "mcp_handlers_shared": all(token in mcp_source for token in shared_handlers),
    }

    ci_source = (ROOT / "scripts/switchboard_ci.sh").read_text(encoding="utf-8")
    ci_discovery = {
        "test_prefix": "-name 'test_*.py'" in ci_source,
        "test_suffix": "-name '*_test.py'" in ci_source,
        "denylist_audited": "TEST_DENYLIST=(" in ci_source,
    }
    missing_artifacts = [path for path in REQUIRED_ARTIFACTS if not (ROOT / path).is_file()]

    checks = {
        "extraction_threshold": (
            store_reduction >= STORE_REDUCTION_TARGET
            or current_counts["store.py"] <= STORE_FACADE_MAX
        ),
        "no_net_monolith_growth": all(delta <= 0 for delta in deltas.values()),
        "verbatim_access_move": all(moved_matches.values()),
        "application_layer_proven": all(application_seam.values()),
        "ci_discovery_active": all(ci_discovery.values()),
        "required_artifacts_present": not missing_artifacts,
    }
    return {
        "schema": "switchboard.arch_ms_phase0_exit.v1",
        "baseline_sha": BASELINE_SHA,
        "baseline_lines": baseline_counts,
        "current_lines": current_counts,
        "line_deltas": deltas,
        "store_reduction_lines": store_reduction,
        "store_reduction_target": STORE_REDUCTION_TARGET,
        "store_facade_max": STORE_FACADE_MAX,
        "moved_access_functions": moved_matches,
        "evolved_access_functions": list(EVOLVED_ACCESS_FUNCTIONS),
        "application_seam": application_seam,
        "ci_discovery": ci_discovery,
        "missing_artifacts": missing_artifacts,
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> int:
    try:
        report = build_report()
    except (OSError, subprocess.CalledProcessError, SyntaxError) as exc:
        report = {
            "schema": "switchboard.arch_ms_phase0_exit.v1",
            "baseline_sha": BASELINE_SHA,
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
