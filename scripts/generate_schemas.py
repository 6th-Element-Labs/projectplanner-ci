#!/usr/bin/env python3
"""Generate or check the canonical Switchboard event JSON Schema registry (ARCH-MS-42).

Usage:
  python scripts/generate_schemas.py            # write schemas/*.json + manifest.json
  python scripts/generate_schemas.py --check    # exit 1 if the checked-in tree is stale
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Repo root first so legacy modules like ``constants`` resolve when contracts
# pull domain helpers; scripts/ puts ``switchboard_path`` on the path.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import switchboard_path  # noqa: E402, F401 — puts src/ on sys.path

from switchboard.contracts.schema_export import (  # noqa: E402
    check_schema_registry,
    registered_v1_schemas,
    write_schema_registry,
)

DEFAULT_OUT = ROOT / "schemas"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare regenerated schemas to the checked-in tree and exit 1 on drift.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output directory (default: {DEFAULT_OUT})",
    )
    args = parser.parse_args(argv)

    schemas = registered_v1_schemas()
    if not schemas:
        print("no switchboard.*.v1 schemas registered", file=sys.stderr)
        return 2

    out: Path = args.out
    if args.check:
        if not out.is_dir():
            print(
                f"missing checked-in schema registry: {out}\n"
                f"Regenerate with: python scripts/generate_schemas.py",
                file=sys.stderr,
            )
            return 1
        problems = check_schema_registry(out)
        if problems:
            print(
                "Schema registry drift detected:\n  - "
                + "\n  - ".join(problems)
                + "\nRegenerate with: python scripts/generate_schemas.py",
                file=sys.stderr,
            )
            return 1
        print(f"OK  {out} matches {len(schemas)} registered switchboard.*.v1 schemas")
        return 0

    manifest = write_schema_registry(out)
    print(f"Wrote {manifest['count']} schemas + {out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
