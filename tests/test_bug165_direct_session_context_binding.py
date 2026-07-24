#!/usr/bin/env python3
"""BUG-165: MCP project authorization preserves direct-session bindings."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="bug165-context-binding-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)

import store  # noqa: E402
from switchboard.mcp import authorization  # noqa: E402


P = "switchboard"
TASK = "BUG-165"
AGENT = "agent/codex/bug-165"
PRINCIPAL = {
    "id": "direct-session/run_bug165",
    "kind": "direct_session",
    "display_name": AGENT,
    "project": "*",
    "environment_operator": True,
    "scopes": ["read", "write:ixp"],
    "bound_task_id": TASK,
    "bound_agent_id": AGENT,
    "bound_host_id": "host/bug165",
    "bound_wake_id": "wake-bug165",
    "bound_runner_session_id": "run_bug165",
}


try:
    store.init_db(P)
    with authorization.transport_principal_scope(PRINCIPAL):
        context = authorization.authorize_project_context(
            PRINCIPAL, P, authorization.AccessClass.WRITE)
        authorized = authorization.require_current_access(P, ("write:ixp",))

    assert context.bound_task_id == TASK
    assert context.bound_agent_id == AGENT
    assert authorized["bound_task_id"] == TASK
    assert authorized["bound_agent_id"] == AGENT

    binding = store.resolve_write_actor(
        AGENT, project=P, task_id=TASK, agent_id=AGENT,
        principal_id=authorized["id"], principal_kind=authorized["kind"],
        bound_task_id=authorized["bound_task_id"],
        bound_agent_id=authorized["bound_agent_id"],
    )
    assert binding.get("ok") is True, binding
    assert binding.get("binding") == "direct_session", binding
finally:
    shutil.rmtree(TMP, ignore_errors=True)
