#!/usr/bin/env python3
"""Inventory tracked PM_* environment names and fail on unread declarations.

The census is intentionally conservative: any reference from non-test, non-documentation
runtime code counts as a defender.  Deployment declarations are narrower: assignments in
``.env.example`` and systemd ``Environment=PM_...`` entries.  A declared name with no runtime
defender is actionable dead configuration and makes ``--check`` fail.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable


SCHEMA = "switchboard.pm_env_flag_census.v1"
TOKEN_RE = re.compile(r"\bPM_[A-Z][A-Z0-9_]*\b")
ENV_ASSIGNMENT_RE = re.compile(r"^\s*(PM_[A-Z][A-Z0-9_]*)=")
SYSTEMD_ASSIGNMENT_RE = re.compile(
    r"\bEnvironment\s*=\s*[\"']?(PM_[A-Z][A-Z0-9_]*)="
)
TEXT_SUFFIXES = {
    ".css", ".html", ".js", ".json", ".md", ".py", ".service", ".sh",
    ".toml", ".txt", ".yaml", ".yml",
}


def tracked_files(root: Path) -> Iterable[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    )
    for raw in completed.stdout.split(b"\0"):
        if raw:
            yield root / raw.decode("utf-8", errors="surrogateescape")


def is_text_candidate(path: Path) -> bool:
    return (
        path.name.startswith(".env")
        or ".service" in path.name
        or path.suffix.lower() in TEXT_SUFFIXES
        or path.name.startswith("README")
    )


def bucket(relative: Path) -> str:
    parts = relative.parts
    if relative.name.startswith(".env") or ".service" in relative.name:
        return "configuration"
    if "tests" in parts or (relative.suffix == ".py" and relative.name.startswith("test_")):
        return "test"
    if "docs" in parts or relative.name.startswith("README") or relative.suffix == ".md":
        return "documentation"
    return "runtime"


def census(root: Path) -> dict:
    references: dict[str, dict[str, set[str]]] = {}
    declared: dict[str, set[str]] = {}

    for path in tracked_files(root):
        if not path.is_file() or not is_text_candidate(path):
            continue
        relative = path.relative_to(root)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        category = bucket(relative)
        for name in TOKEN_RE.findall(text):
            references.setdefault(name, {}).setdefault(category, set()).add(str(relative))

        for line in text.splitlines():
            match = ENV_ASSIGNMENT_RE.match(line) if path.name.startswith(".env") else None
            if not match and ".service" in path.name:
                match = SYSTEMD_ASSIGNMENT_RE.search(line)
            if match:
                declared.setdefault(match.group(1), set()).add(str(relative))

    runtime_names = {name for name, rows in references.items() if rows.get("runtime")}
    declared_names = set(declared)
    mentioned_names = set(references)
    unread = sorted(declared_names - runtime_names)
    mention_only = sorted(mentioned_names - runtime_names - declared_names)

    return {
        "schema": SCHEMA,
        "summary": {
            "declared": len(declared_names),
            "runtime_referenced": len(runtime_names),
            "tracked_names": len(mentioned_names),
            "unread_declarations": len(unread),
            "documentation_or_test_only": len(mention_only),
        },
        "declared": {
            name: sorted(declared[name]) for name in sorted(declared)
        },
        "runtime_referenced": sorted(runtime_names),
        "unread_declarations": unread,
        "documentation_or_test_only": mention_only,
        "dynamic_families": sorted(name for name in runtime_names if name.endswith("_")),
    }


def markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# PM environment flag census",
        "",
        f"Schema: `{report['schema']}`",
        "",
        "| Measure | Count |",
        "|---|---:|",
        f"| Tracked names | {summary['tracked_names']} |",
        f"| Runtime-referenced names/families | {summary['runtime_referenced']} |",
        f"| Tracked deployment declarations | {summary['declared']} |",
        f"| Unread deployment declarations | {summary['unread_declarations']} |",
        f"| Documentation/test-only tombstones or examples | {summary['documentation_or_test_only']} |",
        "",
        "## Declared names",
        "",
    ]
    for name, paths in report["declared"].items():
        lines.append(f"- `{name}` — {', '.join(f'`{path}`' for path in paths)}")
    lines.extend(["", "## Unread declarations", ""])
    if report["unread_declarations"]:
        lines.extend(f"- `{name}`" for name in report["unread_declarations"])
    else:
        lines.append("None. Every tracked deployment declaration has a runtime defender.")
    lines.extend(["", "## Documentation/test-only names", ""])
    if report["documentation_or_test_only"]:
        lines.extend(f"- `{name}`" for name in report["documentation_or_test_only"])
    else:
        lines.append("None.")
    lines.extend([
        "",
        "Run `python3 scripts/pm_env_flag_census.py --check` after changing environment",
        "configuration. Any declaration without a runtime reference fails closed.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args()

    report = census(args.root.resolve())
    if args.format == "markdown":
        print(markdown(report))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    if args.check and report["unread_declarations"]:
        print(
            "Unread PM_* declaration(s): " + ", ".join(report["unread_declarations"]),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
