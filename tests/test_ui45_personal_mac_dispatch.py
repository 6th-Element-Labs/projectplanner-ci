#!/usr/bin/env python3
"""UI-45: browser personal-Codex dispatch targets the enrolled native Mac."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="ui45-personal-dispatch-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_SWITCHBOARD_PUBLIC_BASE"] = "https://plan.example"
os.environ["PM_RUNNER_PTY_RELAY_SECRET"] = "ui45-direct-relay-secret"

import dispatch  # noqa: E402
import auth  # noqa: E402
import store  # noqa: E402
from constants import MCP_OPERATOR_SCOPES  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.mcp.authorization import (  # noqa: E402
    MCPAuthorizationGuard,
    transport_principal_scope,
)
from switchboard.application.commands import runner_pty  # noqa: E402


P = "switchboard"
OWNER = "user/ui45-owner"
MAC = "host/ui45-mac"
OTHER = "host/ui45-aws"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def placement(host_class):
    return {
        "schema": "switchboard.agent_host_placement.v1",
        "wakeable": True,
        "drain_state": "accepting",
        "host_class": host_class,
        "projects": [P],
        "providers": ["openai-codex"],
        "repositories": ["6th-Element-Labs/projectplanner"],
        "session_policies": ["code_strict"],
        "isolation_modes": ["task_worktree"],
        "runtime_binaries": ["git", "python3"],
        "concurrency": {"max_sessions": 8},
        "cost_class": "already_paid" if host_class == "persistent" else "ephemeral_variable",
    }


def host_inventory(host_id, host_class):
    local_auth = {
        "available": True,
        "runtime": "codex",
        "auth_mode": "chatgpt_personal",
        "account_fingerprint": "acct-ui45",
        "credential_values_redacted": True,
        "provider_credential_exported": False,
    }
    return {
        "host_id": host_id,
        "hostname": host_id.rsplit("/", 1)[-1],
        "agent_host_version": "0.2.14",
        "repo_root": str(ROOT),
        "runtimes": [{
            "runtime": "codex",
            "lanes": [],
            "capabilities": ["docs", "github", "python", "tests"],
            "policy": {
                "allow_work": True,
                "allow_global_claim": False,
                "lane_mode": "all_project_lanes",
            },
            "local_auth": local_auth,
        }],
        "limits": {"max_sessions": 8},
        "capacity": {
            "active_sessions": 0,
            "local_auth": local_auth,
            "placement": placement(host_class),
        },
        "heartbeat_ttl_s": 60,
    }


try:
    store.init_db(P)
    task = store.create_task({
        "workstream_id": "UI",
        "title": "UI-45 native personal dispatch proof",
        "description": "policy_profile:code_strict",
        "ui_impact": "yes",
    }, actor="ui45-test", project=P)
    task_id = task["task_id"]
    store.create_deliverable({
        "id": "ui45-direct-deliverable",
        "title": "UI-45 direct Mac proof",
    }, actor="ui45-test", project=P)
    store.link_task_to_deliverable(
        "ui45-direct-deliverable", P, task_id,
        actor="ui45-test", project=P)

    mac = store.register_host(
        host_inventory(MAC, "persistent"), principal_id="principal/ui45-mac",
        actor=MAC, project=P)
    other = store.register_host(
        host_inventory(OTHER, "ephemeral"), principal_id="principal/ui45-aws",
        actor=OTHER, project=P)
    ok(not mac.get("error") and not other.get("error"),
       "native Mac and competing cloud host are registered")

    now = time.time()
    with _conn(P) as connection:
        connection.execute(
            "INSERT INTO principals(id,kind,display_name,project,scopes,token_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("principal/ui45-mac", "host", MAC, P,
             json.dumps(["read", "write:agent_host"]), "ui45-host-token", now),
        )
        connection.execute(
            "INSERT INTO agent_host_enrollments("
            "enrollment_id,project_id,requested_host_id,host_id,owner_user_id,"
            "tenant_allowlist_json,project_allowlist_json,provider_allowlist_json,"
            "bootstrap_hash,bootstrap_expires_at,bootstrap_consumed_at,principal_id,"
            "public_key_fingerprint,identity_generation,package_version,platform,"
            "hostname,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("hostenroll-ui45", P, MAC, MAC, OWNER, "[]", json.dumps([P]),
             json.dumps(["openai-codex"]), "ui45-bootstrap", now + 3600, now,
             "principal/ui45-mac", "sha256:" + "a" * 64, 1, "0.2.14", "macos",
             "ui45-mac", "active", now, now),
        )

    result = dispatch.dispatch(
        task_id, actor=OWNER, principal_id=OWNER, project=P, runtime="codex")
    ok(result.get("dispatched") is True and result.get("host_id") == MAC,
       "browser personal-Codex dispatch targets the owner's enrollment")
    wake = next(
        row for row in store.list_wake_intents(project=P)
        if row.get("wake_id") == result.get("wake_id")
    )
    selector = wake.get("selector") or {}
    policy = wake.get("policy") or {}
    placement_result = wake.get("placement") or {}
    ok(selector.get("host_id") == MAC and selector.get("task_id") == task_id
       and selector.get("agent_id") == f"codex/{task_id}",
       "wake is exact-host and exact-task bound")
    ok("cloud_execution" not in set(selector.get("capabilities") or [])
       and policy.get("mode") == "direct_task"
       and policy.get("execution_mode") == "direct_personal_cli"
       and policy.get("require_runner_bind") is False,
       "personal dispatch is a direct native CLI assignment, not a scheduler claim")
    assignment = policy.get("assignment") or {}
    ok(assignment.get("schema") == "switchboard.direct_cli_assignment.v1"
       and assignment.get("task_id") == task_id
       and assignment.get("host_id") == MAC
       and assignment.get("agent_id") == f"codex/{task_id}"
       and assignment.get("prompt") == (
           f"Do {task_id} for deliverable ui45-direct-deliverable "
           f"in project {P} via Switchboard."),
       "direct assignment gives the selected Mac one exact MCP boot command")
    ok(assignment.get("deliverable_id") == "ui45-direct-deliverable"
       and (assignment.get("repository") or {}).get("branch") == f"codex/{task_id.lower()}",
       "direct assignment identifies the deliverable and intended task branch")

    wrong_claim = store.claim_wake(
        OTHER, wake["wake_id"], principal_id="principal/ui45-aws",
        actor=OTHER, project=P)
    right_claim = store.claim_wake(
        MAC, wake["wake_id"], principal_id="principal/ui45-mac",
        actor=MAC, project=P)
    ok(wrong_claim.get("claimed") is not True
       and "host_id_mismatch" in wrong_claim.get("reason_codes", []),
       "a different Codex host cannot steal the personal wake")
    ok(right_claim.get("claimed") is True,
       "the enrolled Mac atomically claims the exact wake")

    duplicate = dispatch.dispatch(
        task_id, actor=OWNER, principal_id=OWNER, project=P, runtime="codex")
    ok(duplicate.get("wake_id") == wake["wake_id"]
       and len(store.list_wake_intents(task_id=task_id, project=P)) == 1,
       "repeat click collapses onto the matching active personal wake")

    failed_wake = store.cancel_wake(
        wake["wake_id"], reason="runner bind test terminal",
        actor=OWNER, project=P)
    retry = dispatch.dispatch(
        task_id, actor=OWNER, principal_id=OWNER, project=P, runtime="codex")
    ok(failed_wake.get("status") == "cancelled"
       and retry.get("wake_id") not in (None, wake["wake_id"])
       and len(store.list_wake_intents(task_id=task_id, project=P)) == 2,
       "browser retry creates one fresh wake after a terminal attempt")

    retry_duplicate = dispatch.dispatch(
        task_id, actor=OWNER, principal_id=OWNER, project=P, runtime="codex")
    ok(retry_duplicate.get("wake_id") == retry.get("wake_id")
       and len(store.list_wake_intents(task_id=task_id, project=P)) == 2,
       "repeat retry click cannot create parallel duplicate sessions")

    missing = dispatch.dispatch(
        task_id, actor="user/other", principal_id="user/other", project=P,
        runtime="codex")
    ok(missing.get("error") == "personal_agent_host_not_enrolled",
       "personal dispatch fails clearly when the signed-in user has no enrollment")

    direct_wake = next(
        row for row in store.list_wake_intents(project=P)
        if row.get("wake_id") == retry.get("wake_id")
    )
    direct_runner_id = "run_" + __import__("hashlib").sha256(
        f"{direct_wake['wake_id']}:{MAC}".encode()).hexdigest()[:16]
    issued = store.issue_direct_session_mcp_token(
        direct_wake["wake_id"], MAC, direct_runner_id,
        principal_id="principal/ui45-mac", actor=MAC, project=P)
    direct_principal = auth.principal_for_token_any_project(issued.get("token") or "")
    os.environ["PM_MCP_TOKEN"] = "ui45-operator-token"
    operator_principal = auth._env_principal("ui45-operator-token", P)
    os.environ.pop("PM_MCP_TOKEN", None)
    ok(issued.get("issued") is True
       and (direct_principal or {}).get("kind") == "direct_session"
       and (direct_principal or {}).get("bound_task_id") == task_id
       and (direct_principal or {}).get("assignment_project") == P
       and (direct_principal or {}).get("project") == "*"
       and (direct_principal or {}).get("environment_operator") is True
       and set((direct_principal or {}).get("scopes") or [])
       == set(MCP_OPERATOR_SCOPES)
       and set((direct_principal or {}).get("scopes") or [])
       == set((operator_principal or {}).get("scopes") or [])
       and "write:bug_intake" in set((direct_principal or {}).get("scopes") or []),
       "the direct CLI bearer receives the desktop environment-operator aura")
    with _conn(P) as c:
        c.execute(
            "UPDATE direct_session_tokens SET expires_at=0 "
            "WHERE runner_session_id=?", (direct_runner_id,))
    ok(auth.principal_for_token_any_project(issued.get("token") or "") is None,
       "an expired direct-session bearer is rejected before runner renewal")

    def claim_task(task_id="", agent_id="", project=P):
        return {"task_id": task_id, "agent_id": agent_id, "project": project}

    def verify_offline_completion(task_id="", evidence="", project=P):
        return {"task_id": task_id, "evidence": evidence, "project": project}

    def parity_write(tool_name):
        def write(project=P):
            return {"tool": tool_name, "project": project}
        write.__name__ = tool_name
        return write

    guarded_claim = MCPAuthorizationGuard().wrap(claim_task)

    def submit_bug(source_task="", project=P):
        return {"source_task": source_task, "project": project}

    guarded_submit_bug = MCPAuthorizationGuard().wrap(submit_bug)
    with transport_principal_scope(direct_principal):
        accepted = guarded_claim(task_id=task_id, agent_id=f"codex/{task_id}", project=P)
        accepted_offline_done = MCPAuthorizationGuard().wrap(
            verify_offline_completion)(
                task_id=task_id, evidence="task-bound production proof", project=P)
        parity_writes = {
            tool_name: MCPAuthorizationGuard().wrap(parity_write(tool_name))(project=P)
            for tool_name in (
                "submit_bug", "abandon_claim", "verify_ci",
                "claim_external_effect", "mark_external_effect_issued",
                "verify_external_effect", "fail_external_effect",
                "record_publication_evidence", "archive_work_session_workspace",
            )
        }
        accepted_bug = guarded_submit_bug(source_task=task_id, project=P)
        crossed_task = guarded_claim(
            task_id="UI-999", agent_id="codex/another-agent", project=P)
        crossed_offline_task = MCPAuthorizationGuard().wrap(
            verify_offline_completion)(
                task_id="UI-999", evidence="operator proof", project=P)
        crossed_bug_source = guarded_submit_bug(source_task="UI-999", project=P)
        crossed_project = guarded_claim(
            task_id="MAX-999", agent_id="codex/another-agent", project="maxwell")
    ok(accepted.get("task_id") == task_id
       and accepted_offline_done.get("task_id") == task_id
       and crossed_task.get("task_id") == "UI-999"
       and crossed_task.get("agent_id") == "codex/another-agent"
       and crossed_offline_task.get("task_id") == "UI-999"
       and accepted_bug.get("source_task") == task_id
       and crossed_bug_source.get("source_task") == "UI-999"
       and crossed_project.get("project") == "maxwell",
       "direct CLI can operate across tasks, agents, and projects like desktop MCP")
    ok(set(parity_writes) == {
           "submit_bug", "abandon_claim", "verify_ci",
           "claim_external_effect", "mark_external_effect_issued",
           "verify_external_effect", "fail_external_effect",
           "record_publication_evidence", "archive_work_session_workspace",
       } and all(row.get("project") == P for row in parity_writes.values()),
       "direct CLI has no transport-only write deny for bug, CI, effects, publication, or cleanup")
    runner_record = {
        "runner_session_id": direct_runner_id,
        "host_id": MAC,
        "agent_id": f"codex/{task_id}",
        "runtime": "codex",
        "task_id": task_id,
        "status": "running",
        "cwd": str(ROOT),
        "control": {
            "tier": "T3", "runner_kill": True, "managed_process": True,
            "runner_open": True, "runner_inject": True, "runner_logs": True,
        },
        "metadata": {
            "wake_id": direct_wake["wake_id"],
            "direct_assignment": True,
            "assignment_schema": "switchboard.direct_cli_assignment.v1",
            "pty": True,
            "stream_bind": "127.0.0.1",
            "stream_port": 45678,
        },
    }
    registered = store.upsert_runner_session(
        runner_record, principal_id="principal/ui45-mac", actor=MAC, project=P)
    renewed_principal = auth.principal_for_token_any_project(issued.get("token") or "")
    ok((renewed_principal or {}).get("bound_runner_session_id") == direct_runner_id,
       "a healthy direct-runner heartbeat renews its exact expired MCP token")
    watch = store.resolve_runner_watch(task_id, project=P)
    ok(not registered.get("error")
       and (registered.get("metadata") or {}).get("native_host_execution") is True
       and watch.get("watchable") is True
       and watch.get("binding_mode") == "direct_assignment",
       "the live direct runner is browser-watchable without a task claim or Work Session")
    ticket = runner_pty.mint_ticket_for_session(
        runner_session_id=direct_runner_id, project=P,
        scopes=["watch", "input"], actor=OWNER)
    ok(ticket.get("minted") is True
       and str(ticket.get("relay_url") or "").startswith(
           f"wss://plan.example/ixp/v1/runner_sessions/{direct_runner_id}/pty?ticket="),
       "the direct native PTY receives the same browser-safe relay used by the POC")
    completed = store.complete_wake(
        direct_wake["wake_id"], runner_session_id=direct_runner_id,
        agent_id=f"codex/{task_id}",
        result={"started": True, "reason": "direct_cli_started",
                "task_id": task_id, "host_id": MAC},
        principal_id="principal/ui45-mac", actor=MAC, project=P)
    ok(completed.get("status") == "completed"
       and completed.get("runner_session_id") == direct_runner_id,
       "the daemon acknowledges the assignment only after the live runner exists")
    terminal_record = {**runner_record, "status": "completed"}
    store.upsert_runner_session(
        terminal_record, principal_id="principal/ui45-mac", actor=MAC, project=P)
    ok(auth.principal_for_token_any_project(issued.get("token") or "") is None,
       "a terminal direct runner revokes its task-bound MCP token")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nUI-45 personal Mac dispatch: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
