#!/usr/bin/env python3
"""HARDEN-71 (CI-4) — test-impact selection + sharding for the CI gate (ADR-0010 Lever 3).

The gate's wall-clock is the amplifier of the merge race: the longer it runs, the more `master`
moves under an open PR. Two levers cut it:

- **Test-impact selection** — run only the tests a PR's diff can affect, instead of the whole
  ~140-file suite. Selection is **fail-safe**: it runs the FULL suite whenever it can't *prove*
  the narrowed set is complete. Specifically it returns "run all" (None) when a change touches a
  broad/shared surface, when ANY non-Python file changes (data/fixtures/SQL/templates a test may
  depend on), or when a changed module isn't reached by any test in the import graph. A test is
  selected iff it **transitively** imports a changed module — so a leaf change that a non-test
  module re-exports still pulls in every test that reaches it. A gate that skips a
  regression-catching test is worse than a slow gate; we only narrow when it's provably safe.
- **Sharding** — split the selected tests into N deterministic, balanced shards so a matrix of
  ephemeral off-box runners executes them in parallel (see `.github/workflows/ci-sharded.yml`).

`impacted_tests` and `shard` are pure and unit-tested. `main()` wires them to git + the tree:
  python scripts/select_impacted_tests.py --base origin/master [--shards N --index I] [--json]
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
    "requirements", "pyproject.toml", "uv.lock", ".python-version", "tests/",
)
# Non-Python paths that provably cannot change a test outcome — safe to ignore. Anything else
# non-Python (JSON/SQL/HTML/CSV/fixtures/config) forces the full suite (it may feed a test).
IRRELEVANT_SUFFIXES = (".md",)
IRRELEVANT_PREFIXES = ("docs/",)


def _is_broad(path: str, broad_prefixes) -> bool:
    p = path.replace("\\", "/")
    return any(p == b or p.startswith(b) for b in broad_prefixes)


def _is_irrelevant_nonpy(path: str) -> bool:
    p = path.replace("\\", "/")
    return p.endswith(IRRELEVANT_SUFFIXES) or p.startswith(IRRELEVANT_PREFIXES)


def _module_aliases(path: str) -> Set[str]:
    """Module names a `.py` file is importable as: 'foo.py'->{'foo'}; 'db/core.py'->
    {'db.core','db','core'} so `import db`, `from db.core import x`, or `from db import core` match.
    Over-approximates (bare names) — over-selecting tests is safe; under-selecting is not."""
    p = path.replace("\\", "/")
    if not p.endswith(".py"):
        return set()
    parts = p[:-3].split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return set()
    return {a for a in ({".".join(parts), parts[0], parts[-1]}) if a}


def _parse_imports(text: str) -> Set[str]:
    """Module aliases a source file imports. Handles `import a, b.c, d as e`, `from a.b import x`,
    and relative `from . import sib` / `from .mod import x`. Over-captures rather than miss an edge."""
    out: Set[str] = set()
    for raw in text.splitlines():
        s = raw.split("#", 1)[0].strip()   # drop inline comments (e.g. `import x  # noqa: E402`)
        if s.startswith("from "):
            m = re.match(r"from\s+(\.*)([\w.]*)\s+import\s+(.+)", s)
            if not m:
                continue
            dots, mod, names = m.groups()
            if mod:
                out.add(mod)
                out.add(mod.split(".")[0])
                out.add(mod.split(".")[-1])
            if dots and not mod:                       # from . import a, b  -> sibling modules
                for n in re.split(r"[,\s]+", names.replace("(", " ").replace(")", " ")):
                    n = n.split(" as ")[0].strip()
                    if n:
                        out.add(n)
        elif s.startswith("import "):
            for part in s[len("import "):].split(","):  # import a, b.c as d
                name = part.strip().split(" as ")[0].strip()
                if name:
                    out.add(name)
                    out.add(name.split(".")[0])
    return {x for x in out if x}


def _alias_to_files(module_imports: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    table: Dict[str, Set[str]] = {}
    for fl in module_imports:
        for a in _module_aliases(fl):
            table.setdefault(a, set()).add(fl)
    return table


def _reachable_files(start: Set[str], module_imports: Dict[str, Set[str]],
                     alias_to_files: Dict[str, Set[str]]) -> Set[str]:
    """All files transitively imported starting from ``start`` (following import edges)."""
    seen = set(start)
    stack = list(start)
    while stack:
        f = stack.pop()
        for alias in module_imports.get(f, ()):  # aliases this file imports
            for tgt in alias_to_files.get(alias, ()):  # files exposing that alias
                if tgt not in seen:
                    seen.add(tgt)
                    stack.append(tgt)
    return seen


def impacted_tests(changed_files: List[str], all_tests: List[str], *,
                   module_imports: Dict[str, Set[str]],
                   broad_prefixes=BROAD_PREFIXES) -> Optional[List[str]]:
    """Tests a PR's diff can affect, or **None** = "run the full suite" (fail-safe).

    ``module_imports`` maps EVERY repo ``.py`` file to the module aliases it imports. A test is
    selected iff it transitively imports a changed module (or is itself changed, or is the
    ``test_<module>.py`` for one). Returns None on a broad change, any non-Python/non-doc change,
    or if any changed module is not reached by an identifiable test.
    """
    all_set = set(all_tests)
    impacted: Set[str] = set()
    changed_py: Set[str] = set()
    for f in changed_files:
        if _is_broad(f, broad_prefixes):
            return None
        if f in all_set:                      # a changed test runs itself
            impacted.add(f)
        elif f.endswith(".py"):
            changed_py.add(f)
        elif not _is_irrelevant_nonpy(f):     # data/fixture/SQL/template/config -> full suite
            return None
    if not changed_py:
        return sorted(impacted)               # only tests and/or docs changed

    alias_to_files = _alias_to_files(module_imports)
    base_names = {os.path.basename(t): t for t in all_tests}

    # A test is impacted iff it transitively imports a changed module.
    for t in all_tests:
        if _reachable_files({t}, module_imports, alias_to_files) & changed_py:
            impacted.add(t)
    for f in changed_py:                      # test_<module>.py convention (additive, safe)
        cand = "test_" + os.path.basename(f)
        if cand in base_names:
            impacted.add(base_names[cand])

    # Fail-safe: every changed module must be reached by >=1 selected test, else we can't prove
    # the impacted set is complete -> run everything.
    for f in changed_py:
        cand = "test_" + os.path.basename(f)
        reached = any(f in _reachable_files({t}, module_imports, alias_to_files) for t in impacted)
        if not reached and cand not in base_names:
            return None
    return sorted(impacted)


def shard(items: List[str], num_shards: int, index: int) -> List[str]:
    """Deterministic, balanced round-robin shard: shard i of N gets items[i], items[i+N], ..."""
    if num_shards <= 1:
        return sorted(items)
    ordered = sorted(items)
    return [x for i, x in enumerate(ordered) if i % num_shards == index]


# --- filesystem / git wiring (not unit-tested; the pure functions above are) ---

def discover_python_files(root: str) -> List[str]:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir.startswith((".git", ".venv", "node_modules")):
            continue
        for fn in files:
            if fn.endswith(".py"):
                out.append(os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/"))
    return sorted(out)


def is_test_file(path: str) -> bool:
    base = os.path.basename(path)
    return base.startswith("test_") or base.endswith("_test.py")


def build_module_imports(root: str, py_files: List[str]) -> Dict[str, Set[str]]:
    imports: Dict[str, Set[str]] = {}
    for f in py_files:
        try:
            text = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
            imports[f] = _parse_imports(text)
        except OSError:
            imports[f] = set()
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

    py_files = discover_python_files(root)
    tests = [f for f in py_files if is_test_file(f)]
    try:
        changed = changed_files(args.base, root)
    except subprocess.CalledProcessError:
        changed = None  # can't diff -> run everything
    selected = impacted_tests(changed, tests, module_imports=build_module_imports(root, py_files)) \
        if changed is not None else None

    run_all = selected is None
    to_run = shard(tests if run_all else selected, args.shards, args.index)
    if args.json:
        print(json.dumps({"run_all": run_all, "count": len(to_run), "tests": to_run}))
    else:
        for t in to_run:
            print(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
