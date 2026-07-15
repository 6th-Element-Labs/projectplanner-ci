#!/usr/bin/env python3
"""CO-7: personal-plan CLI auth isolation, writeback, fencing, and purge proof."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid

from path_setup import ROOT


TMP = tempfile.mkdtemp(prefix="co7-provider-runtime-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_PROVIDER_VAULT_KEY"] = base64.urlsafe_b64encode(b"R" * 32).decode()
os.environ["PM_PROVIDER_VAULT_KEY_ID"] = "co7-test:v1"

import store  # noqa: E402
from switchboard.domain.provider_credentials import CredentialPrincipal  # noqa: E402
from switchboard.integrations.provider_runtime_auth import ProviderRuntimeAuth  # noqa: E402
from switchboard.storage.repositories.provider_credentials import (  # noqa: E402
    CredentialVaultError,
    default_provider_credential_repository as repository,
)


PROJECT = "switchboard"
USER_ID = "user-co7-owner"
AGENT_ID = "codex/CO-700"
HOST_ID = "co7-host"
RUNNER_ID = "co7-runner"
WORK_SESSION_ID = "co7-work-session"
PRINCIPAL_ID = "dev-open"
PRINCIPAL = CredentialPrincipal.from_mapping({
    "principal_id": PRINCIPAL_ID,
    "principal_kind": "system",
    "scopes": ["use:credentials", "admin"],
})
RUNTIME_ROOT = Path(TMP) / "runtime"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def no_runtime_residue() -> bool:
    return RUNTIME_ROOT.is_dir() and not list(RUNTIME_ROOT.iterdir())


observed: list[dict] = []
secret_by_provider: dict[str, str] = {}
refreshed_codex_capsule = ""


def fake_preflight(args, **kwargs):
    env = dict(kwargs.get("env") or {})
    executable = str(args[0])
    if executable == "codex-test":
        provider = "openai-codex"
        auth_path = Path(env["CODEX_HOME"]) / "auth.json"
        value = auth_path.read_text()
        ok(value == secret_by_provider[provider],
           "Codex receives the enrolled opaque capsule only in isolated CODEX_HOME")
        ok(stat.S_IMODE(auth_path.stat().st_mode) == 0o600
           and stat.S_IMODE(auth_path.parent.stat().st_mode) == 0o700,
           "Codex auth file/home permissions are 0600/0700")
        ok("OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env,
           "Codex personal auth removes metered API-key fallbacks")
        ok("PM_PROVIDER_VAULT_KEY" not in env,
           "provider processes never inherit the vault master key")
        output = "Logged in using ChatGPT\n"
    elif executable == "claude-test":
        provider = "anthropic-claude"
        ok(env.get("CLAUDE_CODE_OAUTH_TOKEN") == secret_by_provider[provider],
           "Claude receives only its personal OAuth/setup-token credential")
        ok(not any(key in env for key in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY", "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX", "GOOGLE_APPLICATION_CREDENTIALS")),
           "Claude metered API, Bedrock, and Vertex fallbacks are removed")
        output = json.dumps({
            "loggedIn": True,
            "authMethod": "oauth_token",
            "subscriptionType": "max",
            "debug": secret_by_provider[provider],
        })
    else:
        provider = "cursor"
        ok(env.get("CURSOR_API_KEY") == secret_by_provider[provider],
           "Cursor receives its enrolled personal key only in the isolated process")
        output = json.dumps({
            "authenticated": True,
            "account": {"email": "owner@example.test"},
            "debug": secret_by_provider[provider],
        })
    observed.append({"kind": "preflight", "provider": provider,
                     "args": list(args), "env": env})
    return subprocess.CompletedProcess(args, 0, stdout=output,
                                       stderr="provider-secret-output-must-not-escape")


class FakeProcess:
    next_pid = 7100

    def __init__(self, args, **kwargs):
        global refreshed_codex_capsule
        self.args = list(args)
        self.env = dict(kwargs.get("env") or {})
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode = None
        if "CODEX_HOME" in self.env:
            auth_path = Path(self.env["CODEX_HOME"]) / "auth.json"
            auth_path.write_text(refreshed_codex_capsule)
            os.chmod(auth_path, 0o600)
        observed.append({"kind": "process", "args": self.args, "env": self.env})

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def fake_process(args, **kwargs):
    return FakeProcess(args, **kwargs)


adapter = ProviderRuntimeAuth(
    repository=repository,
    runtime_parent=RUNTIME_ROOT,
    cli_paths={
        "openai-codex": "codex-test",
        "anthropic-claude": "claude-test",
        "cursor": "cursor-test",
    },
    command_runner=fake_preflight,
    process_factory=fake_process,
    base_environment={
        "PATH": os.environ.get("PATH", ""),
        "OPENAI_API_KEY": "metered-openai-must-disappear",
        "ANTHROPIC_API_KEY": "metered-anthropic-must-disappear",
        "ANTHROPIC_AUTH_TOKEN": "anthropic-fallback-must-disappear",
        "AWS_ACCESS_KEY_ID": "aws-must-disappear",
        "AWS_SECRET_ACCESS_KEY": "aws-must-disappear",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "GOOGLE_APPLICATION_CREDENTIALS": "/must/disappear",
        "CURSOR_API_KEY": "other-customer-must-disappear",
        "PM_PROVIDER_VAULT_KEY": "vault-master-key-must-disappear",
    },
)

retry_calls = []
retry_secret = "provider-retry-output-must-not-escape"


def retrying_preflight(args, **kwargs):
    retry_calls.append(list(args))
    if len(retry_calls) < 3:
        return subprocess.CompletedProcess(
            args, 1, stdout=retry_secret, stderr=retry_secret)
    return subprocess.CompletedProcess(
        args, 0,
        stdout=json.dumps({
            "loggedIn": True, "authMethod": "oauth_token", "apiProvider": "firstParty",
        }),
        stderr="",
    )


retry_adapter = ProviderRuntimeAuth(
    cli_paths={"anthropic-claude": "claude-test"},
    command_runner=retrying_preflight,
    preflight_attempts=3,
    preflight_retry_delay_seconds=0,
    base_environment={},
)
retry_receipt = retry_adapter._preflight("anthropic-claude", {}, None)
retry_text = json.dumps(retry_receipt, sort_keys=True)
ok(retry_receipt.get("authenticated") is True
   and retry_receipt.get("attempt_count") == 3
   and len(retry_calls) == 3,
   "provider preflight retries transient failures within a fixed bound")
ok(retry_receipt.get("provider_output_redacted") is True
   and retry_secret not in retry_text
   and "stdout_sha256" in retry_receipt,
   "preflight diagnostics expose only byte counts and output hashes")

failed_calls = []


def failed_preflight(args, **kwargs):
    failed_calls.append(list(args))
    return subprocess.CompletedProcess(
        args, 9, stdout=retry_secret, stderr=retry_secret)


failed_adapter = ProviderRuntimeAuth(
    cli_paths={"anthropic-claude": "claude-test"},
    command_runner=failed_preflight,
    preflight_attempts=2,
    preflight_retry_delay_seconds=0,
    base_environment={},
)
failed_receipt = failed_adapter._preflight("anthropic-claude", {}, None)
failed_text = json.dumps(failed_receipt, sort_keys=True)
ok(failed_receipt.get("authenticated") is False
   and failed_receipt.get("attempt_count") == 2
   and failed_receipt.get("exit_code") == 9
   and len(failed_calls) == 2
   and retry_secret not in failed_text,
   "exhausted preflight returns bounded redacted failure evidence")


try:
    ok(ROOT.is_dir(), "shared test path shim resolves the repository root")
    store.ensure_org(store.DEFAULT_ORG_ID, "6th Element Labs", created_by="co7-test")
    store.set_project_access(
        PROJECT, store.DEFAULT_ORG_ID, purpose="CO-7 fixture", created_by="co7-test")
    store.init_db(PROJECT)
    store.ensure_user(USER_ID, "co7@example.test", "CO-7 owner", created_by="co7-test")
    store.add_org_member(store.DEFAULT_ORG_ID, USER_ID, role="member", created_by="co7-test")
    task = store.create_task({
        "workstream_id": "CO",
        "workstream_name": "CO",
        "title": "CO-7 runtime fixture",
        "status": "Not Started",
    }, actor="co7-test", project=PROJECT)
    TASK_ID = str((task or {}).get("task_id") or "")
    store.create_work_session({
        "work_session_id": WORK_SESSION_ID,
        "task_id": TASK_ID,
        "agent_id": AGENT_ID,
        "runtime": "codex",
        "repo_role": "canonical",
        "branch": f"codex/{TASK_ID}-runtime-fixture",
        "upstream": "origin/master",
        "base_sha": "a" * 40,
        "head_sha": "a" * 40,
        "storage_mode": "worktree",
        "worktree_path": str(Path(TMP) / "worktree"),
        "status": "active",
        "dirty_status": "clean",
        "policy_profile": "code_strict",
        "hygiene": {"repo_preflight": {"ok": True, "verdict": "pass", "findings": []}},
    }, actor="co7-test", principal_id=PRINCIPAL_ID, project=PROJECT)
    claimed = store.claim_task(
        TASK_ID, AGENT_ID, principal_id=PRINCIPAL_ID, actor="co7-test",
        ttl_seconds=3600, idem_key="co7-runtime-fixture",
        work_session_id=WORK_SESSION_ID, session_policy_profile="code_strict",
        require_work_session=True, project=PROJECT)
    CLAIM_ID = str(claimed.get("claim_id") or "")
    store.register_host({
        "host_id": HOST_ID,
        "hostname": "co7-host.test",
        "runtimes": [
            {"runtime": "codex", "capabilities": ["code"]},
            {"runtime": "claude-code", "capabilities": ["code"]},
            {"runtime": "cursor", "capabilities": ["code"]},
        ],
        "capacity": {"active_sessions": 0, "max_sessions": 3},
        "heartbeat_ttl_s": 3600,
    }, principal_id=PRINCIPAL_ID, actor="co7-test", project=PROJECT)
    ok(bool(TASK_ID and CLAIM_ID), "created exact task/claim/session/host binding fixtures")

    def bound_connection(provider: str, account: str, auth_type: str,
                         secret: str | None = None):
        normalized = {
            "codex": "openai-codex",
            "claude": "anthropic-claude",
            "cursor": "cursor",
        }[provider]
        runtime = {
            "codex": "codex",
            "claude": "claude-code",
            "cursor": "cursor",
        }[provider]
        credential = secret or f"co7-{provider}-{uuid.uuid4().hex}"
        secret_by_provider[normalized] = credential
        connection = repository.enroll(
            project=PROJECT,
            user_id=USER_ID,
            provider=provider,
            provider_account_id=account,
            auth_type=auth_type,
            credential=credential,
            project_allowlist=[PROJECT],
            actor="co7-test",
            expires_at=time.time() + 3600,
            concurrency_policy={"mode": "exclusive", "max_parallel": 1},
        )
        store.upsert_runner_session({
            "runner_session_id": RUNNER_ID,
            "host_id": HOST_ID,
            "agent_id": AGENT_ID,
            "runtime": runtime,
            "task_id": TASK_ID,
            "claim_id": CLAIM_ID,
            "status": "ready",
            "heartbeat_ttl_s": 3600,
            "metadata": {
                "work_session_id": WORK_SESSION_ID,
                "credential_reference": connection["credential_reference"],
                "provider_account_id": account,
            },
        }, principal_id=PRINCIPAL_ID, actor="co7-test", project=PROJECT)
        binding = {
            "project": PROJECT,
            "credential_reference": connection["credential_reference"],
            "user_id": USER_ID,
            "provider": normalized,
            "provider_account_id": account,
            "task_id": TASK_ID,
            "host_id": HOST_ID,
            "runner_session_id": RUNNER_ID,
            "work_session_id": WORK_SESSION_ID,
            "ttl_seconds": 900,
        }
        lease = repository.acquire_lease(
            project=PROJECT,
            credential_reference=connection["credential_reference"],
            user_id=USER_ID,
            provider=normalized,
            provider_account_id=account,
            task_id=TASK_ID,
            host_id=HOST_ID,
            runner_session_id=RUNNER_ID,
            work_session_id=WORK_SESSION_ID,
            ttl_seconds=900,
            actor="co7-test",
            principal=PRINCIPAL,
        )
        return connection, binding, lease, credential

    codex, codex_binding, codex_lease, codex_secret = bound_connection(
        "codex", "codex-personal-account", "chatgpt_auth_capsule")
    refreshed_codex_capsule = "co7-codex-refreshed-" + uuid.uuid4().hex
    codex_receipt = adapter.run(
        codex_binding,
        lease_id=codex_lease["lease_id"],
        principal=PRINCIPAL,
        actor="co7-runtime",
        command=["agent-lane", "--task", TASK_ID],
    )
    codex_text = json.dumps(codex_receipt, sort_keys=True)
    ok(codex_receipt.get("allowed") is True
       and codex_receipt.get("status") == "completed"
       and codex_receipt.get("auth_preflight", {}).get("auth_mode") == "chatgpt_personal"
       and codex_receipt.get("codex_writeback", {}).get("written_back") is True,
       "Codex authenticates with ChatGPT state and reseals a changed opaque capsule")
    ok(all(value not in codex_text for value in (
        codex_secret, refreshed_codex_capsule, "codex-personal-account",
        "provider-secret-output-must-not-escape")),
       "Codex receipt contains only redacted account attribution and safe preflight fields")
    ok(codex_receipt.get("lease_state") == "released" and no_runtime_residue(),
       "Codex process exit purges the runtime home before releasing its lease")

    replay_preflights = len([item for item in observed if item["kind"] == "preflight"])
    replay_receipt = adapter.run(
        codex_binding,
        lease_id=codex_lease["lease_id"],
        principal=PRINCIPAL,
        actor="co7-runtime",
        command=["agent-lane", "--task", TASK_ID],
    )
    ok(replay_receipt.get("allowed") is False
       and len([item for item in observed if item["kind"] == "preflight"]) == replay_preflights,
       "a replayed lease cannot start a second provider CLI preflight or process")

    fresh_codex_lease = repository.acquire_lease(
        project=PROJECT, credential_reference=codex["credential_reference"],
        user_id=USER_ID, provider="openai-codex",
        provider_account_id="codex-personal-account", task_id=TASK_ID,
        host_id=HOST_ID, runner_session_id=RUNNER_ID,
        work_session_id=WORK_SESSION_ID, ttl_seconds=900,
        actor="co7-test", principal=PRINCIPAL)
    recovered = repository.materialize_for_runtime(
        fresh_codex_lease["lease_id"], actor="co7-recovery", principal=PRINCIPAL,
        **{key: codex_binding[key] for key in (
            "project", "user_id", "provider", "provider_account_id", "task_id",
            "host_id", "runner_session_id", "work_session_id")})
    repository.fence_materialized_lease(
        fresh_codex_lease["lease_id"], actor="co7-test", reason="recovery_proof",
        principal=PRINCIPAL)
    ok(recovered == refreshed_codex_capsule,
       "a fresh exclusive lease recovers the latest fenced Codex auth-state writeback")

    claude_connection = repository.enroll(
        project=PROJECT, user_id=USER_ID, provider="claude",
        provider_account_id="claude-personal-account",
        auth_type="setup_token_oauth",
        credential="co7-claude-" + uuid.uuid4().hex,
        project_allowlist=[PROJECT], actor="co7-test",
        expires_at=time.time() + 3600,
        concurrency_policy={"mode": "exclusive", "max_parallel": 1},
    )
    claude_secret = "co7-claude-secret-should-not-leak"
    claude_binding = {
        "project": PROJECT,
        "credential_reference": claude_connection["credential_reference"],
        "user_id": USER_ID,
        "provider": "anthropic-claude",
        "provider_account_id": "claude-personal-account",
        "task_id": TASK_ID,
        "host_id": HOST_ID,
        "runner_session_id": RUNNER_ID,
        "work_session_id": WORK_SESSION_ID,
        "ttl_seconds": 900,
    }
    try:
        repository.acquire_lease(
            project=PROJECT,
            credential_reference=claude_connection["credential_reference"],
            user_id=USER_ID, provider="anthropic-claude",
            provider_account_id="claude-personal-account", task_id=TASK_ID,
            host_id=HOST_ID, runner_session_id=RUNNER_ID,
            work_session_id=WORK_SESSION_ID, ttl_seconds=900,
            actor="co7-test", principal=PRINCIPAL,
        )
        ok(False, "Claude subscription lease acquire must fail closed under CO-15")
    except CredentialVaultError as exc:
        ok(exc.code == "provider_auth_vendor_confirmation_required",
           "CO-15 denies Claude subscription leases before issuance")
    before_claude = len(observed)
    claude_receipt = adapter.run(
        claude_binding,
        lease_id="",
        principal=PRINCIPAL,
        actor="co7-runtime",
        command=["agent-lane", "--task", TASK_ID],
    )
    ok(claude_receipt.get("allowed") is False
       and claude_receipt.get("error_code") == "provider_auth_vendor_confirmation_required"
       and len(observed) == before_claude,
       "CO-15 denies Claude subscription routing before CLI preflight or process start")
    ok(claude_secret not in json.dumps(claude_receipt, sort_keys=True)
       and no_runtime_residue(),
       "denied Claude subscription auth remains redacted and purged")

    for provider, account, auth_type, expected_command in (
        ("cursor", "cursor-personal-account", "personal_api_key", ["status", "--format", "json"]),
    ):
        connection, binding, lease, secret = bound_connection(
            provider, account, auth_type)
        receipt = adapter.run(
            binding,
            lease_id=lease["lease_id"],
            principal=PRINCIPAL,
            actor="co7-runtime",
            command=["agent-lane", "--task", TASK_ID],
        )
        text = json.dumps(receipt, sort_keys=True)
        ok(receipt.get("allowed") is True and receipt.get("status") == "completed"
           and receipt.get("auth_preflight", {}).get("authenticated") is True,
           f"{provider} explicit API-key CLI auth preflight succeeds before lane start")
        preflight = next(item for item in reversed(observed)
                         if item["kind"] == "preflight"
                         and item["provider"] == binding["provider"])
        ok(preflight["args"][1:] == expected_command,
           f"{provider} invokes the real vendor CLI authentication-status command shape")
        process = next(item for item in reversed(observed) if item["kind"] == "process")
        ok(secret not in " ".join(process["args"])
           and secret not in text and account not in text,
           f"{provider} keeps credentials out of argv and returns redacted account proof")
        ok(receipt.get("lease_state") == "released" and no_runtime_residue(),
           f"{provider} process exit purges its isolated environment and releases the lease")

    class InterruptedProcess(FakeProcess):
        def __init__(self, args, **kwargs):
            super().__init__(args, **kwargs)
            self.interrupted = False

        def wait(self, timeout=None):
            if timeout is None and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt()
            return int(self.returncode or -15)

    interrupted_adapter = ProviderRuntimeAuth(
        repository=repository,
        runtime_parent=RUNTIME_ROOT,
        cli_paths=adapter.cli_paths,
        command_runner=fake_preflight,
        process_factory=lambda args, **kwargs: InterruptedProcess(args, **kwargs),
        base_environment=adapter.base_environment,
    )
    _interrupted, interrupted_binding, interrupted_lease, _ = bound_connection(
        "cursor", "cursor-interrupted-account", "personal_api_key")
    interrupted = False
    try:
        interrupted_adapter.run(
            interrupted_binding, lease_id=interrupted_lease["lease_id"],
            principal=PRINCIPAL, actor="co7-runtime",
            command=["agent-lane", "--task", TASK_ID])
    except KeyboardInterrupt:
        interrupted = True
    with sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"]) as c:
        interrupted_state = c.execute(
            "SELECT state FROM provider_credential_leases WHERE lease_id=?",
            (interrupted_lease["lease_id"],)).fetchone()[0]
    ok(interrupted and interrupted_state == "released" and no_runtime_residue(),
       "supervisor/host interruption terminates the process, purges residue, and releases the lease")

    revoked, revoked_binding, revoked_lease, _ = bound_connection(
        "cursor", "cursor-revoked-account", "personal_api_key")
    repository.revoke(
        revoked["credential_reference"], project=PROJECT, actor="co7-test",
        reason="revoked adapter proof", principal_user_id=USER_ID)
    before_revoked = len(observed)
    revoked_receipt = adapter.run(
        revoked_binding, lease_id=revoked_lease["lease_id"], principal=PRINCIPAL,
        actor="co7-runtime", command=["agent-lane", "--task", TASK_ID])
    ok(revoked_receipt.get("allowed") is False and len(observed) == before_revoked,
       "revoked provider credentials fail before CLI preflight or process start")

    _expired, expired_binding, expired_lease, _ = bound_connection(
        "cursor", "cursor-expired-account", "personal_api_key")
    with sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"]) as c:
        c.execute("UPDATE provider_credential_leases SET expires_at=? WHERE lease_id=?",
                  (time.time() - 1, expired_lease["lease_id"]))
    before_expired = len(observed)
    expired_receipt = adapter.run(
        expired_binding, lease_id=expired_lease["lease_id"], principal=PRINCIPAL,
        actor="co7-runtime", command=["agent-lane", "--task", TASK_ID])
    ok(expired_receipt.get("allowed") is False and len(observed) == before_expired,
       "expired provider lease fails before CLI preflight or process start")

    _mismatch, mismatch_binding, mismatch_lease, _ = bound_connection(
        "cursor", "cursor-binding-account", "personal_api_key")
    before_mismatch = len(observed)
    mismatch_receipt = adapter.run(
        {**mismatch_binding, "provider_account_id": "other-customer-account"},
        lease_id=mismatch_lease["lease_id"], principal=PRINCIPAL,
        actor="co7-runtime", command=["agent-lane", "--task", TASK_ID],
        validate_runtime=False)
    ok(mismatch_receipt.get("allowed") is False and len(observed) == before_mismatch,
       "mismatched customer/account binding fails before decrypt or process start")

    with sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"]) as c:
        c.row_factory = sqlite3.Row
        writeback_event = c.execute(
            "SELECT * FROM provider_credential_events WHERE event_type='credential_writeback'"
        ).fetchone()
        rows = c.execute(
            "SELECT encrypted_credential, credential_nonce FROM provider_connections"
        ).fetchall()
    ok(writeback_event is not None
       and refreshed_codex_capsule not in json.dumps(dict(writeback_event), default=str),
       "Codex refresh audit records versioned provenance without capsule material")
    stored = b"".join(
        bytes(value or b"")
        for row in rows
        for value in (row["encrypted_credential"], row["credential_nonce"])
    )
    all_secrets = list(secret_by_provider.values()) + [refreshed_codex_capsule]
    ok(all(secret.encode() not in stored for secret in all_secrets),
       "vault artifacts contain encrypted provider state, never plaintext credentials")
    ok(no_runtime_residue(), "final residue scan finds no provider runtime homes")

finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nCO-7 provider runtime auth: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
