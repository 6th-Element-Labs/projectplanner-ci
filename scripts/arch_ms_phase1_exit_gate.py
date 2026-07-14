#!/usr/bin/env python3
"""Fail-closed ARCH-MS Phase 1 exit audit (mission arch-ms-phase-1).

Absolute ceilings from ARCH-MS-45 / mission end_state, plus ARCH-MS-53 residual
policy that forbids rename-as-done:

  1. Entry façades: store.py deleted or <200 lines; app.py / mcp_server.py <500
  2. Residual bodies (shell.py / app_impl.py / mcp_server_impl.py), when present,
     must ALSO sit under shrinking ceilings — or be deleted
  3. PR #440-style façade dump alone must fail (thin entry + fat residual)

Residuals are optional. Requiring them would endorse rename-only extraction.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]

# Entry façades / adapters (mission end_state).
STORE_FACADE_MAX = 200
ADAPTER_MAX = 500

# Residual shrinking ceilings (ARCH-MS-53). Same order as entry ceilings so a
# verbatim dump into *_impl / shell cannot pass Phase 1 exit.
STORE_RESIDUAL_MAX = 200
APP_RESIDUAL_MAX = 500
MCP_RESIDUAL_MAX = 500

MONOLITHS = ("store.py", "app.py", "mcp_server.py")

# Candidate residual bodies. Absent is fine; present must be under ceiling.
STORE_RESIDUAL_CANDIDATES = (
    "src/switchboard/storage/repositories/shell.py",
    "shell.py",
)
APP_RESIDUAL_CANDIDATES = ("app_impl.py",)
MCP_RESIDUAL_CANDIDATES = ("mcp_server_impl.py",)

REQUIRED_ARTIFACTS = (
    "src/switchboard/contracts/__init__.py",
    "src/switchboard/application/commands/create_task.py",
    "src/switchboard/application/commands/move_task.py",
    "src/switchboard/api/routers/tasks.py",
    "src/switchboard/mcp/tools/tasks.py",
    "src/switchboard/domain/ixp/protocol.py",
    "scripts/arch_ms_phase0_exit_gate.py",
    "tests/test_arch_ms24_phase0_exit_gate.py",
    "tests/test_rest_idempotency.py",
)


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _function_names(path: Path) -> List[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _has_sql_literals(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    markers = ("SELECT ", "INSERT ", "UPDATE ", "DELETE FROM ", "CREATE TABLE")
    return any(marker in source for marker in markers)


def _resolve_residual(
    root: Path, candidates: Tuple[str, ...], ceiling: int
) -> Dict[str, object]:
    """Return residual accounting for one family of dump/shim paths.

    Missing residuals are treated as drained (pass). When multiple candidates
    exist, every present file must independently sit under the ceiling.
    """
    present: List[Dict[str, object]] = []
    for rel in candidates:
        path = root / rel
        if not path.is_file():
            continue
        lines = _line_count(path)
        present.append(
            {
                "path": rel,
                "lines": lines,
                "ceiling": ceiling,
                "under_ceiling": lines < ceiling,
            }
        )
    return {
        "candidates": list(candidates),
        "ceiling": ceiling,
        "present": present,
        "deleted": not present,
        "under_ceiling": all(item["under_ceiling"] for item in present),
    }


def build_report(root: Optional[Path] = None) -> Dict[str, object]:
    root = root or ROOT
    current_lines = {
        path: _line_count(root / path) if (root / path).is_file() else 0
        for path in MONOLITHS
    }
    store_missing = not (root / "store.py").is_file()
    store_ok = store_missing or current_lines["store.py"] < STORE_FACADE_MAX
    app_ok = (root / "app.py").is_file() and current_lines["app.py"] < ADAPTER_MAX
    mcp_ok = (
        (root / "mcp_server.py").is_file()
        and current_lines["mcp_server.py"] < ADAPTER_MAX
    )

    store_fns = _function_names(root / "store.py") if not store_missing else []
    allowed_store_fns = {"__getattr__", "__dir__"}
    store_logic_free = store_missing or set(store_fns) <= allowed_store_fns

    missing_artifacts = [
        path for path in REQUIRED_ARTIFACTS if not (root / path).is_file()
    ]

    store_residual = _resolve_residual(
        root, STORE_RESIDUAL_CANDIDATES, STORE_RESIDUAL_MAX
    )
    app_residual = _resolve_residual(
        root, APP_RESIDUAL_CANDIDATES, APP_RESIDUAL_MAX
    )
    mcp_residual = _resolve_residual(
        root, MCP_RESIDUAL_CANDIDATES, MCP_RESIDUAL_MAX
    )
    residuals = {
        "store": store_residual,
        "app": app_residual,
        "mcp": mcp_residual,
    }

    facade_sql_free = store_missing or not _has_sql_literals(root / "store.py")
    adapter_sql_free = (
        (root / "app.py").is_file()
        and not _has_sql_literals(root / "app.py")
        and (root / "mcp_server.py").is_file()
        and not _has_sql_literals(root / "mcp_server.py")
    )

    # Rename-as-done: thin entry façades with at least one fat residual body.
    rename_as_done = (
        store_ok
        and app_ok
        and mcp_ok
        and not (
            store_residual["under_ceiling"]
            and app_residual["under_ceiling"]
            and mcp_residual["under_ceiling"]
        )
    )

    checks = {
        "store_facade_ceiling": store_ok,
        "app_adapter_ceiling": app_ok,
        "mcp_adapter_ceiling": mcp_ok,
        "store_logic_free": store_logic_free,
        "facade_sql_free": facade_sql_free,
        "adapter_sql_free": adapter_sql_free,
        "store_residual_ceiling": bool(store_residual["under_ceiling"]),
        "app_residual_ceiling": bool(app_residual["under_ceiling"]),
        "mcp_residual_ceiling": bool(mcp_residual["under_ceiling"]),
        "rename_as_done_forbidden": not rename_as_done,
        "required_artifacts_present": not missing_artifacts,
    }
    return {
        "schema": "switchboard.arch_ms_phase1_exit.v1",
        "current_lines": current_lines,
        "ceilings": {
            "store.py": STORE_FACADE_MAX,
            "app.py": ADAPTER_MAX,
            "mcp_server.py": ADAPTER_MAX,
            "store_residual": STORE_RESIDUAL_MAX,
            "app_residual": APP_RESIDUAL_MAX,
            "mcp_residual": MCP_RESIDUAL_MAX,
        },
        "store_deleted": store_missing,
        "store_functions": store_fns,
        "residuals": residuals,
        "rename_as_done": rename_as_done,
        "missing_artifacts": missing_artifacts,
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> int:
    try:
        report = build_report()
    except (OSError, SyntaxError) as exc:
        report = {
            "schema": "switchboard.arch_ms_phase1_exit.v1",
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
