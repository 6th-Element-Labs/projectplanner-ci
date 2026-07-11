#!/usr/bin/env python3
"""ARCH-MS-14: new tests share one direct-execution path shim."""
from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

from path_setup import ROOT, SRC


def ok(condition, message):
    if not condition:
        raise AssertionError(message)


ok((ROOT / "tests/__init__.py").is_file(), "tests/ is a package")
ok((ROOT / "tests/path_setup.py").is_file(), "tests/ has a shared path shim")
ok(str(ROOT) in sys.path, "path shim exposes repo-root modules")
ok(str(SRC) in sys.path, "path shim exposes the src/ package")

test_files = sorted((ROOT / "tests").glob("test_*.py"))
ok(bool(test_files), "tests/ contains discoverable test scripts")

for test_file in test_files:
    tree = ast.parse(test_file.read_text(encoding="utf-8"), filename=str(test_file))
    imports_shim = any(
        isinstance(node, ast.ImportFrom) and node.module == "path_setup"
        for node in tree.body
    )
    ok(imports_shim, f"{test_file.name} imports the shared path shim")

    manual_path_edits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"insert", "append"}
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "sys"
        and node.func.value.attr == "path"
    ]
    ok(not manual_path_edits, f"{test_file.name} has no one-off sys.path mutation")

with tempfile.TemporaryDirectory(prefix="arch-ms14-path-") as tmp:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(ROOT / 'tests')!r}); "
                "import path_setup; import store; import switchboard; "
                "print(path_setup.ROOT)"
            ),
        ],
        cwd=tmp,
        check=False,
        capture_output=True,
        text=True,
    )
ok(probe.returncode == 0, probe.stderr or "path shim imports work outside repo cwd")
ok(probe.stdout.strip() == str(ROOT), "path shim resolves the canonical repo root")

package_probe = subprocess.run(
    [sys.executable, "-m", "tests.test_arch_ms11_inbox_routing"],
    cwd=ROOT,
    check=False,
    capture_output=True,
    text=True,
)
ok(package_probe.returncode == 0, package_probe.stderr or "tests import as package modules")

print(f"ARCH-MS-14 test-layout checks passed ({len(test_files)} tests)")
