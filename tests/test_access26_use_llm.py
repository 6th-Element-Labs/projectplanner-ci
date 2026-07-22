#!/usr/bin/env python3
"""ACCESS-26: billable LLM capability is explicit and default-deny."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from path_setup import ROOT
from scripts.frontend_test_source import read_frontend_source


TMP = tempfile.mkdtemp(prefix="access26-use-llm-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "required"

import store  # noqa: E402
from constants import ROLE_SCOPES  # noqa: E402
from switchboard.api.middleware import _llm_required_scopes  # noqa: E402
from switchboard.mcp.authorization import (  # noqa: E402
    LLM_TOOLS,
    MCPAuthorizationGuard,
    READ_TOOLS,
    WRITE_TOOLS,
    transport_principal_scope,
)
from switchboard.mcp.tools import plan  # noqa: E402
from switchboard.storage.repositories import ai_admission  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    store.init_db("switchboard")
    ok("use:llm" not in ROLE_SCOPES["viewer"]
       and "use:llm" not in ROLE_SCOPES["contributor"]
       and "use:llm" in ROLE_SCOPES["operator"],
       "read and write:tasks no longer imply billable LLM use")
    ok("ask_plan" in LLM_TOOLS
       and not (LLM_TOOLS & READ_TOOLS)
       and not (LLM_TOOLS & WRITE_TOOLS),
       "billable MCP tools are declared only in the LLM census")

    protected = {
        ("/api/chat", "POST"),
        ("/api/chat/runs/run-1", "GET"),
        ("/api/tasks/ACCESS-26/chat", "POST"),
        ("/api/intake", "POST"),
        ("/api/intake/upload", "POST"),
        ("/api/digest", "POST"),
        ("/api/narration/narrate-now", "POST"),
        ("/api/deliverables/d-1/breakdown_proposals", "POST"),
    }
    ok(all(_llm_required_scopes(path, method) == ("use:llm",)
           for path, method in protected),
       "REST LLM entry and background-resume paths share one scope decision")
    ok(_llm_required_scopes("/api/tasks", "POST") == ()
       and _llm_required_scopes("/api/chat/history", "GET") == (),
       "ordinary writes and passive chat reads do not acquire billable authority")

    plan._SERVICES = plan.PlanToolServices(dumps=json.dumps)
    queued = []
    original_enqueue = store.enqueue_background_job
    original_admit = ai_admission.admit
    original_bind = ai_admission.bind_run
    store.enqueue_background_job = lambda **kwargs: queued.append(kwargs) or {"run_id": "run-1"}

    class _Decision:
        admission_id = "adm-1"
        status = ai_admission.ACTIVE
        reason_code = "authorized"

    ai_admission.admit = lambda **_kwargs: _Decision()
    ai_admission.bind_run = lambda *_args, **_kwargs: None
    guarded = MCPAuthorizationGuard().wrap(plan.ask_plan)
    read_only = {"id": "viewer", "kind": "user", "project": "switchboard",
                 "scopes": ["read"], "effective_scopes": ["read"]}
    with transport_principal_scope(read_only):
        try:
            guarded("question", project="switchboard")
            denied = False
        except (PermissionError, ValueError):
            denied = True
    ok(denied and not queued, "MCP ask_plan rejects a read-only principal before enqueue")

    llm_user = {"id": "operator", "kind": "user", "project": "switchboard",
                "scopes": ["read", "use:llm"],
                "effective_scopes": ["read", "use:llm"]}
    with transport_principal_scope(llm_user):
        allowed = json.loads(guarded("question", project="switchboard"))
    ok(allowed.get("run_id") == "run-1" and queued
       and queued[0].get("actor") == "operator",
       "MCP ask_plan binds the authorized principal to the queued run")

    ui = read_frontend_source(ROOT)
    ok("this.canUseLlm" in ui and "use:llm" in ui
       and "Your role does not include use:llm" in ui,
       "browser UI reads back and names missing LLM access")
finally:
    try:
        store.enqueue_background_job = original_enqueue
    except NameError:
        pass
    try:
        ai_admission.admit = original_admit
        ai_admission.bind_run = original_bind
    except NameError:
        pass

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
