"""``python3 -m deliverable_gates`` — validate and list the gate registry.

Exit 0 and print one line per gate when the manifest is valid; exit 1 with the
failure reason otherwise. Cheap enough to wire into CI as a manifest lint.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import GateRegistryError, load_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deliverable_gates", description=__doc__)
    parser.add_argument("--path", type=Path, default=None, help="manifest path override")
    parser.add_argument("--json", action="store_true", help="dump the validated manifest as JSON")
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(args.path, use_cache=False)
    except GateRegistryError as exc:
        print(f"gate registry INVALID: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0

    gates = manifest["gates"]
    print(f"gate registry OK: {len(gates)} gate(s)")
    for gate in gates:
        flags = []
        flags.append("required" if gate.get("required") else "optional")
        if gate.get("pending"):
            flags.append(f"pending:{gate.get('pending_task', '?')}")
        print(f"  {gate['id']:<40} {gate['kind']:<16} {', '.join(flags)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
