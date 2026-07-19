#!/usr/bin/env python3
"""UI-48: an ended In Review runner gets one preserved replacement review run."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="ui48-resume-review-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import dispatch  # noqa: E402
import store  # noqa: E402
from adapters.direct_codex_session import _codex_command  # noqa: E402
from db.connection import _conn  # noqa: E402


P = "switchboard"
OWNER = "user/ui48-owner"
HOST = "host/ui48-mac"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def host_inventory():
    local_auth = {
        "available": True, "runtime": "codex", "auth_mode": "chatgpt_personal",
        "account_fingerprint": "acct-ui48", "credential_values_redacted": True,
        "provider_credential_exported": False,
    }
    return {
        "host_id": HOST, "hostname": "ui48-mac", "repo_root": str(ROOT),
        "runtimes": [{
            "runtime": "codex", "lanes": [], "capabilities": ["github", "tests"],
            "policy": {"allow_work": True, "allow_global_claim": False,
                       "lane_mode": "all_project_lanes"},
            "local_auth": local_auth,
        }],
        "limits": {"max_sessions": 8},
        "capacity": {"active_sessions": 0, "local_auth": local_auth},
        "heartbeat_ttl_s": 60,
    }


try:
    store.init_db(P)
    store.register_host(
        host_inventory(), principal_id="principal/ui48-mac", actor=HOST, project=P)
    now = time.time()
    with _conn(P) as connection:
        connection.execute(
            "INSERT INTO agent_host_enrollments("
            "enrollment_id,project_id,requested_host_id,host_id,owner_user_id,"
            "tenant_allowlist_json,project_allowlist_json,provider_allowlist_json,"
            "bootstrap_hash,bootstrap_expires_at,bootstrap_consumed_at,principal_id,"
            "public_key_fingerprint,identity_generation,package_version,platform,"
            "hostname,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("hostenroll-ui48", P, HOST, HOST, OWNER, "[]", json.dumps([P]),
             json.dumps(["openai-codex"]), "ui48-bootstrap", now + 3600, now,
             "principal/ui48-mac", "sha256:" + "b" * 64, 1, "0.2.20", "macos",
             "ui48-mac", "active", now, now),
        )

    task = store.create_task({
        "workstream_id": "UI", "title": "Resume dead review", "status": "In Review",
        "description": "Review the existing PR and merge if green.",
    }, actor="ui48-test", project=P)
    task_id = task["task_id"]
    store.upsert_runner_session({
        "runner_session_id": "run-ui48-dead", "host_id": HOST,
        "agent_id": f"codex/{task_id}", "runtime": "codex", "task_id": task_id,
        "status": "failed", "cwd": str(ROOT),
        "metadata": {"wake_id": "wake-old", "direct_assignment": True,
                     "assignment_schema": "switchboard.direct_cli_assignment.v1"},
        "last_snapshot": {"head_sha": "a" * 40, "pr_url": "https://example/pr/48"},
    }, principal_id="principal/ui48-mac", actor=HOST, project=P)
    with _conn(P) as connection:
        connection.execute(
            "UPDATE runner_sessions SET heartbeat_at=? WHERE runner_session_id=?",
            (now - 600, "run-ui48-dead"),
        )

    resumed = dispatch.resume_review(
        task_id, actor=OWNER, principal_id=OWNER, project=P)
    ok(resumed.get("resumed") is True
       and resumed.get("continuation_mode") == "replacement_handoff",
       "an ended In Review task starts a replacement with a generated handoff")
    wakes = store.list_wake_intents(task_id=task_id, project=P)
    assignment = (wakes[-1].get("policy") or {}).get("assignment") or {}
    continuation = assignment.get("continuation") or {}
    ok(continuation.get("previous_runner_session_id") == "run-ui48-dead"
       and (continuation.get("handoff") or {}).get("workflow_status") == "In Review"
       and "Review and merge" in assignment.get("prompt", ""),
       "the exact dead run and review instruction are carried in the new assignment")
    handoff_command = _codex_command(
        assignment, Path("/tmp/ui48-handoff"), "codex", "https://plan.example/mcp")
    ok("Replacement review handoff:" in handoff_command[-1]
       and "run-ui48-dead" not in handoff_command[-1]
       and "In Review" in handoff_command[-1],
       "a fresh reviewer receives the generated review handoff in its opening prompt")
    ok(store.get_runner_session("run-ui48-dead", project=P) is not None
       and store.get_task(task_id, project=P).get("status") == "In Review",
       "the dead runner remains immutable history and workflow stays In Review")
    claimed_review = store.claim_task(
        task_id, f"codex/{task_id}", actor=HOST, project=P,
        idem_key="ui48-review-continuation-claim")
    ok(claimed_review.get("claimed") is True
       and claimed_review.get("task", {}).get("status") == "In Review"
       and claimed_review.get("dispatch_reason", {}).get(
           "workflow_status_preserved") == "In Review",
       "the replacement reviewer acquires its lease without leaving In Review")
    unrelated_review = store.create_task({
        "workstream_id": "UI", "title": "Unassigned review", "status": "In Review",
    }, actor="ui48-test", project=P)
    unauthorized_review_claim = store.claim_task(
        unrelated_review["task_id"], f"codex/{unrelated_review['task_id']}",
        actor=HOST, project=P)
    ok(unauthorized_review_claim.get("claimed") is False
       and unauthorized_review_claim.get("reason") == "status_not_ready",
       "an ordinary agent still cannot claim an In Review task without the exact continuation wake")
    replay = dispatch.resume_review(
        task_id, actor=OWNER, principal_id=OWNER, project=P)
    ok(replay.get("wake_id") == resumed.get("wake_id")
       and len(store.list_wake_intents(task_id=task_id, project=P)) == 1,
       "repeat Resume review collapses onto the same replacement wake")

    resumable = store.create_task({
        "workstream_id": "UI", "title": "Resume provider conversation",
        "status": "In Review",
    }, actor="ui48-test", project=P)
    resumable_id = resumable["task_id"]
    store.upsert_runner_session({
        "runner_session_id": "run-ui48-resumable", "host_id": HOST,
        "agent_id": f"codex/{resumable_id}", "runtime": "codex",
        "task_id": resumable_id, "status": "failed", "cwd": str(ROOT),
        "metadata": {"codex_conversation_id": "019f-ui48-conversation"},
    }, principal_id="principal/ui48-mac", actor=HOST, project=P)
    resumed_provider = dispatch.resume_review(
        resumable_id, actor=OWNER, principal_id=OWNER, project=P)
    provider_wake = store.list_wake_intents(task_id=resumable_id, project=P)[-1]
    provider_assignment = (provider_wake.get("policy") or {}).get("assignment") or {}
    command = _codex_command(
        provider_assignment, Path("/tmp/ui48-workspace"), "codex", "https://plan.example/mcp")
    ok(resumed_provider.get("continuation_mode") == "resume_conversation"
       and command[1:3] == ["resume", "019f-ui48-conversation"],
       "a durable Codex conversation id uses native codex resume in the replacement process")

    live_task = store.create_task({
        "workstream_id": "UI", "title": "Already live review", "status": "In Review",
    }, actor="ui48-test", project=P)
    live_id = live_task["task_id"]
    store.upsert_runner_session({
        "runner_session_id": "run-ui48-live", "host_id": HOST,
        "agent_id": f"codex/{live_id}", "runtime": "codex", "task_id": live_id,
        "status": "running", "cwd": str(ROOT),
        "metadata": {"wake_id": "wake-live", "direct_assignment": True,
                     "native_host_execution": True,
                     "assignment_schema": "switchboard.direct_cli_assignment.v1"},
    }, principal_id="principal/ui48-mac", actor=HOST, project=P)
    refused = dispatch.resume_review(live_id, actor=OWNER, principal_id=OWNER, project=P)
    ok(refused.get("error") == "review_runner_already_live",
       "Resume review refuses to create a parallel runner when review is already live")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nUI-48 stale review continuation: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
