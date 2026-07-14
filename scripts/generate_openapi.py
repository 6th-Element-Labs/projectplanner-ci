#!/usr/bin/env python3
"""Generate or check the canonical Switchboard OpenAPI 3.1 artifact (ARCH-MS-41).

Usage:
  python scripts/generate_openapi.py            # write openapi/switchboard.openapi.json
  python scripts/generate_openapi.py --check    # exit 1 if the checked-in file is stale
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

from switchboard.contracts.openapi import (  # noqa: E402
    OPENAPI_VERSION,
    render_openapi_json,
)

DEFAULT_OUT = ROOT / "openapi" / "switchboard.openapi.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare regenerated OpenAPI to the checked-in artifact and exit 1 on drift.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output path (default: {DEFAULT_OUT})",
    )
    args = parser.parse_args(argv)

    rendered = render_openapi_json()
    if OPENAPI_VERSION not in rendered:
        print("generated document is missing openapi 3.1.0", file=sys.stderr)
        return 2

    out: Path = args.out
    if args.check:
        if not out.is_file():
            print(
                f"missing checked-in OpenAPI artifact: {out}\n"
                f"Regenerate with: python scripts/generate_openapi.py",
                file=sys.stderr,
            )
            return 1
        existing = out.read_text(encoding="utf-8")
        if existing != rendered:
            print(
                f"OpenAPI drift detected: {out} is out of date.\n"
                f"Regenerate with: python scripts/generate_openapi.py",
                file=sys.stderr,
            )
            return 1
        print(f"OK  {out} matches generated OpenAPI {OPENAPI_VERSION}")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
