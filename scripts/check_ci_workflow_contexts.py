#!/usr/bin/env python3
"""Compare the required status-context vocabularies emitted by CI workflows."""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import FrozenSet


CONTEXT_ENV_RE = re.compile(
    r"^\s*(?:[A-Z0-9_]*STATUS_CONTEXT)\s*:\s*[\"']?([^#\"'\n]+?)[\"']?\s*$",
    re.MULTILINE,
)


def emitted_status_contexts(path: Path) -> FrozenSet[str]:
    """Return literal status contexts declared by a workflow's *_STATUS_CONTEXT env."""
    text = path.read_text(encoding="utf-8")
    contexts = frozenset(
        match.group(1).strip() for match in CONTEXT_ENV_RE.finditer(text)
    )
    if not contexts:
        raise ValueError(f"{path}: no *_STATUS_CONTEXT declarations found")
    return contexts


def assert_workflow_contexts_match(left: Path, right: Path) -> None:
    left_contexts = emitted_status_contexts(left)
    right_contexts = emitted_status_contexts(right)
    if left_contexts != right_contexts:
        missing = sorted(left_contexts - right_contexts)
        extra = sorted(right_contexts - left_contexts)
        raise AssertionError(
            f"required status-context vocabulary diverged: {left} != {right}; "
            f"missing from {right}: {missing}; extra in {right}: {extra}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    args = parser.parse_args()
    assert_workflow_contexts_match(args.left, args.right)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
