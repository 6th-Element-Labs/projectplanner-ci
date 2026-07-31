#!/usr/bin/env python3
"""ADAPTER-43: legacy enrolled hosts get a safe server-side lease floor."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = Path(tempfile.mkdtemp(prefix="adapter43-enrolled-host-ttl-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.storage.repositories.agent_host_enrollments import (  # noqa: E402
    check_agent_host_identity,
)
from switchboard.storage.repositories.coordination import (
    _registered_host_heartbeat_ttl_s,
)

P = "switchboard"
HOST = "host/adapter43-enrolled"
PRINCIPAL = "principal/adapter43-enrolled"

try:
    store.init_db(P)
    now = time.time()
    with _conn(P) as connection:
        connection.execute(
            "INSERT INTO agent_host_enrollments("
            "enrollment_id,project_id,requested_host_id,host_id,owner_user_id,"
            "tenant_allowlist_json,project_allowlist_json,provider_allowlist_json,"
            "execution_policy_json,bootstrap_hash,bootstrap_expires_at,"
            "bootstrap_consumed_at,principal_id,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "hostenroll-adapter43",
                P,
                HOST,
                HOST,
                "user/adapter43",
                "[]",
                json.dumps([P]),
                "[]",
                "{}",
                "adapter43-bootstrap",
                now + 3600,
                now,
                PRINCIPAL,
                "active",
                now,
                now,
            ),
        )

    advertised_legacy = {"heartbeat_ttl_s": 60}
    enrolled_identity = check_agent_host_identity(HOST, PRINCIPAL, project=P)
    assert enrolled_identity.get("required") is True
    assert "enrollment_id" not in enrolled_identity
    assert _registered_host_heartbeat_ttl_s(
        advertised_legacy, enrolled_identity
    ) == 180

    unmanaged_identity = check_agent_host_identity(
        "host/adapter43-unmanaged", "", project=P
    )
    assert unmanaged_identity == {"required": False, "allowed": True}
    assert _registered_host_heartbeat_ttl_s(
        advertised_legacy, unmanaged_identity
    ) == 60

    assert _registered_host_heartbeat_ttl_s(
        {"heartbeat_ttl_s": 240}, enrolled_identity
    ) == 240

    print("ADAPTER-43 enrolled host TTL floor: PASS")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
