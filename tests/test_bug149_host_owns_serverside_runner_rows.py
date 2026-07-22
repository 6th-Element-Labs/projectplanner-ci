#!/usr/bin/env python3
"""BUG-149: a host may heartbeat/terminalize its OWN runner rows regardless of
which principal created them.

Server-side paths (Connect late-bind, MCP registration under env-mcp-token)
legitimately create runner rows for a host before the host touches them. The
2026-07-22 fleet wedge: require_agent_host_runner_identity 403'd the host's
heartbeats for such a row, so terminal cleanup retried forever and every
claimed wake on the host starved. Ownership must be the host binding alone;
cross-HOST takeover stays refused."""
import os
import shutil
import tempfile

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="bug149-runner-identity-")
os.environ["PM_DB_PATH"] = os.path.join(TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from switchboard.api import deps  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(cond, msg):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    passed += int(bool(cond))
    failed += int(not cond)


def narrow(principal_id):
    return {"id": principal_id, "scopes": ["write:agent_host"]}


try:
    store.init_db(P)
    store.register_host({
        "host_id": "host/mac-a",
        "runtimes": [{"runtime": "codex", "lanes": ["BUG"]}],
        "limits": {"max_sessions": 4},
        "heartbeat_ttl_s": 60,
    }, principal_id="host-principal-a", actor="host/mac-a", project=P)
    # Server-side registration (the wedge shape): row created under env-mcp-token.
    store.upsert_runner_session({
        "runner_session_id": "run_serverside",
        "host_id": "host/mac-a",
        "agent_id": "agent/codex/bug-148",
        "runtime": "codex",
        "status": "running",
    }, principal_id="env-mcp-token", actor="coordinator", project=P)

    try:
        deps.require_agent_host_runner_identity(
            narrow("host-principal-a"), "run_serverside", "host/mac-a", P)
        same_host_allowed = True
    except HTTPException:
        same_host_allowed = False
    ok(same_host_allowed,
       "host bearer may manage a same-host row created by env-mcp-token")

    try:
        deps.require_agent_host_runner_identity(
            narrow("host-principal-b"), "run_serverside", "host/mac-b", P)
        cross_host_allowed = True
    except HTTPException as exc:
        cross_host_allowed = False
        cross_host_code = exc.status_code
    ok(not cross_host_allowed and cross_host_code == 403,
       "cross-host takeover of an existing runner id still 403s")

    try:
        deps.require_agent_host_runner_identity(
            narrow("host-principal-a"), "run_never_seen", "host/mac-a", P)
        fresh_allowed = True
    except HTTPException:
        fresh_allowed = False
    ok(fresh_allowed, "a brand-new runner id is registrable (no existing row)")

    ok(not deps.is_narrow_agent_host_principal(
        {"id": "op", "scopes": ["write:agent_host", "admin"]}),
       "operator/admin principals bypass the narrow fence (unchanged)")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nBUG-149 host-owns-serverside-runner-rows: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
