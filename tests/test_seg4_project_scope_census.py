"""SEG-4 exit proof: endpoint census + ProjectContext unit checks."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from path_setup import ROOT

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if condition:
        passed += 1
    else:
        failed += 1


def load_census():
    path = ROOT / "scripts" / "seg4_endpoint_census.py"
    spec = importlib.util.spec_from_file_location("seg4_endpoint_census", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    census = load_census()
    report = census.census()
    ok(report["ok"], f"endpoint census clean ({report['scanned_files']} files)")
    if not report["ok"]:
        for row in report["violations"][:20]:
            print(f"         {row['file']}:{row['line']} {row['label']}: {row['text']}")

    from switchboard.application.queries.project_scope import (
        ConflictingProjectScope,
        MissingProjectScope,
        reconcile_explicit_projects,
        require_explicit_project,
    )
    from switchboard.application.adapters import legacy_maxwell_default
    from switchboard.domain.projects.context import ProjectContext

    try:
        require_explicit_project("", source="query")
        ok(False, "empty project raises MissingProjectScope")
    except MissingProjectScope:
        ok(True, "empty project raises MissingProjectScope")

    try:
        reconcile_explicit_projects(("a", "query"), ("b", "body"))
        ok(False, "conflicting scope raises")
    except ConflictingProjectScope:
        ok(True, "conflicting scope raises ConflictingProjectScope")

    ctx = legacy_maxwell_default.project_context()
    ok(isinstance(ctx, ProjectContext), "legacy Maxwell adapter yields ProjectContext")
    ok(ctx.source.startswith("adapter:"), "Maxwell adapter source is named")
    ok(ctx.project_id == legacy_maxwell_default.maxwell_project_id(),
       "adapter project_id is Maxwell")

    app_loc = sum(1 for _ in (ROOT / "app.py").open())
    mcp_loc = sum(1 for _ in (ROOT / "mcp_server.py").open())
    ok(app_loc <= 25 and mcp_loc <= 25,
       f"app.py+mcp_server.py stay thin shells ({app_loc}+{mcp_loc} LOC)")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
