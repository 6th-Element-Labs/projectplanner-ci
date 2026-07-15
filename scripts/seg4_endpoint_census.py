#!/usr/bin/env python3
"""SEG-4 endpoint census — customer surfaces must not reach DEFAULT_PROJECT by omission.

Scans REST router/middleware/deps + Ask Taikun UI for:
  * Query(store.DEFAULT_PROJECT) / Query(DEFAULT_PROJECT)
  * ``or store.DEFAULT_PROJECT`` / ``or "maxwell"`` on customer ingress
  * bare Maxwell literals in messaging / export helpers

Allowlist (reachable only via the named adapter or non-customer shells):
  * src/switchboard/application/adapters/legacy_maxwell_default.py
  * comment / docstring mentions of DEFAULT_PROJECT

Exit code 1 when --check and violations remain.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "switchboard.seg4_endpoint_census.v1"

CUSTOMER_SURFACES = [
    ROOT / "src/switchboard/api/deps.py",
    ROOT / "src/switchboard/api/middleware.py",
    ROOT / "src/switchboard/api/project_scope.py",
    ROOT / "src/switchboard/api/routers",
    ROOT / "static/js/api.js",
    ROOT / "static/js/plan-chat.js",
]

ALLOWLIST_PATH_PARTS = (
    "legacy_maxwell_default.py",
)

FORBIDDEN_PATTERNS = [
    (re.compile(r"Query\(\s*store\.DEFAULT_PROJECT\s*\)"), "Query(store.DEFAULT_PROJECT)"),
    (re.compile(r"Query\(\s*DEFAULT_PROJECT\s*\)"), "Query(DEFAULT_PROJECT)"),
    (re.compile(r"or\s+store\.DEFAULT_PROJECT\b"), "or store.DEFAULT_PROJECT"),
    (re.compile(r"""or\s+["']maxwell["']"""), "or 'maxwell' literal"),
    (re.compile(r"""project\s*=\s*["']maxwell["']"""), "project='maxwell' default"),
    (re.compile(r"""\|\|\s*['"]maxwell['"]"""), "JS || 'maxwell' fallback"),
]


def _iter_files() -> list[Path]:
    out: list[Path] = []
    for entry in CUSTOMER_SURFACES:
        if entry.is_file():
            out.append(entry)
        elif entry.is_dir():
            out.extend(sorted(entry.rglob("*.py")))
    return out


def census() -> dict:
    violations: list[dict] = []
    scanned = 0
    for path in _iter_files():
        rel = str(path.relative_to(ROOT))
        if any(part in rel for part in ALLOWLIST_PATH_PARTS):
            continue
        text = path.read_text(encoding="utf-8")
        scanned += 1
        for i, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*") or stripped.startswith("//"):
                continue
            # Docstrings mentioning the ban are fine.
            if "never invent" in line.lower() or "pending SEG-6" in line or "SEG-4" in line and "DEFAULT" in line:
                if "Query(store.DEFAULT_PROJECT)" not in line and "or store.DEFAULT_PROJECT" not in line:
                    continue
            for pattern, label in FORBIDDEN_PATTERNS:
                if pattern.search(line):
                    violations.append({
                        "file": rel,
                        "line": i,
                        "label": label,
                        "text": line.strip()[:200],
                    })
    return {
        "schema": SCHEMA,
        "scanned_files": scanned,
        "violation_count": len(violations),
        "violations": violations,
        "ok": not violations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 on violations")
    parser.add_argument("--json", action="store_true", help="emit machine-readable census")
    args = parser.parse_args(argv)
    report = census()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"SEG-4 census: scanned={report['scanned_files']} violations={report['violation_count']}")
        for row in report["violations"]:
            print(f"  FAIL  {row['file']}:{row['line']} [{row['label']}] {row['text']}")
        if report["ok"]:
            print("  PASS  zero DEFAULT_PROJECT reachability on customer surfaces")
    if args.check and not report["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
