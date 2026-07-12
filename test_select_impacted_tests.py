#!/usr/bin/env python3
"""HARDEN-71: unit tests for test-impact selection + sharding (no git, no fs)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import select_impacted_tests as sel  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# Import graph over ALL .py files (modules AND tests). agent -> signals (transitive edge).
MI = {
    "narration_ops.py": {"narration_outbox", "store"},
    "narration_outbox.py": {"store"},
    "agent.py": {"signals"},                     # non-test module that re-exports a leaf
    "signals.py": set(),
    "orphan.py": set(),                          # changed-but-untested module
    "widgets/parser.py": set(),
    "test_narration_ops.py": {"narration_ops", "narration_outbox", "store"},
    "test_narration_outbox.py": {"narration_outbox", "store"},
    "test_signals.py": {"signals"},
    "test_session_health.py": {"agent"},         # transitively reaches signals via agent
    "test_deliverables_model.py": {"store"},
    "test_comma.py": {"os", "signals"},          # comma-import edge (import os, signals)
    "test_widget.py": {"widgets.parser", "widgets"},
}
ALL = [f for f in MI if os.path.basename(f).startswith("test_")]

try:
    # 1. broad/shared changes run the FULL suite (None), fail-safe.
    for broad in ["store.py", "app.py", "mcp_server.py", "db/core.py", "requirements.txt",
                  "conftest.py", "scripts/switchboard_ci.sh", "tests/path_setup.py"]:
        if sel.impacted_tests([broad], ALL, module_imports=MI) is not None:
            ok(False, f"broad change {broad} did NOT force full suite"); break
    else:
        ok(True, "any broad/shared change forces the full suite (None)")

    # 2. a leaf change narrows to the tests that (transitively) import it.
    imp = sel.impacted_tests(["narration_ops.py"], ALL, module_imports=MI)
    ok(imp == ["test_narration_ops.py"], "a leaf change runs only the tests that import it")

    # 3. BUG-2 FIX — transitive imports are caught: changing a leaf that a NON-test module
    #    re-exports pulls in tests that reach it only through that module.
    imp = sel.impacted_tests(["signals.py"], ALL, module_imports=MI)
    ok("test_session_health.py" in imp and "test_signals.py" in imp and "test_comma.py" in imp,
       "a transitively-dependent test (test -> agent -> signals) is selected")

    # 4. BUG-1 FIX — a non-Python change (data/fixture/SQL/template) forces the full suite.
    ok(sel.impacted_tests(["seed_plan.json"], ALL, module_imports=MI) is None
       and sel.impacted_tests(["static/index.html"], ALL, module_imports=MI) is None
       and sel.impacted_tests(["db/migrations/003.sql"], ALL, module_imports=MI) is None,
       "any non-Python (non-doc) change forces the full suite — no silent empty selection")

    # 5. docs-only changes can't affect tests → select nothing (not the full suite).
    ok(sel.impacted_tests(["docs/x.md", "README.md"], ALL, module_imports=MI) == [],
       "a docs-only change selects no tests (docs can't change a test outcome)")

    # 6. fail-safe: a changed module with NO covering test → full suite.
    ok(sel.impacted_tests(["orphan.py"], ALL, module_imports=MI) is None,
       "a changed module reached by no test forces the full suite")

    # 7. a changed test file runs itself.
    ok("test_deliverables_model.py" in sel.impacted_tests(
        ["test_deliverables_model.py"], ALL, module_imports=MI),
       "a changed test file runs itself")

    # 8. BUG-3 FIX — comma multi-import + relative imports are parsed.
    ok("signals" in sel._parse_imports("import os, signals\n")
       and "sib" in sel._parse_imports("from . import sib\n")
       and "a.b" in sel._parse_imports("from a.b import c\n")
       and sel._parse_imports("import saturation_signals  # noqa: E402\n") == {"saturation_signals"}
       and sel._parse_imports("from store import x  # noqa\n") == {"store"},
       "import parser handles comma/relative imports AND strips inline # comments")

    # 9. sharding partitions exactly, balanced within 1, deterministic.
    items = [f"t{n}.py" for n in range(23)]
    N = 4
    shards = [sel.shard(items, N, i) for i in range(N)]
    union = sorted(x for s in shards for x in s)
    sizes = sorted(len(s) for s in shards)
    ok(union == sorted(items)
       and all(not (set(shards[i]) & set(shards[j])) for i in range(N) for j in range(i + 1, N))
       and sizes[-1] - sizes[0] <= 1,
       "shard partitions all items exactly once, balanced within 1, deterministic")
    ok(sel.shard(items, 1, 0) == sorted(items), "shards=1 returns the whole sorted set")

except Exception as exc:  # pragma: no cover
    import traceback
    traceback.print_exc()
    ok(False, f"unexpected exception: {exc}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
