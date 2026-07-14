#!/usr/bin/env python3
"""COORD-25: stable CO Fleet principals remain bound to one ephemeral host."""
from __future__ import annotations

import time

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands import provider_credentials as commands
from switchboard.contracts.provider_credentials import AcquireProviderCredentialLeaseCommand
from switchboard.domain.provider_credentials import CredentialPrincipal
from switchboard.storage.repositories.provider_credentials import CredentialVaultError


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


PROJECT = "switchboard"
TASK_ID = "COORD-25"
AGENT_ID = "claude-code/COORD-15"
HOST_ID = "host/i-ephemeral"
RUNNER_ID = "run-coord25"
WORK_SESSION_ID = "worksession-coord25"
CLAIM_ID = "taskclaim-coord25"
PRINCIPAL_ID = "host/co-fleet-worker"
AFFINITY_ID = "affinity-coord25"

command = AcquireProviderCredentialLeaseCommand.model_validate({
    "project": PROJECT,
    "credential_reference": "provider-cred-coord25",
    "user_id": "user-coord25",
    "provider": "anthropic-claude",
    "provider_account_id": "account-coord25",
    "task_id": TASK_ID,
    "host_id": HOST_ID,
    "runner_session_id": RUNNER_ID,
    "work_session_id": WORK_SESSION_ID,
    "account_affinity_id": AFFINITY_ID,
})
principal = CredentialPrincipal.from_mapping({
    "principal_id": PRINCIPAL_ID,
    "principal_kind": "host",
    "scopes": ["use:credentials"],
})
work_session = {
    "work_session_id": WORK_SESSION_ID,
    "task_id": TASK_ID,
    "agent_id": AGENT_ID,
    "claim_id": CLAIM_ID,
    "principal_id": PRINCIPAL_ID,
    "status": "active",
    "health": {"blocking": False},
}
runner = {
    "runner_session_id": RUNNER_ID,
    "task_id": TASK_ID,
    "host_id": HOST_ID,
    "agent_id": AGENT_ID,
    "claim_id": CLAIM_ID,
    "principal_id": PRINCIPAL_ID,
    "status": "running",
    "stale": False,
    "claim": {
        "id": CLAIM_ID,
        "task_id": TASK_ID,
        "agent_id": AGENT_ID,
        "principal_id": PRINCIPAL_ID,
        "status": "active",
        "expires_at": time.time() + 600,
    },
    "metadata": {
        "work_session_id": WORK_SESSION_ID,
        "account_affinity_id": AFFINITY_ID,
    },
}
host = {
    "host_id": HOST_ID,
    "principal_id": PRINCIPAL_ID,
    "status": "online",
    "stale": False,
}

real_get_task = commands.store.get_task
real_get_work_session = commands.store.get_work_session
real_get_runner_session = commands.store.get_runner_session
real_list_agent_hosts = commands.store.list_agent_hosts
try:
    commands.store.get_task = lambda *args, **kwargs: {"task_id": TASK_ID}
    commands.store.get_work_session = lambda *args, **kwargs: dict(work_session)
    commands.store.get_runner_session = lambda *args, **kwargs: dict(runner)
    commands.store.list_agent_hosts = lambda *args, **kwargs: [dict(host)]

    commands._validate_runtime_binding(command, principal)
    ok(True, "stable fleet host principal is accepted for its exact live ephemeral host")

    commands.store.list_agent_hosts = lambda *args, **kwargs: [
        {**host, "principal_id": "host/other-fleet-principal"}
    ]
    try:
        commands._validate_runtime_binding(command, principal)
        wrong_host_denied = False
    except CredentialVaultError as exc:
        wrong_host_denied = exc.code == "credential_host_binding_invalid"
    ok(wrong_host_denied, "a host registered to a different principal is denied")

    commands.store.list_agent_hosts = lambda *args, **kwargs: [dict(host)]
    commands.store.get_runner_session = lambda *args, **kwargs: {
        **runner, "principal_id": "host/other-fleet-principal"
    }
    try:
        commands._validate_runtime_binding(command, principal)
        wrong_runtime_denied = False
    except CredentialVaultError as exc:
        wrong_runtime_denied = exc.code == "credential_principal_binding_invalid"
    ok(wrong_runtime_denied, "a runner bound to a different principal is denied")
finally:
    commands.store.get_task = real_get_task
    commands.store.get_work_session = real_get_work_session
    commands.store.get_runner_session = real_get_runner_session
    commands.store.list_agent_hosts = real_list_agent_hosts

print(f"\nCOORD-25 stable fleet host principal: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
