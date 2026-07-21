"""UI-46: native Codex learns its exact assignment through Switchboard MCP."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

tmp = tempfile.mkdtemp(prefix="ui46-mcp-bootstrap-")
os.environ.update({
    "PM_DB_PATH": os.path.join(tmp, "maxwell.db"),
    "PM_HELM_DB_PATH": os.path.join(tmp, "helm.db"),
    "PM_SWITCHBOARD_DB_PATH": os.path.join(tmp, "switchboard.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": os.path.join(tmp, "registry.db"),
    "PM_DYNAMIC_PROJECTS_DIR": tmp,
    "PM_PROJECT": "switchboard",
    "PM_BASE": "https://plan.taikunai.com",
})

import auth  # noqa: E402
import store  # noqa: E402
from adapters import codex_local_worker  # noqa: E402
from constants import MCP_OPERATOR_SCOPES  # noqa: E402
from switchboard.mcp.authorization import (  # noqa: E402
    MCPAuthorizationGuard,
    transport_principal_scope,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


prompt = codex_local_worker._prompt({
    "task_id": "UI-46",
    "deliverable_id": "agent-host-autopilot",
    "title": "Must not cross into the prompt",
    "description": "The MCP server owns this assignment.",
    "task": {"title": "Nested secret title", "description": "Nested description"},
}, source_sha="a" * 40, wake_id="wake-ui46",
   execution_connection_id="execconn-ui46")
ok(prompt == ("Do UI-46 for deliverable agent-host-autopilot in project "
              "switchboard via Switchboard."),
   "native child receives stable task, deliverable, and project ids")
ok("description" not in prompt.lower() and "source sha" not in prompt.lower(),
   "task payload and lifecycle metadata do not replace MCP boot")
fallback_prompt = codex_local_worker._prompt(
    {"task_id": "UI-47"}, source_sha="a" * 40, wake_id="wake-ui47",
    execution_connection_id="execconn-ui47")
ok(fallback_prompt == "Do UI-47 in project switchboard via Switchboard.",
   "unlinked tasks retain the same project-scoped MCP boot contract")

captured = []


def fake_http(method, path, body):
    captured.append((method, path, body))
    return {"issued": True, "token": "wst-child-only"}


values = {
    "task_id": "UI-46", "claim_id": "taskclaim-ui46",
    "work_session_id": "worksession-ui46", "runner_session_id": "run-ui46",
    "host_id": "host/steve-mbp-co16", "agent_id": "codex/UI-46",
    "wake_id": "wake-ui46", "source_sha": "a" * 40,
    "execution_connection_id": "execconn-ui46",
}
token, overrides = codex_local_worker._work_session_mcp_bootstrap(
    fake_http, values)
ok(token == "wst-child-only"
   and captured[0][1] == "/ixp/v1/work_sessions/worksession-ui46/mcp_token",
   "exact bound Work Session issues the child-only bearer")
ok(any("mcp_servers.taikun_plan.url" in item for item in overrides)
   and any("bearer_token_env_var" in item for item in overrides)
   and any("required=true" in item for item in overrides)
   and not any("enabled_tools" in item for item in overrides),
   "one-run Codex configuration connects the complete authenticated MCP surface")
worker_source = Path(codex_local_worker.__file__).read_text(encoding="utf-8")
ok('"--dangerously-bypass-approvals-and-sandbox"' in worker_source
   and '"workspace-write"' not in worker_source,
   "legacy native worker launches with the same unrestricted envelope as desktop")

store.init_project_registry()
store.init_db("switchboard")
task = store.create_task(
    {"workstream_id": "UI", "title": "MCP task bootstrap"},
    actor="test", project="switchboard")
agent_id = f"codex/{task['task_id']}"
store.register_agent(agent_id, "codex", lane="UI",
                     task_id=task["task_id"], project="switchboard")
claim = store.claim_task(task["task_id"], agent_id, actor="test",
                         project="switchboard")
created = store.create_work_session({
    "task_id": task["task_id"], "claim_id": claim["claim_id"],
    "agent_id": agent_id, "runtime": "codex", "repo_role": "canonical",
    "branch": f"codex/{task['task_id']}", "upstream": "origin/master",
    "base_sha": "a" * 40, "head_sha": "a" * 40,
    "worktree_path": os.path.join(tmp, "worktree"), "storage_mode": "worktree",
    "status": "active", "dirty_status": "clean", "conflict_marker_count": 0,
    "policy_profile": "code_strict",
}, actor="test", project="switchboard")
issued = store.issue_work_session_mcp_token(
    created["work_session"]["work_session_id"], actor="host/test",
    project="switchboard")
principal = auth.principal_for_token_any_project(issued["token"])
ok(principal and set(principal["scopes"]) == set(MCP_OPERATOR_SCOPES)
   and principal["project"] == "*"
   and principal["environment_operator"] is True
   and principal["assignment_project"] == "switchboard"
   and principal["bound_task_id"] == task["task_id"]
   and issued["expires_at"] > 0,
   "temporary bearer authenticates the child with the full operator contract")


def get_task(project="switchboard", task_id=""):
    return {"project": project, "task_id": task_id}


def search_tasks(project="switchboard"):
    return {"project": project}


guard = MCPAuthorizationGuard()
with transport_principal_scope(principal):
    allowed_boot = guard.wrap(get_task)(
        project="switchboard", task_id=task["task_id"])
    allowed_non_boot = guard.wrap(search_tasks)(project="switchboard")
    allowed_other_task = guard.wrap(get_task)(
        project="switchboard", task_id="UI-999")
    allowed_other_project = guard.wrap(get_task)(
        project="maxwell", task_id="MAX-999")
ok(allowed_boot["task_id"] == task["task_id"]
   and allowed_non_boot["project"] == "switchboard"
   and allowed_other_task["task_id"] == "UI-999"
   and allowed_other_project["project"] == "maxwell",
   "server gives the child the same cross-task and cross-project MCP tools")
store.update_work_session(
    created["work_session"]["work_session_id"], {"status": "expired"},
    actor="test", project="switchboard")
ok(auth.principal_for_token_any_project(issued["token"]) is None,
   "Work Session completion or expiry revokes child MCP authentication")

print(f"\nUI-46 native MCP bootstrap: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
