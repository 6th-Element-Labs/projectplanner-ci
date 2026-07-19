#!/usr/bin/env python3
"""UI-40: live Mac Watch/Chat and Work Session lifecycle regressions."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="ui40-mac-relay-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_SWITCHBOARD_PUBLIC_BASE"] = "https://plan.example"
os.environ["PM_RUNNER_PTY_RELAY_SECRET"] = "ui40-test-secret"

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.application.commands import runner_pty  # noqa: E402


P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def create_bound_attempt(task_id: str, suffix: str, *, principal: str):
    agent_id = f"codex/{task_id}"
    session = store.create_work_session({
        "task_id": task_id,
        "agent_id": agent_id,
        "runtime": "codex",
        "repo_role": "canonical",
        "branch": f"codex/{task_id}-{suffix}",
        "upstream": "origin/master",
        "base_sha": "a" * 40,
        "head_sha": "a" * 40,
        "worktree_path": str(ROOT),
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": "clean",
        "conflict_marker_count": 0,
        "policy_profile": "code_strict",
        "hygiene": {"repo_preflight": {"ok": True, "verdict": "pass", "findings": []}},
    }, actor="ui40-test", principal_id=principal, project=P)["work_session"]
    claim = store.claim_task(
        task_id, agent_id, principal_id=principal, actor="ui40-test",
        work_session_id=session["work_session_id"],
        session_policy_profile="code_strict", require_work_session=True, project=P,
    )
    return agent_id, session, claim


try:
    store.init_db(P)

    # Historical expired/claimless attempts stay queryable but cannot override
    # the current claim-bound Work Session's task-level health.
    task = store.create_task({
        "workstream_id": "UI", "title": "UI-40 current health",
        "status": "Not Started", "ui_impact": "yes",
    }, actor="ui40-test", project=P)
    tid = task["task_id"]
    for n in range(3):
        old = store.create_work_session({
            "task_id": tid, "agent_id": f"codex/{tid}", "runtime": "codex",
            "repo_role": "canonical", "branch": f"codex/{tid}-old-{n}",
            "worktree_path": str(ROOT), "storage_mode": "worktree",
            "status": "expired", "dirty_status": "clean",
            "policy_profile": "code_strict",
        }, actor="ui40-test", project=P)
        ok(bool(old.get("work_session")), f"historical expired attempt {n + 1} is recorded")
    agent_id, current_ws, current_claim = create_bound_attempt(
        tid, "current", principal="principal/ui40-current")
    detail = store.get_task(tid, project=P)
    health = detail["session_health"]
    ok(current_claim.get("claimed") is True, "current attempt owns the active claim")
    ok(health["current_session_count"] == 1 and health["active_session_count"] == 1,
       "task health selects only the current claim-bound Work Session")
    ok(not any(f.get("code") == "expired_work_session" for f in health["findings"]),
       "historical expired attempts do not duplicate the current task blocker")

    # A fully task/claim/host/wake/Work-Session-bound runner may omit the optional
    # tenant/user fields; ticket mint supplies only those documented safe defaults.
    host_id = "host/ui40-mac"
    runner_id = "run_ui40_watch"
    wake_id = "wake-ui40-watch"
    store.register_host({
        "host_id": host_id,
        "runtimes": [{"runtime": "codex", "lanes": ["UI"]}],
        "limits": {"max_sessions": 8},
        "heartbeat_ttl_s": 60,
    }, principal_id="principal/ui40-host", actor=host_id, project=P)
    runner = store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": host_id,
        "agent_id": agent_id,
        "runtime": "codex",
        "task_id": tid,
        "claim_id": current_claim["claim_id"],
        "status": "running",
        "control": {
            "managed_process": True,
            "runner_open": True,
            "runner_inject": True,
        },
        "metadata": {
            "wake_id": wake_id,
            "work_session_id": current_ws["work_session_id"],
            "credential_admission_phase": "claim_bound",
            "execution_connection_id": "execconn-ui40-watch",
            "source_sha": "a" * 40,
        },
    }, principal_id="principal/ui40-host", actor=host_id, project=P)
    ticket = runner_pty.mint_ticket_for_session(
        runner_session_id=runner_id, project=P,
        scopes=["watch", "input"], actor="user/ui40-operator",
    )
    ok(not runner.get("error") and ticket.get("minted") is True,
       "bound Mac runner mints a browser-safe PTY ticket")
    ok(ticket.get("binding", {}).get("tenant_id") == "tenant/default"
       and ticket.get("binding", {}).get("user_id") == "user/ui40-operator",
       "ticket fills absent optional tenant/user identity defaults")
    ok(str(ticket.get("relay_url") or "").startswith(
        f"wss://plan.example/ixp/v1/runner_sessions/{runner_id}/pty?ticket="),
       "ticket exposes the public Switchboard WebSocket relay, never host loopback")
    control = store.request_runner_control(
        runner_id, "open", actor="ui40-operator",
        principal_id="user/ui40-operator", project=P)
    claimed_control = store.claim_runner_control_request(
        host_id, control["request_id"], actor=host_id, project=P)
    relay_options = ((claimed_control.get("request") or {}).get("options") or {}).get(
        "server_relay") or {}
    persisted_control = store.list_runner_control_requests(
        runner_session_id=runner_id, project=P)[-1]
    ok(str(relay_options.get("host_url") or "").startswith(
       f"wss://plan.example/ixp/v1/runner_sessions/{runner_id}/pty/host?ticket=")
       and str(relay_options.get("browser_url") or "").startswith(
       f"wss://plan.example/ixp/v1/runner_sessions/{runner_id}/pty?ticket="),
       "server mints distinct host and browser relay capabilities at control claim")
    ok("server_relay" not in (persisted_control.get("options") or {}),
       "server relay capabilities are delivered ephemerally and never persisted")

    repeated_open = store.request_runner_control(
        runner_id, "open", actor="ui40-operator",
        principal_id="user/ui40-operator", project=P)
    repeated_inject_1 = store.request_runner_control(
        runner_id, "inject",
        options={"task_id": tid, "text": "same text"},
        actor="ui40-operator", principal_id="user/ui40-operator", project=P)
    repeated_inject_2 = store.request_runner_control(
        runner_id, "inject",
        options={"task_id": tid, "text": "same text"},
        actor="ui40-operator", principal_id="user/ui40-operator", project=P)
    ok(repeated_open.get("requested") is True
       and repeated_open.get("request_id") != control.get("request_id"),
       "a later Watch open is a fresh recovery request, not a permanently deduped no-op")
    ok(repeated_inject_1.get("requested") is True
       and repeated_inject_2.get("requested") is True
       and repeated_inject_1.get("request_id") != repeated_inject_2.get("request_id"),
       "same-text durable chat retries cannot inherit an earlier failed external effect")

    # A terminal failed runner is the durable fallback for hosts that die before
    # their child can expire its Work Session explicitly.
    failed_task = store.create_task({
        "workstream_id": "UI", "title": "UI-40 terminal runner cleanup",
        "status": "Not Started", "ui_impact": "no",
    }, actor="ui40-test", project=P)
    failed_tid = failed_task["task_id"]
    failed_agent, failed_ws, failed_claim = create_bound_attempt(
        failed_tid, "failed", principal="principal/ui40-failed")
    failed_runner_id = "run_ui40_failed"
    failed_meta = {
        "wake_id": "wake-ui40-failed",
        "work_session_id": failed_ws["work_session_id"],
        "credential_admission_phase": "claim_bound",
        "auth_lane": "codex_host_local",
    }
    base_runner = {
        "runner_session_id": failed_runner_id,
        "host_id": host_id,
        "agent_id": failed_agent,
        "runtime": "codex",
        "task_id": failed_tid,
        "claim_id": failed_claim["claim_id"],
        "metadata": failed_meta,
    }
    now = time.time()
    with _conn(P) as c:
        c.execute(
            "INSERT INTO wake_intents(wake_id,source,reason,selector_json,policy_json,"
            "status,requested_at,claimed_at,claimed_by_host,result_json,placement_json,task_id) "
            "VALUES (?,?,?,?,?,'claimed',?,?,?,'{}','{}',?)",
            (failed_meta["wake_id"], "ui40-test", "autopilot",
             json.dumps({"runtime": "codex", "agent_id": failed_agent,
                         "task_id": failed_tid}),
             json.dumps({"require_runner_bind": True, "mode": "agent_host"}),
             now, now, host_id, failed_tid),
        )
    store.upsert_runner_session(
        {**base_runner, "status": "running"},
        principal_id="principal/ui40-host", actor=host_id, project=P)
    launched = store.complete_wake(
        failed_meta["wake_id"], runner_session_id=failed_runner_id,
        agent_id=failed_agent,
        result={"started": True, "task_id": failed_tid},
        principal_id="principal/ui40-host", actor=host_id, project=P)
    ok(launched.get("status") == "completed",
       "host records the native CLI launch before child post-processing")
    store.upsert_runner_session(
        {**base_runner, "status": "failed"},
        principal_id="principal/ui40-host", actor=host_id, project=P)
    closed = store.get_work_session(failed_ws["work_session_id"], project=P)
    ok(closed.get("status") == "expired",
       "terminal failed runner expires its exact bound Work Session")
    exact_binding = {
        "wake_id": failed_meta["wake_id"], "host_id": host_id,
        "runner_session_id": failed_runner_id, "task_id": failed_tid,
        "agent_id": failed_agent,
    }
    authority = store.check_agent_host_bootstrap_authority(
        exact_binding, principal_id="principal/ui40-host", project=P,
        action="complete_wake")
    ok(authority.get("allowed") is True,
       "exact terminal child retains authority to correct its launch receipt")
    unflagged = store.complete_wake(
        failed_meta["wake_id"], runner_session_id=failed_runner_id,
        agent_id=failed_agent,
        result={"started": False, "reason": "executed_tests_failed",
                "task_id": failed_tid},
        principal_id="principal/ui40-host", actor=host_id, project=P)
    ok("completed_wake_rewrite_denied" in unflagged.get("reason_codes", []),
       "an unflagged completed-wake rewrite remains denied")
    recovered = store.complete_wake(
        failed_meta["wake_id"], runner_session_id=failed_runner_id,
        agent_id=failed_agent,
        result={"started": False, "reason": "executed_tests_failed",
                "task_id": failed_tid,
                "recoverable_post_execution_failure": True},
        principal_id="principal/ui40-host", actor=host_id, project=P)
    ok(recovered.get("status") == "failed"
       and recovered.get("recovered_post_execution_failure") is True,
       "exact terminal child atomically recovers the completed wake to failed")

    runner_js = (Path(ROOT) / "static/js/runner-session.js").read_text(encoding="utf-8")
    app_js = (Path(ROOT) / "static/app.js").read_text(encoding="utf-8")
    worker_py = (Path(ROOT) / "adapters/codex_local_worker.py").read_text(
        encoding="utf-8")
    ok("_runnerPtyApiError" in runner_js and "JSON.stringify(value)" in runner_js,
       "Watch/Chat renders structured API errors instead of [object Object]")
    ok("const latest = new Map()" in app_js
       and "['active', 'proposed']" in app_js,
       "fleet dock selects one newest live attempt per task/agent")
    ok(worker_py.count('"recoverable_post_execution_failure": True') >= 2,
       "personal exact-host failures retain server recovery without rewriting generic launch wakes")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nUI-40 Mac relay/session health: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
