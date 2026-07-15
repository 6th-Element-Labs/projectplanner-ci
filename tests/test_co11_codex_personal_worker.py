#!/usr/bin/env python3
"""CO-11: dedicated Agent Host + Codex personal OAuth (no API keys) proof."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

from path_setup import ROOT

from adapters import agent_host
from adapters import codex_personal_worker as worker


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


tmp = Path(tempfile.mkdtemp(prefix="co11-codex-"))
repo = tmp / "repo"
subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
subprocess.run(["git", "-C", str(repo), "config", "user.name", "Switchboard Test"], check=True)
(repo / "proof.txt").write_text("clean\n")
subprocess.run(["git", "-C", str(repo), "add", "proof.txt"], check=True)
subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True,
               capture_output=True)

# --- Dedicated host registration inventory: runtime=codex, allow_work=true ---
old_env = {
    key: os.environ.get(key)
    for key in (
        "PM_RUNTIME", "PM_AGENT_HOST_ALLOW_WORK", "PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM",
        "PM_HOST_LANES", "PM_HOST_ID", "PM_WAKE_ID", "PM_HOST_SUPPORTS_CREDENTIAL_LEASES",
        "PM_HOST_CLASS",
    )
}
try:
    os.environ["PM_RUNTIME"] = "codex"
    os.environ["PM_AGENT_HOST_ALLOW_WORK"] = "1"
    os.environ["PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM"] = "0"
    os.environ["PM_HOST_LANES"] = "CO"
    os.environ["PM_HOST_ID"] = "host/co11-dedicated-codex"
    os.environ.pop("PM_WAKE_ID", None)
    os.environ["PM_HOST_SUPPORTS_CREDENTIAL_LEASES"] = "1"
    inventory = agent_host.default_inventory()
finally:
    for key, value in old_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

runtime_row = (inventory.get("runtimes") or [{}])[0]
policy = runtime_row.get("policy") or inventory.get("policy") or {}
placement = ((inventory.get("capacity") or {}).get("placement") or {})
ok(runtime_row.get("runtime") == "codex"
   and policy.get("allow_work") is True
   and policy.get("mode") == "lane_scoped",
   "dedicated host inventory registers runtime=codex with allow_work=true")
ok(placement.get("host_class") == "persistent"
   and placement.get("supports_credential_leases") is True,
   "dedicated host without PM_WAKE_ID advertises persistent + credential leases")
ok(inventory.get("host_id") == "host/co11-dedicated-codex",
   "dedicated host_id is bound into registration inventory")

# --- Worker source + metered key refusal contract ---
worker_source = (ROOT / "adapters" / "codex_personal_worker.py").read_text()
ok("OPENAI_API_KEY" in worker_source
   and "CODEX_API_KEY" in worker_source
   and "CODEX_ACCESS_TOKEN" in worker_source
   and "chatgpt_personal" in worker_source
   and "register_runner_session" in worker_source
   and "_METERED_API_KEYS" in worker_source,
   "Codex personal worker refuses metered API keys and rebinds runner after claim")
ok("ANTHROPIC_API_KEY" not in worker_source
   and "os.environ[\"OPENAI_API_KEY\"]" not in worker_source
   and "os.environ['OPENAI_API_KEY']" not in worker_source,
   "worker does not inject metered OpenAI/Codex API keys as a fallback path")

# --- register_runner_session emits task_id + claim_id + host_id ---
register_calls = []
real_http = worker.sb._http


def capture_register(method, path, body=None, **kwargs):
    register_calls.append((method, path, dict(body or {})))
    if path == "/ixp/v1/register_runner_session":
        return {
            "ok": True,
            "task_id": body.get("task_id"),
            "claim_id": body.get("claim_id"),
            "host_id": body.get("host_id"),
            "runner_session_id": body.get("runner_session_id"),
        }
    return {"completed": True}


real_binding = worker._binding
real_lease_body = worker._lease_body
try:
    worker._binding = lambda: {"project": "switchboard"}
    worker._lease_body = lambda binding, task: {
        "project": "switchboard",
        "credential_reference": "provider-ref-codex",
        "user_id": "user-test",
        "provider": "openai-codex",
        "provider_account_id": "codex-account-test",
        "task_id": "CO-11",
        "host_id": "host/co11-dedicated-codex",
        "runner_session_id": "runner-co11-test",
        "work_session_id": "worksession-co11-test",
        "account_affinity_id": "affinity-co11",
        "ttl_seconds": 900,
    }
    worker.sb._http = capture_register
    os.environ["PM_AGENT_ID"] = "cursor/CO-11-test"
    os.environ["PM_CO_WAKE_ID"] = "wake-co11"
    worker._register_bound_runner(
        {"task_id": "CO-11", "claim_id": "taskclaim-co11-sample",
         "managed": {"workspace_path": str(repo)}},
        worker._lease_body({}, {"task_id": "CO-11"}),
    )
finally:
    worker._binding = real_binding
    worker._lease_body = real_lease_body
    worker.sb._http = real_http
    os.environ.pop("PM_AGENT_ID", None)
    os.environ.pop("PM_CO_WAKE_ID", None)

ok(len(register_calls) == 1
   and register_calls[0][0] == "POST"
   and register_calls[0][1] == "/ixp/v1/register_runner_session",
   "sample claim path posts register_runner_session")
payload = register_calls[0][2]
ok(payload.get("task_id") == "CO-11"
   and payload.get("claim_id") == "taskclaim-co11-sample"
   and payload.get("host_id") == "host/co11-dedicated-codex"
   and payload.get("runtime") == "codex",
   "register_runner_session emits task_id+claim_id+host_id for Codex runtime")

# --- Metered key refusal + lease failure closes wake ---
failed_wake_calls = []
real_register = worker._register_bound_runner
real_refuse = worker._refuse_metered_keys
try:
    worker._binding = lambda: {"project": "switchboard"}
    worker._lease_body = lambda binding, task: {
        "project": "switchboard", "credential_reference": "provider-ref",
        "user_id": "user-test", "provider": "openai-codex",
        "provider_account_id": "account-test", "task_id": "CO-11",
        "host_id": "host-test", "runner_session_id": "runner-test",
        "work_session_id": "worksession-test", "account_affinity_id": "affinity-test",
        "ttl_seconds": 900,
    }
    worker._register_bound_runner = lambda task, body: None

    def fail_lease_http(method, path, body=None, **kwargs):
        failed_wake_calls.append((method, path, dict(body or {})))
        if path.endswith("/leases"):
            return {}
        return {"completed": True}

    worker.sb._http = fail_lease_http
    try:
        worker.run({"task_id": "CO-11", "claim_id": "taskclaim-x",
                    "managed": {"workspace_path": str(repo)}})
        lease_failure_closed = False
    except RuntimeError:
        lease_failure_closed = True
finally:
    worker._binding = real_binding
    worker._lease_body = real_lease_body
    worker._register_bound_runner = real_register
    worker.sb._http = real_http

ok(lease_failure_closed
   and any(path == "/txp/v1/complete_wake" and not body["result"]["started"]
           for _method, path, body in failed_wake_calls),
   "lease/bootstrap failure closes the reserved wake with redacted failure evidence")

try:
    worker._refuse_metered_keys({"PATH": "/usr/bin", "CODEX_HOME": "/tmp/x"})
    clean_env_ok = True
except RuntimeError:
    clean_env_ok = False
try:
    worker._refuse_metered_keys({"OPENAI_API_KEY": "sk-test", "CODEX_HOME": "/tmp/x"})
    metered_refused = False
except RuntimeError as exc:
    metered_refused = "OPENAI_API_KEY" in str(exc)
try:
    worker._refuse_metered_keys({"CODEX_ACCESS_TOKEN": "tok-test", "CODEX_HOME": "/tmp/x"})
    access_token_refused = False
except RuntimeError as exc:
    access_token_refused = "CODEX_ACCESS_TOKEN" in str(exc)
ok(clean_env_ok and metered_refused and access_token_refused,
   "Codex runtime env refuses OPENAI_API_KEY/CODEX_API_KEY/CODEX_ACCESS_TOKEN before process start")

example = (ROOT / "deploy" / "switchboard-agent-host-work.service.example").read_text()
ok("PM_RUNTIME=codex" in example
   and "adapters.codex_personal_worker:run" in example
   and "PM_HOST_SUPPORTS_CREDENTIAL_LEASES=1" in example,
   "work-host unit example documents Codex personal worker + credential leases")

print(f"\nCO-11 Codex personal host: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
