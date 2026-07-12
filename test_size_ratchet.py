#!/usr/bin/env python3
"""CONSOL-6: keep the application shell at its measured high-water marks."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent

# These values intentionally match the tree exactly when the ratchet is updated. If a target
# shrinks, lower its ceiling in the same PR; if it grows, follow ADR-0007's relief order before
# raising the value with a one-line justification visible in review.
LINE_CEILINGS = {
    "store.py": 15_566,  # DELIVERABLES-16 closure surface (+95) atop create_project cache fix
    "app.py": 3_091,  # ARCH-MS-16 task router extraction atop DELIVERABLES-17 (+16)
    "mcp_server.py": 2_982,  # ARCH-MS-19 extracted board reads after the task-tool seam
    "static/app.js": 4_888,  # ARCH-MS-21 composition root after board/mission/state extraction
    "static/js/api.js": 25,  # project-aware fetch boundary
    "static/js/state.js": 79,  # application state and UI vocabularies
    "static/js/board.js": 227,  # board filters/cards/summary rendering
    "static/js/mission.js": 1_379,  # deliverable mission cockpit and authoring
}
ROOT_PYTHON_FILE_CEILING = 205  # ARCH-MS-4 then ARCH-MS-21 add one focused regression proof each

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
