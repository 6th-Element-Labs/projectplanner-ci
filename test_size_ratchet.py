#!/usr/bin/env python3
"""CONSOL-6: keep the application shell at its measured high-water marks."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent

# These values intentionally match the tree exactly when the ratchet is updated. If a target
# shrinks, lower its ceiling in the same PR; if it grows, follow ADR-0007's relief order before
# raising the value with a one-line justification visible in review.
LINE_CEILINGS = {
    "store.py": 15_470,  # ARCH-MS-20 extracted runner persistence/control into runner_store.py
    "app.py": 3_278,  # ARCH-MS-15: get_task/update_task delegate to application layer (+imports, fail-loud 400)
    "mcp_server.py": 3_148,  # ARCH-MS-15: update_task dep validation moved into application/commands
    "static/app.js": 6_566,  # pre-existing drift on master (not BUG-49); re-baselined to re-green the ratchet
}
ROOT_PYTHON_FILE_CEILING = 204  # NARRATE-12 (+2) then ARCH-MS-4 adds test_consol8_edge_mission_poll.py (CONSOL-8 CI lock)

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
