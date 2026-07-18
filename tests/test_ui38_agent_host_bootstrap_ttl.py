#!/usr/bin/env python3
"""UI-38: slow native startup keeps exact bootstrap authority without downgrade."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT

TMP = Path(tempfile.mkdtemp(prefix="ui38-bootstrap-ttl-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import store  # noqa: E402
from adapters import agent_host  # noqa: E402
from adapters import switchboard_core as sb  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.api.routers.agents import create_router  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    store.init_db(P)
    task = store.create_task(
        {"workstream_id": "UI", "title": "UI-38 slow bootstrap proof"},
        actor="ui38-test", project=P)
    task_id = task["task_id"]
    host_id = "host/ui38-mac"
    principal_id = "principal/ui38-host"
    wake_id = "wake-ui38"
    runner_id = "run_ui38"
    agent_id = f"codex/{task_id}"
    now = time.time()
    selector = {"runtime": "codex", "agent_id": agent_id, "task_id": task_id}
    policy = {"require_runner_bind": True, "mode": "agent_host"}
    with _conn(P) as c:
        c.execute(
            "INSERT INTO principals(id,kind,display_name,project,scopes,token_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (principal_id, "agent_host", host_id, P,
             json.dumps(["read", "write:agent_host"]), "ui38-token-hash", now),
        )
        c.execute(
            "INSERT INTO agent_host_enrollments("
            "enrollment_id,project_id,requested_host_id,host_id,owner_user_id,"
            "tenant_allowlist_json,project_allowlist_json,provider_allowlist_json,"
            "execution_policy_json,bootstrap_hash,bootstrap_expires_at,"
            "bootstrap_consumed_at,principal_id,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("enroll-ui38", P, host_id, host_id, "user/ui38", "[]",
             json.dumps([P]), "[]", "{}", "ui38-bootstrap-hash", now + 3600,
             now, principal_id, "active", now, now),
        )
        c.execute(
            "INSERT INTO agent_hosts(host_id,hostname,agent_host_version,repo_root,"
            "runtimes_json,limits_json,capacity_json,principal_id,registered_at,"
            "heartbeat_at,heartbeat_ttl_s,status,last_error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (host_id, "ui38-mac", "0.2.10", str(ROOT), '["codex"]',
             '{"max_sessions":8}', '{}', principal_id, now, now, 60, "online", ""),
        )
        c.execute(
            "INSERT INTO wake_intents(wake_id,source,reason,selector_json,policy_json,"
            "status,requested_at,claimed_at,claimed_by_host,result_json,placement_json,task_id) "
            "VALUES (?,?,?,?,?,'claimed',?,?,?,'{}','{}',?)",
            (wake_id, "ui38-test", "autopilot", json.dumps(selector),
             json.dumps(policy), now, now, host_id, task_id),
        )
        c.execute(
            "INSERT INTO runner_sessions(runner_session_id,host_id,agent_id,runtime,task_id,"
            "claim_id,status,control_json,metadata_json,last_snapshot_json,principal_id,"
            "started_at,heartbeat_at,heartbeat_ttl_s,updated_at) "
            "VALUES (?,?,?,?,?,NULL,'starting','{}',?,'{}',?,?,?,?,?)",
            (runner_id, host_id, agent_id, "codex", task_id,
             json.dumps({"credential_admission_phase": "preclaim", "wake_id": wake_id,
                         "native_host_execution": True}),
             principal_id, now - 120, now - 55, 60, now - 55),
        )

    binding = {
        "wake_id": wake_id,
        "host_id": host_id,
        "runner_session_id": runner_id,
        "task_id": task_id,
        "agent_id": agent_id,
    }
    for action in ("register_agent", "heartbeat_agent", "create_work_session"):
        allowed = store.check_agent_host_bootstrap_authority(
            binding, principal_id=principal_id, project=P, action=action)
        ok(allowed.get("allowed") is True and allowed.get("runtime") == "codex",
           f"exact fresh preclaim authorizes narrow {action}")
    denied = store.check_agent_host_bootstrap_authority(
        {**binding, "task_id": "UI-9999"}, principal_id=principal_id,
        project=P, action="register_agent")
    ok(denied.get("allowed") is False,
       "cross-task narrow agent registration is denied")

    principal = {
        "id": principal_id, "kind": "agent_host", "display_name": host_id,
        "scopes": ["read", "write:agent_host"],
        "effective_scopes": ["read", "write:agent_host"],
    }

    def resolve_principal(_request, _project, _scopes, **_kwargs):
        return principal

    app = FastAPI()
    app.include_router(create_router(
        resolve_project=lambda project: project,
        resolve_principal=resolve_principal,
        resolve_body_project=lambda body: str(body.get("project") or ""),
        control_plane_http=lambda result: result,
    ))
    client = TestClient(app)
    exact_body = {
        "project": P, "agent_id": agent_id, "task_id": task_id,
        "runtime": "codex", "agent_host_bootstrap_binding": binding,
    }
    registered = client.post("/ixp/v1/register_agent", json=exact_body)
    heartbeated = client.post("/ixp/v1/heartbeat", json={
        "project": P, "agent_id": agent_id, "task_id": task_id,
        "agent_host_bootstrap_binding": binding,
    })
    wrong = client.post("/ixp/v1/register_agent", json={
        **exact_body, "task_id": "UI-9999",
    })
    wrong_runtime = client.post("/ixp/v1/register_agent", json={
        **exact_body, "runtime": "claude-code",
    })
    ok(registered.status_code == 200 and heartbeated.status_code == 200,
       "narrow host register_agent and heartbeat pass through the exact REST gate")
    ok(wrong.status_code == 403 and wrong_runtime.status_code == 403,
       "REST gate rejects task or runtime bodies that cross the durable tuple")

    before = store.get_runner_session(runner_id, project=P)["heartbeat_at"]
    renewed = store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": host_id,
        "agent_id": agent_id,
        "runtime": "codex",
        "task_id": task_id,
        "status": "starting",
        "metadata": {
            "credential_admission_phase": "preclaim",
            "wake_id": wake_id,
            "preclaim_renewal": True,
        },
    }, principal_id=principal_id, actor=host_id, project=P)
    ok(renewed.get("heartbeat_at", 0) > before and not renewed.get("stale"),
       "exact renewal extends authority past the original preclaim TTL")

    with _conn(P) as c:
        c.execute(
            "UPDATE runner_sessions SET claim_id=?,status='running',metadata_json=? "
            "WHERE runner_session_id=?",
            ("taskclaim-ui38", json.dumps({
                "credential_admission_phase": "claim_bound",
                "wake_id": wake_id,
                "work_session_id": "worksession-ui38",
                "native_host_execution": True,
            }), runner_id),
        )
    raced = store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": host_id, "agent_id": agent_id,
        "runtime": "codex", "task_id": task_id, "status": "starting",
        "metadata": {"credential_admission_phase": "preclaim", "wake_id": wake_id,
                     "preclaim_renewal": True},
    }, principal_id=principal_id, actor=host_id, project=P)
    ok(raced.get("claim_id") == "taskclaim-ui38"
       and raced.get("status") == "running"
       and raced.get("metadata", {}).get("credential_admission_phase") == "claim_bound",
       "late renewal returns the stronger claim-bound row without overwriting it")
    bound_heartbeat = store.check_agent_host_bootstrap_authority(
        binding, principal_id=principal_id, project=P, action="heartbeat_agent")
    ok(bound_heartbeat.get("allowed") is True,
       "exact claim-bound worker heartbeat remains authorized after startup")
    cross_renewal = store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": host_id, "agent_id": agent_id,
        "runtime": "codex", "task_id": "UI-9999", "status": "starting",
        "metadata": {"credential_admission_phase": "preclaim", "wake_id": wake_id,
                     "preclaim_renewal": True},
    }, principal_id=principal_id, actor=host_id, project=P)
    ok(cross_renewal.get("error_code") == "preclaim_renewal_denied",
       "cross-tuple renewal fails closed")

    wake = {"wake_id": wake_id, "task_id": task_id, "policy": policy,
            "selector": selector}
    inventory = {"host_id": host_id, "repo_root": str(ROOT)}
    preclaim_row = {
        "runner_session_id": runner_id, "host_id": host_id, "agent_id": agent_id,
        "runtime": "codex", "task_id": task_id, "claim_id": None,
        "status": "starting", "stale": False,
        "metadata": {"credential_admission_phase": "preclaim", "wake_id": wake_id},
    }
    bound_row = {
        **preclaim_row, "claim_id": "taskclaim-ui38", "status": "running",
        "metadata": {"credential_admission_phase": "claim_bound", "wake_id": wake_id,
                     "work_session_id": "worksession-ui38"},
    }
    clock = {"now": 0.0, "polls": 0}
    renewals = []
    real_try = agent_host._try
    real_register = agent_host._register_preclaim_runner
    old_interval = os.environ.get("PM_AGENT_HOST_PRECLAIM_RENEW_INTERVAL_S")
    os.environ["PM_AGENT_HOST_PRECLAIM_RENEW_INTERVAL_S"] = "10"

    def fake_try(_method, _path, _body=None, _timeout=None):
        clock["polls"] += 1
        return {"sessions": [bound_row if clock["now"] > 70 else preclaim_row]}

    def fake_sleep(seconds):
        clock["now"] += max(10.0, seconds)

    def fake_register(_wake, _inventory, _runner_id, *, renewal=False):
        renewals.append((clock["now"], renewal))
        return preclaim_row

    agent_host._try = fake_try
    agent_host._register_preclaim_runner = fake_register
    try:
        waited = agent_host.wait_for_runner_binding(
            wake, inventory, runner_id, timeout_s=100,
            sleep=fake_sleep, monotonic=lambda: clock["now"])
    finally:
        agent_host._try = real_try
        agent_host._register_preclaim_runner = real_register
        if old_interval is None:
            os.environ.pop("PM_AGENT_HOST_PRECLAIM_RENEW_INTERVAL_S", None)
        else:
            os.environ["PM_AGENT_HOST_PRECLAIM_RENEW_INTERVAL_S"] = old_interval
    ok(waited.get("bound") is True and len(renewals) >= 6
       and all(is_renewal for _at, is_renewal in renewals),
       "bind finalizer renews throughout startup longer than one 60s TTL")

    # The default 90-second readiness SLO must not kill a healthy exact-task boot
    # while a slow canonical worktree is still being created. A live local runner
    # plus the exact renewable preclaim extends the wait, with a separate hard cap.
    clock = {"now": 0.0}
    renewals = []

    def delayed_bind_try(_method, _path, _body=None, _timeout=None):
        return {"sessions": [bound_row if clock["now"] > 120 else preclaim_row]}

    def delayed_bind_sleep(seconds):
        clock["now"] += max(10.0, seconds)

    agent_host._try = delayed_bind_try
    agent_host._register_preclaim_runner = fake_register
    try:
        waited = agent_host.wait_for_runner_binding(
            wake, inventory, runner_id,
            max_timeout_s=180,
            runner_alive=lambda requested_id: requested_id == runner_id,
            sleep=delayed_bind_sleep, monotonic=lambda: clock["now"])
    finally:
        agent_host._try = real_try
        agent_host._register_preclaim_runner = real_register
    ok(waited.get("bound") is True and clock["now"] > 90,
       "live exact preclaim extends bind wait beyond the readiness SLO")

    clock = {"now": 0.0}
    agent_host._try = delayed_bind_try
    try:
        waited = agent_host.wait_for_runner_binding(
            wake, inventory, runner_id,
            max_timeout_s=180,
            runner_alive=lambda _requested_id: False,
            sleep=delayed_bind_sleep, monotonic=lambda: clock["now"])
    finally:
        agent_host._try = real_try
    ok(waited.get("bound") is False and clock["now"] <= 100,
       "dead local runner still fails closed at the readiness deadline")

    # If Switchboard briefly requeues a claimed wake during a slow boot, the next
    # host tick must reclaim and reuse the deterministic local runner instead of
    # calling supervisor start again and failing the live first process.
    finalizer_key = agent_host._bound_finalizer_key(wake, inventory, runner_id)
    real_supervisor_action = agent_host.supervisor_action
    with agent_host._BOUND_FINALIZERS_LOCK:
        agent_host._BOUND_FINALIZERS[finalizer_key] = object()
    agent_host.supervisor_action = lambda action, requested_id, options=None: {
        "runner_session_id": requested_id,
        "alive": True,
        "pid": 4242,
        "cwd": str(ROOT),
        "task_id": task_id,
        "control": {"managed_process": True},
    }
    try:
        reused = agent_host._reuse_inflight_bound_runner(
            wake, inventory, runner_id, preclaim_registration=preclaim_row)
    finally:
        agent_host.supervisor_action = real_supervisor_action
        with agent_host._BOUND_FINALIZERS_LOCK:
            agent_host._BOUND_FINALIZERS.pop(finalizer_key, None)
    ok(reused.get("reused_local_runner") is True
       and reused.get("reason") == "runner_binding_pending_reused"
       and reused.get("pid") == 4242
       and reused.get("binding_pending") is True,
       "a second host tick reuses the live local runner without duplicate start")

    source = Path(agent_host.__file__).read_text(encoding="utf-8")
    reuse_pos = source.index("reused = _reuse_inflight_bound_runner(")
    launch_pos = source.index("rec = (launch(", reuse_pos)
    ok(reuse_pos < launch_pos and "if reused:" in source[reuse_pos:launch_pos]
       and "continue" in source[reuse_pos:launch_pos],
       "run_once exits the wake path before launch when local boot is reused")

    old_env = {key: os.environ.get(key) for key in (
        "PM_CO_WAKE_ID", "PM_CO_HOST_ID", "PM_RUNNER_SESSION_ID",
        "PM_TASK_ID", "PM_AGENT_ID",
    )}
    os.environ.update({
        "PM_CO_WAKE_ID": wake_id, "PM_CO_HOST_ID": host_id,
        "PM_RUNNER_SESSION_ID": runner_id, "PM_TASK_ID": task_id,
        "PM_AGENT_ID": agent_id,
    })
    calls = []
    real_http = sb._http
    try:
        sb._http = lambda method, path, body=None, **kwargs: (
            calls.append((method, path, body)) or {})
        sb.handshake(P, agent_id, "codex")
        sb.heartbeat(P, agent_id)
    finally:
        sb._http = real_http
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    bootstrap_calls = [body for method, path, body in calls
                       if method == "POST" and path in {
                           "/ixp/v1/register_agent", "/ixp/v1/heartbeat"}]
    ok(len(bootstrap_calls) == 2 and all(
        body.get("agent_host_bootstrap_binding") == binding
        and body.get("task_id") == task_id for body in bootstrap_calls),
       "native child sends the exact bootstrap tuple on register and heartbeat")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nUI-38 Agent Host bootstrap TTL proof: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
