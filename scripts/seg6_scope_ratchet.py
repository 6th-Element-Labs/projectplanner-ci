#!/usr/bin/env python3
"""Mechanical SEG-6 ratchet for project-sensitive shared surfaces."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_CALLS = {
    "activity_since", "add_digest", "last_digest", "list_digests",
    "compute_plan_signals", "get_meta",
}
SCOPED_MODULES = (ROOT / "digest.py",)


def violations() -> list[str]:
    found: list[str] = []
    for path in SCOPED_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else "")
            if name not in SENSITIVE_CALLS:
                continue
            if not any(keyword.arg == "project" for keyword in node.keywords):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name} lacks project=")
    return found


if __name__ == "__main__":
    errors = violations()
    if errors:
        print("\n".join(errors))
        raise SystemExit(1)
    print("SEG-6 scope ratchet passed")
