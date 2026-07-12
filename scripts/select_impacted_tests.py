#!/usr/bin/env python3
"""HARDEN-71 (CI-4) — test-impact selection + sharding for the CI gate (ADR-0010 Lever 3).

The gate's wall-clock is the amplifier of the merge race: the longer it runs, the more `master`
moves under an open PR. Two levers cut it:

- **Test-impact selection** — run only the tests a PR's diff can actually affect, instead of the
  whole ~140-file suite. Selection is deliberately **fail-safe**: any change to a broad/shared
  surface (`store.py`, `app.py`, `mcp_server.py`, `db/`, `constants.py`, deps, conftest, or CI
  itself) runs the **full** suite, and any changed module not covered by an identifiable test
  also runs the full suite. A gate that skips a real failure is worse than a slow gate — so we
  only ever narrow when we are confident the impacted set is complete.
- **Sharding** — split the selected tests into N deterministic, balanced shards so a matrix of
  ephemeral off-box runners executes them in parallel (see `.github/workflows/ci-sharded.yml`).

`impacted_tests` and `shard` are pure and unit-tested. `main()` wires them to git + the tree:
  python scripts/select_impacted_tests.py --base origin/master [--shards N --index I] [--json]
Prints the test files to run (one per line), or ALL sentinel semantics via `--json`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Set

# A change to any of these forces the FULL suite — they can affect anything.
BROAD_PREFIXES = (
    "store.py", "app.py", "mcp_server.py", "constants.py", "conftest.py",
    "db/", "src/", "scripts/switchboard_ci.sh", "scripts/select_impacted_tests.py",
    "requirements", "pyproject.toml", "uv.lock", ".python-version",
    "tests/",  # shared test scaffolding (path_setup etc.) affects every test
)

_IMPORT_RE = re.compile(r"^\s*(?:from\s+([.\w]+)\s+import|import\s+([.\w]+))", re.MULTILINE)


def _is_broad(path: str, broad_prefixes) -> bool:
    p = path.replace("\\", "/")
    return any(p == b or p.startswith(b) for b in broad_prefixes)


def _module_aliases(path: str) -> Set[str]:
    """Module names a `.py` file is importable as: 'foo.py'->{'foo'}; 'db/core.py'->
    {'db.core','db'} so a test that does `import db` or `from db.core import x` matches."""
    p = path.replace("\\", "/")
    if not p.endswith(".py"):
        return set()
    stem = p[:-3]
    parts = stem.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    aliases = {".".join(parts)} if parts else set()
    if parts:
        aliases.add(parts[0])          # top-level package (db/core.py -> db)
        aliases.add(parts[-1])         # bare module name (db/core.py -> core)
    return {a for a in aliases if a}


def impacted_tests(changed_files: List[str], all_tests: List[str], *,
                   test_imports: Dict[str, Set[str]],
                   broad_prefixes=BROAD_PREFIXES) -> Optional[List[str]]:
    """Tests a PR's diff can affect, or **None** meaning "run the full suite" (fail-safe).

    ``test_imports`` maps each test file to the set of module names it imports. A test runs if it
    is itself changed, if it imports a changed module, or if it is the ``test_<module>.py`` for a
    changed module. If any changed non-test ``.py`` module has no identifiable covering test, we
    return None rather than risk skipping a regression.
    """
    if any(_is_broad(f, broad_prefixes) for f in changed_files):
        return None
    all_set = set(all_tests)
    base_names = {os.path.basename(t): t for t in all_tests}
    impacted: Set[str] = set()
    changed_modules: Set[str] = set()
    for f in changed_files:
        if f in all_set:                       # a changed test runs itself
            impacted.add(f)
            continue
        if f.endswith(".py"):
            changed_modules.add(f)             # track for coverage check + module mapping

    module_names: Set[str] = set()
    for f in changed_modules:
        module_names |= _module_aliases(f)

    for test_file, mods in test_imports.items():
        if mods & module_names and test_file in all_set:
            impacted.add(test_file)
    for f in changed_modules:
        cand = "test_" + os.path.basename(f)   # foo.py -> test_foo.py
        if cand in base_names:
            impacted.add(base_names[cand])

    # Fail-safe: a changed module with no covering test = a coverage blind spot -> run everything.
    for f in changed_modules:
        names = _module_aliases(f)
        covered = any(names & mods for mods in test_imports.values())
        covered = covered or ("test_" + os.path.basename(f)) in base_names
        if not covered:
            return None
    return sorted(impacted)


def shard(items: List[str], num_shards: int, index: int) -> List[str]:
    """Deterministic, balanced round-robin shard: shard i of N gets items[i], items[i+N], ..."""
    if num_shards <= 1:
        return sorted(items)
    ordered = sorted(items)
    return [x for i, x in enumerate(ordered) if i % num_shards == index]


# --- filesystem / git wiring (not unit-tested; the pure functions above are) ---

def discover_tests(root: str) -> List[str]:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        if "/.git" in dirpath or "/.venv" in dirpath:
            continue
        for fn in files:
            if (fn.startswith("test_") or fn.endswith("_test.py")) and fn.endswith(".py"):
                out.append(os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/"))
    return sorted(out)


def build_test_imports(root: str, tests: List[str]) -> Dict[str, Set[str]]:
    imports: Dict[str, Set[str]] = {}
    for t in tests:
        try:
            text = open(os.path.join(root, t), encoding="utf-8", errors="ignore").read()
        except OSError:
            imports[t] = set()
            continue
        mods: Set[str] = set()
        for m in _IMPORT_RE.finditer(text):
            name = (m.group(1) or m.group(2) or "").lstrip(".")
            if name:
                mods.add(name)
                mods.add(name.split(".")[0])
        imports[t] = mods
    return imports


def changed_files(base: str, root: str) -> List[str]:
    res = subprocess.run(["git", "-C", root, "diff", "--name-only", f"{base}...HEAD"],
                         capture_output=True, text=True, check=True)
    return [f for f in res.stdout.splitlines() if f.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="origin/master")
    ap.add_argument("--root", default=".")
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)

    tests = discover_tests(root)
    try:
        changed = changed_files(args.base, root)
    except subprocess.CalledProcessError:
        changed = None  # can't diff -> run everything
    selected = impacted_tests(changed, tests, test_imports=build_test_imports(root, tests)) \
        if changed is not None else None

    run_all = selected is None
    to_run = tests if run_all else selected
    to_run = shard(to_run, args.shards, args.index)
    if args.json:
        print(json.dumps({"run_all": run_all, "count": len(to_run), "tests": to_run}))
    else:
        for t in to_run:
            print(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
