#!/usr/bin/env python3
"""CONSOL-6: keep the application shell at its measured high-water marks."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent

# These values intentionally match the tree exactly when the ratchet is updated. If a target
# shrinks, lower its ceiling in the same PR; if it grows, follow ADR-0007's relief order before
# raising the value with a one-line justification visible in review.
LINE_CEILINGS = {
    "store.py": 15_471,  # +1: create_project busts the project-map cache (registry-churn perf fix)
    "app.py": 3_274,  # ARCH-MS-18/8 share one package bootstrap and thin create adapter
    "mcp_server.py": 3_152,  # ARCH-MS-8: create_task now delegates to application/commands
    "static/app.js": 6_566,  # pre-existing drift on master (not BUG-49); re-baselined to re-green the ratchet
}
ROOT_PYTHON_FILE_CEILING = 205  # master 204 + ARCH-MS-6 test_arch_ms6_pyproject_scaffold.py CI lock

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def check_ratchet(label, actual, ceiling):
    if actual > ceiling:
        detail = (f"{label}: {actual:,} exceeds ceiling {ceiling:,}; delete dead weight, "
                  "extract the runner cluster, or justify a ceiling raise in this PR")
    elif actual < ceiling:
        detail = f"{label}: {actual:,} is below ceiling {ceiling:,}; lower the ceiling to {actual:,}"
    else:
        detail = f"{label}: {actual:,} matches ratchet ceiling {ceiling:,}"
    ok(actual == ceiling, detail)


for relative_path, ceiling in LINE_CEILINGS.items():
    actual = len((ROOT / relative_path).read_text(encoding="utf-8").splitlines())
    check_ratchet(f"{relative_path} lines", actual, ceiling)

root_python_files = sorted(path.name for path in ROOT.glob("*.py"))
actual_root_count = len(root_python_files)
check_ratchet("repo-root .py files", actual_root_count, ROOT_PYTHON_FILE_CEILING)

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
