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
    ok(issued.get("issued") is True
       and (direct_principal or {}).get("kind") == "direct_session"
       and (direct_principal or {}).get("bound_task_id") == task_id
       and "write:tasks" in set((direct_principal or {}).get("scopes") or []),
       "the boot exchanges host identity for a short-lived task-bound MCP bearer")

    def claim_task(task_id="", agent_id="", project=P):
        return {"task_id": task_id, "agent_id": agent_id, "project": project}

    guarded_claim = MCPAuthorizationGuard().wrap(claim_task)
    with transport_principal_scope(direct_principal):
        accepted = guarded_claim(task_id=task_id, agent_id=f"codex/{task_id}", project=P)
        try:
            guarded_claim(task_id="UI-999", agent_id=f"codex/{task_id}", project=P)
            crossed_task = True
        except ValueError:
            crossed_task = False
    ok(accepted.get("task_id") == task_id and crossed_task is False,
       "direct CLI can run the assigned Switchboard workflow but cannot cross tasks")
    registered = store.upsert_runner_session({
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
        },
    }, principal_id="principal/ui45-mac", actor=MAC, project=P)
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
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nUI-45 personal Mac dispatch: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
