#!/usr/bin/env python3
"""Mint one cloud-agent bearer token with read/write on every Switchboard board.

Run on the Plan VM after deploying master:

    cd /opt/projectplanner
    .venv/bin/python scripts/mint_global_cloud_agent_token.py

Prints the raw token once. Store it in SWITCHBOARD_TOKEN for cloud agents.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import auth  # noqa: E402
import store  # noqa: E402


def mint(display_name: str = "cloud-agent-global",
         principal_id: str = "agent-cloud-global",
         token: str | None = None) -> dict:
    store.init_project_registry()
    for pid in store.project_ids():
        store.init_db(pid)

    token = (token or "").strip() or auth.new_secret_token()
    resolved = store.resolve_principal_scopes([], role="admin")
    if resolved.get("error"):
        raise SystemExit(resolved["error"])

    if store.is_global_project_binding("*"):
        created = store.create_principal(
            kind="agent",
            display_name=display_name,
            token=token,
            scopes=resolved["scopes"],
            principal_id=principal_id,
            project="*",
        )
        if created.get("error"):
            raise SystemExit(created["error"])
        return {
            "mode": "global",
            "token": token,
            "principal_id": created["id"],
            "project_binding": "*",
        }

    # Pre-ACCESS fallback: replicate the same hash into every board DB.
    created_ids = []
    for pid in store.project_ids():
        created = store.create_principal(
            kind="agent",
            display_name=f"{display_name} ({pid})",
            token=token,
            scopes=resolved["scopes"],
            principal_id=f"{principal_id}-{pid}",
            project=pid,
        )
        if created.get("error"):
            raise SystemExit(created["error"])
        created_ids.append(created["id"])
    return {
        "mode": "replicated",
        "token": token,
        "principal_ids": created_ids,
        "project_binding": "per-board",
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--token",
        help="Use a predetermined bearer token (for registering a token already committed to cloud config).",
    )
    args = parser.parse_args()
    result = mint(token=args.token)
    print(json.dumps(result, indent=2, sort_keys=True))
    print("\nSet SWITCHBOARD_TOKEN to the token value above.", file=sys.stderr)


if __name__ == "__main__":
    main()
