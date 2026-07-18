#!/usr/bin/env python3
"""COORD-21: remote Work Session and encrypted BYOA bootstrap proof."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from path_setup import ROOT

from adapters import switchboard_core as sb
from adapters import claude_personal_worker as worker
from switchboard.integrations.worker_credential_envelope import (
    decrypt_on_worker,
    encrypt_for_worker,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


tmp = Path(tempfile.mkdtemp(prefix="coord21-byoa-"))
repo = tmp / "repo"
subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
subprocess.run(["git", "-C", str(repo), "config", "user.name", "Switchboard Test"], check=True)
(repo / "proof.txt").write_text("clean\n")
subprocess.run(["git", "-C", str(repo), "add", "proof.txt"], check=True)
subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True,
               capture_output=True)
origin = tmp / "origin.git"
subprocess.run(["git", "clone", "--bare", str(repo), str(origin)], check=True,
               capture_output=True)
subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)],
               check=True)

calls = []
real_http = sb._http


def fake_http(method, path, body=None, **kwargs):
    calls.append((method, path, dict(body or {})))
    return {"created": True, "work_session": {
        "work_session_id": "worksession-coord21",
        "branch": (body or {}).get("branch"),
    }}


try:
    sb._http = fake_http
    session = sb.create_external_work_session(
        "switchboard", "COORD-21", "claude/COORD-21", "claude-code",
        str(repo), policy_profile="code_strict",
    )
finally:
    sb._http = real_http

payload = calls[0][2]
ok(session["external"] is True and session["work_session_id"] == "worksession-coord21",
   "worker-local git state becomes a durable external Work Session")
ok("coord-21" in session["branch"].lower()
   and payload["dirty_status"] == "clean"
   and payload["hygiene"]["external_host_preflight"] is True,
   "external Work Session is clean and task-branch scoped before claim")
ok("git@github.com" not in json.dumps(payload)
   and len(payload["hygiene"]["origin_fingerprint"]) == 16,
   "external registration persists only a remote fingerprint, not transport details")

real_create_external = sb.create_external_work_session
real_claim_task = sb.claim_task
real_expire_external = sb.expire_external_work_session
old_remote = os.environ.get("PM_REMOTE_WORK_SESSION_REGISTRATION")
old_task = os.environ.get("PM_TASK_ID")
expired = []
try:
    os.environ["PM_REMOTE_WORK_SESSION_REGISTRATION"] = "1"
    os.environ["PM_TASK_ID"] = "COORD-21"
    sb.create_external_work_session = lambda *args, **kwargs: {
        "work_session_id": "worksession-lost-race", "external": True,
    }
    sb.claim_task = lambda *args, **kwargs: {"claimed": False, "reason": "already_claimed"}
    sb.expire_external_work_session = lambda *args, **kwargs: expired.append(args) or {}
    lost_claim, lost_session = sb._acquire_claim(
        "switchboard", "claude/COORD-21", ["COORD"], "https://example.test",
        "test-token", 1800, True, str(repo),
    )
finally:
    sb.create_external_work_session = real_create_external
    sb.claim_task = real_claim_task
    sb.expire_external_work_session = real_expire_external
    if old_remote is None:
        os.environ.pop("PM_REMOTE_WORK_SESSION_REGISTRATION", None)
    else:
        os.environ["PM_REMOTE_WORK_SESSION_REGISTRATION"] = old_remote
    if old_task is None:
        os.environ.pop("PM_TASK_ID", None)
    else:
        os.environ["PM_TASK_ID"] = old_task
ok(not lost_claim["claimed"] and lost_session["external"] and len(expired) == 1,
   "a lost exact-claim race expires the orphaned external Work Session")

bound_head = subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "HEAD"],
    check=True, capture_output=True, text=True).stdout.strip()
real_get_task = sb.get_task
real_get_work_session = sb.get_work_session
personal_workspace_root = tmp / "personal-workspaces"
bound_env = {key: os.environ.get(key) for key in (
    "PM_REMOTE_WORK_SESSION_REGISTRATION", "PM_AUTO_WORK_SESSION", "PM_TASK_ID",
    "PM_PERSONAL_AGENT_HOST_EXECUTION", "PM_PERSONAL_WORKSPACE_ROOT",
    "PM_CO_ACCOUNT_BINDING_JSON", "PM_SOURCE_SHA", "PM_AGENT_HOST_ALLOW_FILE_REPO",
)}
try:
    os.environ.update({
        "PM_REMOTE_WORK_SESSION_REGISTRATION": "1",
        "PM_AUTO_WORK_SESSION": "1",
        "PM_TASK_ID": "COORD-21",
        "PM_PERSONAL_AGENT_HOST_EXECUTION": "1",
        "PM_PERSONAL_WORKSPACE_ROOT": str(personal_workspace_root),
        "PM_AGENT_HOST_ALLOW_FILE_REPO": "1",
        "PM_CO_ACCOUNT_BINDING_JSON": json.dumps({
            "task_id": "COORD-21",
            "claim_id": "taskclaim-personal-bound",
            "work_session_id": "worksession-personal-bound",
        }),
        "PM_SOURCE_SHA": bound_head,
    })
    sb.get_work_session = lambda *args, **kwargs: {
        "work_session_id": "worksession-personal-bound",
        "task_id": "COORD-21",
        "claim_id": "taskclaim-personal-bound",
        "agent_id": "codex/COORD-21",
        "status": "active",
        "head_sha": bound_head,
        "branch": "codex/COORD-21-byoa",
        "repo": repo.as_uri(),
        "worktree_path": str(repo),
        "policy_profile": "code_strict",
    }
    sb.get_task = lambda *args, **kwargs: {
        "task_id": "COORD-21",
        "active_claims": [{
            "claim_id": "taskclaim-personal-bound",
            "agent_id": "codex/COORD-21",
        }],
    }
    adopted_claim, adopted_session = sb._acquire_claim(
        "switchboard", "codex/COORD-21", ["COORD"], "https://example.test",
        "test-token", 1800, True, str(repo),
    )
finally:
    sb.get_task = real_get_task
    sb.get_work_session = real_get_work_session
    for key, value in bound_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
adopted_workspace = Path((adopted_session or {}).get("workspace_path") or "")
adopted_head = subprocess.run(
    ["git", "-C", str(adopted_workspace), "rev-parse", "HEAD"],
    check=True, capture_output=True, text=True).stdout.strip()
ok(adopted_claim.get("adopted_existing_claim") is True
   and adopted_session.get("bound_existing") is True
   and adopted_workspace.resolve().is_relative_to(personal_workspace_root.resolve())
   and adopted_workspace.resolve() != repo.resolve()
   and adopted_head == bound_head,
   "personal Agent Host adopts the exact pre-bound claim into a host-local exact checkout")

secret = "claude-setup-token-must-never-serialize"
private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
private_pem = private_key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
public_pem = private_key.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()
binding = {
    "project": "switchboard", "task_id": "COORD-21", "host_id": "host/i-test",
    "runner_session_id": "run-test", "work_session_id": "worksession-coord21",
    "lease_id": "provider-lease-test",
}
envelope = encrypt_for_worker(secret, public_pem, binding)
serialized = json.dumps(envelope, sort_keys=True)
ok(secret not in serialized and envelope["algorithm"] == "RSA-OAEP-256+A256GCM",
   "vault returns hybrid ciphertext rather than a plaintext credential")
ok(decrypt_on_worker(envelope, private_pem) == secret,
   "only the worker's ephemeral private key decrypts the bound envelope")
tampered = json.loads(serialized)
tampered["binding"]["host_id"] = "host/i-other"
try:
    decrypt_on_worker(tampered, private_pem)
    tamper_denied = False
except Exception:
    tamper_denied = True
ok(tamper_denied, "host-binding tampering fails authenticated decryption")

fleet_source = (ROOT / "co_fleet.py").read_text()
worker_source = (ROOT / "adapters" / "claude_personal_worker.py").read_text()
ok("personal-subscription fleet config contains forbidden fallback fields" in fleet_source
   and "CLAUDE_CODE_OAUTH_TOKEN" in worker_source
   and "ANTHROPIC_API_KEY" not in worker_source,
   "personal Claude worker has no metered API-key fallback path")

real_binding = worker._binding
real_lease_body = worker._lease_body
real_register_bound_runner = worker._register_bound_runner
real_worker_http = worker.sb._http
failed_wake_calls = []
try:
    worker._binding = lambda: {"project": "switchboard"}
    worker._lease_body = lambda binding, task: {
        "project": "switchboard", "credential_reference": "provider-ref",
        "user_id": "user-test", "provider": "anthropic-claude",
        "provider_account_id": "account-test", "task_id": "COORD-21",
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
        worker.run({"task_id": "COORD-21", "managed": {"workspace_path": str(repo)}})
        lease_failure_closed = False
    except RuntimeError:
        lease_failure_closed = True
finally:
    worker._binding = real_binding
    worker._lease_body = real_lease_body
    worker._register_bound_runner = real_register_bound_runner
    worker.sb._http = real_worker_http
ok(lease_failure_closed
   and any(path == "/txp/v1/complete_wake" and not body["result"]["started"]
           for _method, path, body in failed_wake_calls),
   "lease/bootstrap failure closes the reserved wake with redacted failure evidence")
ok(any(path == "/ixp/v1/register_runner_session" and body.get("status") == "failed"
       for _method, path, body in failed_wake_calls),
   "worker failure terminalizes the central runner record for audited drain")

print(f"\nCOORD-21 BYOA bootstrap: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
