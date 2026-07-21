#!/usr/bin/env python3
"""Provider-process fixture used by the DISPATCH-13 integration proof.

The process deliberately receives no MCP endpoint from Connect.  Like a real
Codex, Claude, or Cursor installation, it discovers communication configuration
from the host account after boot.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def main() -> int:
    note = sys.argv[1] if len(sys.argv) > 1 else ""
    host_home = Path(os.environ["HOME"])
    config = json.loads(
        (host_home / ".switchboard" / "mcp.json").read_text(encoding="utf-8")
    )
    message_board = Path(config["message_board"])
    receipt = {
        "assignment_id": os.environ["SWITCHBOARD_CONNECT_ASSIGNMENT_ID"],
        "lease_id": os.environ["SWITCHBOARD_CONNECT_LEASE_ID"],
        "principal_ref": os.environ["SWITCHBOARD_CONNECT_PRINCIPAL_REF"],
        "provider_runtime": os.environ["PROVIDER_RUNTIME"],
        "used_host_mcp": True,
        "work_ref": os.environ["SWITCHBOARD_CONNECT_WORK_REF"],
        "note": note,
    }
    with message_board.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(receipt, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
