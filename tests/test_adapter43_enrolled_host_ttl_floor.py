#!/usr/bin/env python3
"""ADAPTER-43: legacy enrolled hosts get a safe server-side lease floor."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from switchboard.storage.repositories.coordination import (
    _registered_host_heartbeat_ttl_s,
)


advertised_legacy = {"heartbeat_ttl_s": 60}

assert _registered_host_heartbeat_ttl_s(
    advertised_legacy,
    {"allowed": True, "enrollment_id": "hostenroll-example"},
) == 180

assert _registered_host_heartbeat_ttl_s(
    advertised_legacy,
    {"allowed": True},
) == 60

assert _registered_host_heartbeat_ttl_s(
    {"heartbeat_ttl_s": 240},
    {"allowed": True, "enrollment_id": "hostenroll-example"},
) == 240

print("ADAPTER-43 enrolled host TTL floor: PASS")
