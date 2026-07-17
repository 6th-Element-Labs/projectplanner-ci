#!/usr/bin/env python3
"""Render and validate the declarative process-cut deployment inventory."""
from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path


def load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "switchboard.service_cut_inventory.v1":
        raise ValueError("unexpected service-cut inventory schema")
    services = data.get("services") or []
    names = [row.get("name") for row in services]
    ports = [row.get("port") for row in services]
    if len(names) != len(set(names)) or len(ports) != len(set(ports)):
        raise ValueError("service names and ports must be unique")
    for row in services:
        for field in ("name", "unit", "port", "health", "restart_order"):
            if row.get(field) in (None, ""):
                raise ValueError(f"{row.get('name')}: missing {field}")
    return data


def shell_array(name: str, values: list[str]) -> str:
    return f"{name}=({' '.join(shlex.quote(value) for value in values)})"


def render_shell(data: dict) -> str:
    rows = sorted(data["services"], key=lambda row: row["restart_order"])
    cut = [row for row in rows if row["name"] != "projectplanner"]
    health = [f"http://127.0.0.1:{row['port']}{row['health']}" for row in rows]
    ready = [f"http://127.0.0.1:{row['port']}{row['ready']}"
             for row in rows if row.get("ready")]
    proof_services = [f"{row['name']}:{row['port']}" for row in cut]
    proof_edges = [f"{owner}:{row['port']}" for row in cut
                   for owner in row.get("edge_owns", [])]
    proof_ready = [f"{row.get('runtime_identity') or row['name']}:{row['port']}:{row['ready']}"
                   for row in rows if row.get("ready")]
    return "\n".join((
        shell_array("CUT_SERVICES", [row["name"] for row in cut]),
        shell_array("CUT_UNITS", [row["unit"] for row in cut]),
        shell_array("REQUIRED_HEALTH_URLS", health),
        shell_array("REQUIRED_READY_URLS", ready),
        shell_array("PROOF_SERVICES", proof_services),
        shell_array("PROOF_EDGE_OWNS", proof_edges),
        shell_array("PROOF_READY", proof_ready),
    ))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "shell"))
    parser.add_argument("--inventory", type=Path, required=True)
    args = parser.parse_args()
    data = load(args.inventory)
    if args.command == "shell":
        print(render_shell(data))
    else:
        print(json.dumps({"ok": True, "schema": data["schema"],
                          "service_count": len(data["services"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
