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


ALL = ["test_narration_ops.py", "test_narration_outbox.py", "test_web_write_auth.py",
       "test_deliverables_model.py"]
IMPORTS = {
    "test_narration_ops.py": {"narration_ops", "narration_outbox", "store"},
    "test_narration_outbox.py": {"narration_outbox", "store"},
    "test_web_write_auth.py": {"app", "auth"},
    "test_deliverables_model.py": {"store"},
}

try:
    # 1. a broad/shared change runs the FULL suite (None sentinel) — fail-safe.
    for broad in ["store.py", "app.py", "mcp_server.py", "db/core.py", "constants.py",
                  "requirements.txt", "conftest.py", "scripts/switchboard_ci.sh", "tests/path_setup.py"]:
        if sel.impacted_tests([broad], ALL, test_imports=IMPORTS) is not None:
            ok(False, f"broad change {broad} did NOT force full suite"); break
    else:
        ok(True, "any broad/shared change forces the full suite (None)")

    # 2. a leaf-module change narrows to the tests that import it + its test_<module>.
    imp = sel.impacted_tests(["narration_ops.py"], ALL, test_imports=IMPORTS)
    ok(imp == ["test_narration_ops.py"],
       "a leaf module change runs only the tests that import it")

    # 3. a changed test file runs itself.
    imp = sel.impacted_tests(["test_deliverables_model.py"], ALL, test_imports=IMPORTS)
    ok("test_deliverables_model.py" in imp, "a changed test file runs itself")

    # 4. fail-safe: a changed module with NO covering test → full suite.
    imp = sel.impacted_tests(["some_uncovered_helper.py"], ALL, test_imports=IMPORTS)
    ok(imp is None, "a changed module with no covering test forces the full suite")

    # 5. test_<module>.py convention picks up the sibling test even without an import edge.
    imp = sel.impacted_tests(["narration_outbox.py"], ALL, test_imports=IMPORTS)
    ok("test_narration_outbox.py" in imp,
       "test_<module>.py is selected for a changed module")

    # 6. package-file change maps to importers of the package and dotted module.
    imports2 = dict(IMPORTS, **{"test_db_thing.py": {"db.core", "db"}})
    all2 = ALL + ["test_db_thing.py", "test_core.py"]
    # db/ is broad, so use a non-broad package to prove the alias mapping:
    imports3 = {"test_widget.py": {"widgets.parser", "widgets"}}
    imp = sel.impacted_tests(["widgets/parser.py"], ["test_widget.py"], test_imports=imports3)
    ok(imp == ["test_widget.py"], "a package-file change maps to tests importing the dotted module")

    # 7. sharding is deterministic, balanced, and partitions exactly (no loss, no overlap).
    items = [f"t{n}.py" for n in range(23)]
    N = 4
    shards = [sel.shard(items, N, i) for i in range(N)]
    union = sorted(x for s in shards for x in s)
    sizes = sorted(len(s) for s in shards)
    ok(union == sorted(items)
       and all(len(set(shards[i]) & set(shards[j])) == 0 for i in range(N) for j in range(i + 1, N))
       and sizes[-1] - sizes[0] <= 1,
       "shard partitions all items exactly once, balanced within 1, deterministic")

    ok(sel.shard(items, 1, 0) == sorted(items), "shards=1 returns the whole sorted set")

except Exception as exc:  # pragma: no cover
    import traceback
    traceback.print_exc()
    ok(False, f"unexpected exception: {exc}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
