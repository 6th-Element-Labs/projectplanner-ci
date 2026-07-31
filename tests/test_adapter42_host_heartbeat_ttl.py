#!/usr/bin/env python3
"""ADAPTER-42: host liveness survives bounded control-plane stalls."""
from __future__ import annotations

import os

from path_setup import ROOT  # noqa: F401

from adapters import agent_host
from adapters import agent_host_enrollment as enrollment


saved = os.environ.get("PM_HOST_HEARTBEAT_TTL_S")
try:
    os.environ.pop("PM_HOST_HEARTBEAT_TTL_S", None)
    assert agent_host.default_inventory()["heartbeat_ttl_s"] == 180

    os.environ["PM_HOST_HEARTBEAT_TTL_S"] = "240"
    assert agent_host.default_inventory()["heartbeat_ttl_s"] == 240

    os.environ["PM_HOST_HEARTBEAT_TTL_S"] = "60"
    assert agent_host.default_inventory()["heartbeat_ttl_s"] == 180

    os.environ["PM_HOST_HEARTBEAT_TTL_S"] = "not-a-number"
    assert agent_host.default_inventory()["heartbeat_ttl_s"] == 180

    assert enrollment.host_heartbeat_ttl_s(7200) == 3600
finally:
    if saved is None:
        os.environ.pop("PM_HOST_HEARTBEAT_TTL_S", None)
    else:
        os.environ["PM_HOST_HEARTBEAT_TTL_S"] = saved

print("ADAPTER-42 host heartbeat TTL: PASS")
