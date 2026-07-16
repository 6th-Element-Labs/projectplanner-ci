#!/usr/bin/env python3
"""CO-8: subscription capacity, bounded polling, restart, and metered-policy proof."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

from path_setup import ROOT


TMP = tempfile.mkdtemp(prefix="co8-provider-capacity-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_PROVIDER_VAULT_KEY"] = base64.urlsafe_b64encode(b"C" * 32).decode()
os.environ["PM_PROVIDER_VAULT_KEY_ID"] = "co8-test:v1"

import store  # noqa: E402
from switchboard.domain.provider_capacity import (  # noqa: E402
    CAPACITY_STATES,
    account_fingerprint,
    normalize_provider_response,
)
from switchboard.domain.provider_credentials import CredentialPrincipal  # noqa: E402
from switchboard.integrations.provider_capacity import (  # noqa: E402
    SubscriptionCapacityController,
)
from switchboard.storage.repositories.provider_capacity import (  # noqa: E402
    ProviderCapacityRepository,
)
from switchboard.storage.repositories.provider_credentials import (  # noqa: E402
    default_provider_credential_repository as credential_repository,
)


PROJECT = "switchboard"
USER_ID = "user-co8-owner"
OTHER_USER_ID = "user-co8-other"
AGENT_ID = "codex/CO-8"
HOST_ID = "co8-host"
RUNNER_ID = "co8-runner"
WORK_SESSION_ID = "co8-work-session"
WAKE_ID = "wake-co8-binding"
BASE = time.time()
PRINCIPAL = CredentialPrincipal.from_mapping({
    "principal_id": "co8-system",
    "principal_kind": "system",
    "scopes": ["use:credentials"],
})
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def enroll(user_id: str, provider: str, account: str, *, mode: str = "exclusive"):
    return credential_repository.enroll(
        project=PROJECT,
        user_id=user_id,
        provider=provider,
        provider_account_id=account,
        auth_type="personal_subscription" if mode == "exclusive" else "customer_metered_api",
        credential=f"co8-secret-{user_id}-{provider}-{account}",
        project_allowlist=[PROJECT],
        actor="co8-test",
        expires_at=time.time() + 7200,
        concurrency_policy={"mode": mode, "max_parallel": 1},
    )


try:
    ok(ROOT.is_dir(), "shared test path shim resolves the repository root")

    fixtures = [
        ("openai-codex", {"status_code": 200, "ok": True}, "ready"),
        ("openai-codex", {"status_code": 429, "error": {"type": "rate_limit_error"}},
         "throttled_retryable"),
        ("anthropic-claude", {"status_code": 529, "error": {"type": "overloaded_error"}},
         "provider_capacity_exhausted"),
        ("anthropic-claude", {
            "status_code": 429, "error_code": "usage_limit_reached",
            "reset_at": BASE + 1800,
        }, "waiting_for_plan_reset"),
        ("cursor", {"status_code": 401, "error_code": "session_expired"},
         "reauthentication_required"),
        ("cursor", {"status_code": 403, "error_code": "credential_revoked"},
         "revoked"),
        ("openai-codex", {"status_code": 402, "error_code": "buy_credits"},
         "policy_blocked"),
    ]
    normalized_states = []
    for provider, response, expected in fixtures:
        signal = normalize_provider_response(provider, response, now=BASE)
        normalized_states.append(signal.state)
        ok(signal.state == expected and signal.state in CAPACITY_STATES,
           f"{provider} fixture normalizes to {expected}")
    ok(set(normalized_states) == set(CAPACITY_STATES),
       "fixtures exercise every explicit subscription-capacity state")
    secret_signal = normalize_provider_response("codex", {
        "status_code": 429,
        "error_code": "usage_limit_reached",
        "message": "token=raw-provider-secret resets in one hour",
        "headers": {"authorization": "Bearer raw-provider-secret", "retry-after": "60"},
    }, now=BASE)
    ok("raw-provider-secret" not in json.dumps(secret_signal.as_dict()),
       "normalization never carries provider messages, headers, or tokens forward")
    explicit_secret = normalize_provider_response("codex", {
        "capacity_state": "provider_capacity_exhausted",
        "error_code": "raw-provider-secret",
    }, now=BASE)
    ok("raw-provider-secret" not in json.dumps(explicit_secret.as_dict()),
       "even explicit provider states map untrusted error codes to stable safe reasons")
    contradictory_auth = normalize_provider_response("codex", {
        "capacity_state": "ready", "status_code": 401, "authenticated": False,
    }, now=BASE)
    contradictory_revoked = normalize_provider_response("cursor", {
        "capacity_state": "ready", "error_code": "credential_revoked",
    }, now=BASE)
    contradictory_metered = normalize_provider_response("codex", {
        "capacity_state": "ready", "error_code": "buy_credits",
    }, now=BASE)
    ok(contradictory_auth.state == "reauthentication_required"
       and contradictory_revoked.state == "revoked"
       and contradictory_metered.state == "policy_blocked",
       "denial signals override a contradictory explicit ready state")

    store.ensure_org(store.DEFAULT_ORG_ID, "6th Element Labs", created_by="co8-test")
    store.set_project_access(
        PROJECT, store.DEFAULT_ORG_ID, purpose="CO-8 fixture", created_by="co8-test")
    store.init_db(PROJECT)
    for user_id, email in (
        (USER_ID, "co8@example.test"), (OTHER_USER_ID, "co8-other@example.test"),
    ):
        store.ensure_user(user_id, email, user_id, created_by="co8-test")
        store.add_org_member(store.DEFAULT_ORG_ID, user_id, role="member", created_by="co8-test")
    task = store.create_task({
        "workstream_id": "CO", "workstream_name": "CO",
        "title": "CO-8 capacity fixture", "status": "Not Started",
    }, actor="co8-test", project=PROJECT)
    TASK_ID = str(task["task_id"])
    store.create_work_session({
        "work_session_id": WORK_SESSION_ID,
        "task_id": TASK_ID,
        "agent_id": AGENT_ID,
        "runtime": "codex",
        "repo_role": "canonical",
        "branch": f"codex/{TASK_ID}-capacity-fixture",
        "upstream": "origin/master",
        "base_sha": "a" * 40,
        "head_sha": "a" * 40,
        "storage_mode": "worktree",
        "worktree_path": str(Path(TMP) / "worktree"),
        "status": "active",
        "dirty_status": "clean",
        "policy_profile": "code_strict",
        "hygiene": {"repo_preflight": {"ok": True, "verdict": "pass", "findings": []}},
    }, actor="co8-test", principal_id=PRINCIPAL.principal_id, project=PROJECT)
    claimed = store.claim_task(
        TASK_ID, AGENT_ID, principal_id=PRINCIPAL.principal_id, actor="co8-test",
        ttl_seconds=3600, idem_key="co8-capacity-fixture",
        work_session_id=WORK_SESSION_ID, session_policy_profile="code_strict",
        require_work_session=True, project=PROJECT)
    CLAIM_ID = str(claimed["claim_id"])
    store.register_host({
        "host_id": HOST_ID,
        "hostname": "co8-host.test",
        "runtimes": [
            {"runtime": "codex", "capabilities": ["code"]},
            {"runtime": "claude-code", "capabilities": ["code"]},
        ],
        "capacity": {"active_sessions": 0, "max_sessions": 4},
        "heartbeat_ttl_s": 3600,
    }, principal_id=PRINCIPAL.principal_id, actor="co8-test", project=PROJECT)
    store.upsert_runner_session({
        "runner_session_id": RUNNER_ID,
        "host_id": HOST_ID,
        "agent_id": AGENT_ID,
        "runtime": "codex",
        "task_id": TASK_ID,
        "claim_id": CLAIM_ID,
        "status": "ready",
        "heartbeat_ttl_s": 3600,
        "metadata": {"work_session_id": WORK_SESSION_ID, "wake_id": WAKE_ID},
    }, principal_id=PRINCIPAL.principal_id, actor="co8-test", project=PROJECT)
    ok(bool(TASK_ID and CLAIM_ID), "created exact task, claim, Work Session, runner, and host bindings")

    personal = enroll(USER_ID, "codex", "co8-personal")
    metered = enroll(USER_ID, "codex", "co8-metered", mode="bounded")
    alternate = enroll(USER_ID, "claude", "co8-claude")
    borrowed = enroll(OTHER_USER_ID, "claude", "co8-other-claude")

    def binding(connection, user_id, provider, account):
        return {
            "project": PROJECT,
            "credential_reference": connection["credential_reference"],
            "user_id": user_id,
            "provider": provider,
            "provider_account_id": account,
            "task_id": TASK_ID,
            "claim_id": CLAIM_ID,
            "host_id": HOST_ID,
            "runner_session_id": RUNNER_ID,
            "work_session_id": WORK_SESSION_ID,
        }

    personal_binding = binding(personal, USER_ID, "openai-codex", "co8-personal")
    metered_binding = binding(metered, USER_ID, "openai-codex", "co8-metered")
    alternate_binding = binding(alternate, USER_ID, "anthropic-claude", "co8-claude")
    borrowed_binding = binding(borrowed, OTHER_USER_ID, "anthropic-claude", "co8-other-claude")
    repository = ProviderCapacityRepository()
    controller = SubscriptionCapacityController(repository=repository)

    raw_secret = "provider-secret-must-not-persist"
    waiting = repository.observe(
        personal_binding,
        {
            "status_code": 429,
            "error_code": "usage_limit_reached",
            "message": f"weekly limit; auth={raw_secret}",
            "headers": {"authorization": raw_secret},
            "retry_after_seconds": 60,
            "reset_at": BASE + 1800,
        },
        checkpoint={
            "branch": "codex/CO-8-fixture",
            "head_sha": "b" * 40,
            "continuation_ref": "switchboard://CO-8/checkpoint",
            "credential": raw_secret,
            "last_command": f"API_TOKEN={raw_secret} provider-cli",
        },
        actor="co8-worker",
        now=BASE,
    )
    waiting_text = json.dumps(waiting, sort_keys=True)
    ok(waiting["account"]["state"] == "waiting_for_plan_reset"
       and waiting["checkpoint"]["status"] == "paused",
       "plan exhaustion persists an account cooldown and paused task checkpoint")
    ok(waiting["checkpoint"]["claim_id"] == CLAIM_ID
       and waiting["checkpoint"]["work_session_id"] == WORK_SESSION_ID
       and waiting["checkpoint"]["runner_session_id"] == RUNNER_ID,
       "pause preserves claim, Work Session, runner, task, and account affinity")
    ok(raw_secret not in waiting_text
       and "credential" not in waiting["checkpoint"]["checkpoint"]
       and "last_command" not in waiting["checkpoint"]["checkpoint"],
       "operator receipt and checkpoint allowlist exclude commands and raw provider secrets")

    restarted_repository = ProviderCapacityRepository()
    restarted = restarted_repository.get_state(personal_binding)
    ok(restarted["account"]["state"] == "waiting_for_plan_reset"
       and restarted["checkpoint"]["status"] == "paused",
       "a fresh coordinator repository reconstructs the pause after restart")
    denied = restarted_repository.admission_decision(
        personal_binding,
        task_policy={"customer_user_id": USER_ID, "requested_provider": "codex"},
        host_available=True,
        now=BASE + 1,
    )
    ok(not denied["allowed"]
       and denied["reason_code"] == "personal_plan_capacity_exhausted",
       "account cooldown overrides otherwise-available raw host capacity")

    probe_calls = []
    early = controller.poll(
        personal_binding,
        idem_key="poll-too-early",
        checkpoint={"head_sha": "b" * 40},
        probe=lambda: probe_calls.append("early") or {"status_code": 200},
        actor="co8-coordinator",
        now=BASE + 1,
    )
    ok(not early["allowed"] and not probe_calls
       and early["reason_code"] == "capacity_poll_not_due",
       "reset polling does not run before the persisted retry boundary")

    still_waiting = controller.poll(
        personal_binding,
        idem_key="poll-plan-still-full",
        checkpoint={"head_sha": "c" * 40},
        probe=lambda: probe_calls.append("wait") or {
            "status_code": 429,
            "error_code": "subscription_limit_reached",
            "retry_after_seconds": 60,
            "reset_at": BASE + 1800,
            "message": raw_secret,
        },
        actor="co8-coordinator",
        now=BASE + 60,
    )
    calls_after_first_poll = list(probe_calls)
    replay = controller.poll(
        personal_binding,
        idem_key="poll-plan-still-full",
        checkpoint={"head_sha": "should-not-run"},
        probe=lambda: probe_calls.append("duplicate") or {"status_code": 200},
        actor="co8-coordinator-restarted",
        now=BASE + 60,
    )
    ok(still_waiting["account"]["state"] == "waiting_for_plan_reset"
       and replay.get("idempotent_replay") is True
       and probe_calls == calls_after_first_poll,
       "poll idempotency survives coordinator restart and prevents duplicate probes")

    bounded = controller.poll(
        personal_binding,
        idem_key="poll-budget-exhausted",
        checkpoint={"head_sha": "c" * 40},
        probe=lambda: probe_calls.append("over-budget") or {"status_code": 200},
        actor="co8-coordinator",
        now=BASE + 120,
        max_attempts=1,
    )
    ok(not bounded["allowed"]
       and bounded["reason_code"] == "capacity_poll_budget_exhausted"
       and "over-budget" not in probe_calls,
       "bounded polling stops retry storms after the configured window budget")

    resumed = controller.poll(
        personal_binding,
        idem_key="poll-capacity-returned",
        checkpoint={"head_sha": "c" * 40},
        probe=lambda: probe_calls.append("ready") or {"status_code": 200, "ok": True},
        actor="co8-coordinator-restarted",
        now=BASE + 120,
        max_attempts=8,
    )
    after_resume = ProviderCapacityRepository().get_state(personal_binding)
    ok(resumed["account"]["state"] == "ready"
       and after_resume["checkpoint"]["status"] == "resume_ready"
       and after_resume["checkpoint"]["checkpoint"]["head_sha"] == "c" * 40,
       "capacity return durably marks the preserved checkpoint ready to resume")

    repository.observe(
        personal_binding,
        {"status_code": 429, "error_code": "usage_limit_reached",
         "retry_after_seconds": 60, "reset_at": BASE + 900},
        checkpoint={"head_sha": "e" * 40}, actor="co8-worker", now=BASE + 180)
    crashed = repository.begin_poll(
        personal_binding, idem_key="poll-crash-reclaim", actor="co8-coordinator",
        now=BASE + 240, lease_seconds=60)
    in_flight = ProviderCapacityRepository().begin_poll(
        personal_binding, idem_key="poll-crash-reclaim", actor="co8-other-coordinator",
        now=BASE + 241, lease_seconds=60)
    reclaimed = ProviderCapacityRepository().begin_poll(
        personal_binding, idem_key="poll-crash-reclaim", actor="co8-other-coordinator",
        now=BASE + 300, lease_seconds=60)
    old_completion = repository.complete_poll(
        personal_binding, poll_id=crashed["poll_id"], attempt=crashed["attempt"],
        response={"status_code": 200, "ok": True},
        checkpoint={"head_sha": "old-worker-must-not-win"},
        actor="co8-crashed-coordinator", now=BASE + 301)
    state_after_old_completion = repository.get_state(personal_binding)
    new_completion = ProviderCapacityRepository().complete_poll(
        personal_binding, poll_id=reclaimed["poll_id"], attempt=reclaimed["attempt"],
        response={
            "status_code": 429, "error_code": "usage_limit_reached",
            "retry_after_seconds": 60, "reset_at": BASE + 900,
        },
        checkpoint={"head_sha": "f" * 40},
        actor="co8-other-coordinator", now=BASE + 302)
    reclaimed_replay = repository.begin_poll(
        personal_binding, idem_key="poll-crash-reclaim", actor="co8-coordinator-restarted",
        now=BASE + 303)
    ok(crashed["execute_probe"]
       and not in_flight["execute_probe"]
       and in_flight["reason_code"] == "capacity_poll_in_flight"
       and reclaimed["execute_probe"] and reclaimed.get("reclaimed") is True
       and reclaimed["attempt"] > crashed["attempt"],
       "an in-flight poll is single-owner until its expired lease is reclaimed")
    ok(old_completion["reason_code"] == "capacity_poll_stale"
       and state_after_old_completion["account"]["state"] == "waiting_for_plan_reset"
       and new_completion["account"]["state"] == "waiting_for_plan_reset"
       and reclaimed_replay.get("idempotent_replay") is True
       and not reclaimed_replay["execute_probe"],
       "poll generations fence a crashed worker while preserving idempotent replay")

    stale_started = repository.begin_poll(
        personal_binding, idem_key="poll-stale-race", actor="co8-coordinator",
        now=BASE + 362)
    repository.observe(
        personal_binding, {"status_code": 200, "ok": True},
        checkpoint={"head_sha": "e" * 40}, actor="co8-other-coordinator", now=BASE + 363)
    stale_completed = repository.complete_poll(
        personal_binding, poll_id=stale_started["poll_id"], attempt=stale_started["attempt"],
        response={"status_code": 429, "error_code": "usage_limit_reached"},
        checkpoint={"head_sha": "stale-must-not-win"},
        actor="co8-stale-coordinator", now=BASE + 364)
    state_after_stale = repository.get_state(personal_binding)
    ok(stale_completed["reason_code"] == "capacity_poll_stale"
       and state_after_stale["account"]["state"] == "ready",
       "a stale reset poll cannot overwrite a newer ready observation")

    lease = credential_repository.acquire_lease(
        project=PROJECT,
        credential_reference=personal["credential_reference"],
        user_id=USER_ID,
        provider="codex",
        provider_account_id="co8-personal",
        task_id=TASK_ID,
        host_id=HOST_ID,
        runner_session_id=RUNNER_ID,
        work_session_id=WORK_SESSION_ID,
        ttl_seconds=900,
        actor="co8-test",
        principal=PRINCIPAL,
        claim_id=CLAIM_ID,
        wake_id=WAKE_ID,
        account_affinity_id=account_fingerprint("codex", "co8-personal"),
    )
    concurrency = repository.admission_decision(
        personal_binding,
        task_policy={"customer_user_id": USER_ID, "requested_provider": "codex"},
        host_available=True,
    )
    ok(not concurrency["allowed"]
       and concurrency["reason_code"] == "provider_account_concurrency_limit",
       "per-user/provider/account concurrency overrides free host slots")
    credential_repository.release_lease(
        lease["lease_id"], project=PROJECT, actor="co8-test", reason="fixture_complete",
        principal=PRINCIPAL)

    no_substitute = repository.admission_decision(
        alternate_binding,
        task_policy={
            "customer_user_id": USER_ID,
            "requested_provider": "codex",
            "allow_provider_substitution": False,
            "allowed_providers": ["claude"],
        },
    )
    allowed_substitute = repository.admission_decision(
        alternate_binding,
        task_policy={
            "customer_user_id": USER_ID,
            "requested_provider": "codex",
            "allow_provider_substitution": True,
            "allowed_providers": ["claude"],
        },
    )
    borrowed_denied = repository.admission_decision(
        borrowed_binding,
        task_policy={
            "customer_user_id": USER_ID,
            "requested_provider": "codex",
            "allow_provider_substitution": True,
            "allowed_providers": ["claude"],
        },
    )
    ok(not no_substitute["allowed"] and not allowed_substitute["allowed"]
       and allowed_substitute["reason_code"] == "provider_auth_vendor_confirmation_required",
       "task substitution cannot override the server provider-auth approval gate")
    ok(not borrowed_denied["allowed"]
       and borrowed_denied["reason_code"] == "cross_customer_account_denied",
       "scheduler policy can never borrow another customer's provider account")

    issued = []
    default_metered = controller.run_lane(
        metered_binding,
        task_policy={"customer_user_id": USER_ID, "requested_provider": "codex"},
        lane_policy={"lane_kind": "api"},
        checkpoint={"head_sha": "d" * 40},
        personal_request=lambda: issued.append("personal") or {"status_code": 200},
        metered_request=lambda: issued.append("metered") or {"status_code": 200},
        actor="co8-scheduler",
    )
    ok(not default_metered["allowed"] and not issued
       and default_metered["reason_code"] == "metered_lane_disabled_by_default",
       "API/pay-as-you-go fallback is off by default and issues no request")

    payg_metered = controller.run_lane(
        metered_binding,
        task_policy={"customer_user_id": USER_ID, "requested_provider": "codex"},
        lane_policy={"lane_kind": "payg"},
        checkpoint={"head_sha": "d" * 40},
        personal_request=lambda: issued.append("payg-personal") or {"status_code": 200},
        metered_request=lambda: issued.append("payg-metered") or {"status_code": 200},
        actor="co8-scheduler",
    )
    unknown_lane = controller.run_lane(
        metered_binding,
        task_policy={"customer_user_id": USER_ID, "requested_provider": "codex"},
        lane_policy={"lane_kind": "common"},
        checkpoint={"head_sha": "d" * 40},
        personal_request=lambda: issued.append("unknown-personal") or {"status_code": 200},
        metered_request=lambda: issued.append("unknown-metered") or {"status_code": 200},
        actor="co8-scheduler",
    )
    ok(not payg_metered["allowed"]
       and payg_metered["reason_code"] == "metered_lane_disabled_by_default"
       and not unknown_lane["allowed"]
       and unknown_lane["reason_code"] == "lane_kind_not_supported"
       and not issued,
       "payg is metered and unknown lane kinds fail closed without issuing a request")

    incomplete_metered = repository.admission_decision(
        metered_binding,
        task_policy={"customer_user_id": USER_ID, "requested_provider": "codex"},
        lane_policy={
            "lane_kind": "metered", "enabled": True,
            "personal_credential_reference": personal["credential_reference"],
            "metered_credential_reference": metered["credential_reference"],
            "audited_opt_in": {"enabled": True},
            "budget_ceiling": 25,
            "cost_attribution": {"budget_id": "B-8", "cost_center": "CO", "currency": "USD"},
        },
    )
    ok(not incomplete_metered["allowed"]
       and incomplete_metered["reason_code"] == "audited_metered_opt_in_required",
       "metered lane fails closed without a complete audited opt-in")

    metered_policy = {
        "lane_kind": "metered",
        "enabled": True,
        "personal_credential_reference": personal["credential_reference"],
        "metered_credential_reference": metered["credential_reference"],
        "audited_opt_in": {
            "enabled": True, "actor": USER_ID,
            "audit_id": "metered-opt-in-co8", "approved_at": BASE,
        },
        "budget_ceiling": 25,
        "cost_attribution": {
            "budget_id": "B-8", "cost_center": "CO", "currency": "USD",
            "secret": raw_secret,
        },
    }
    allowed_metered = controller.run_lane(
        metered_binding,
        task_policy={"customer_user_id": USER_ID, "requested_provider": "codex"},
        lane_policy=metered_policy,
        checkpoint={"head_sha": "d" * 40},
        personal_request=lambda: issued.append("wrong-personal") or {"status_code": 200},
        metered_request=lambda: issued.append("authorized-metered") or {
            "status_code": 200, "ok": True, "debug": raw_secret,
        },
        actor="co8-scheduler",
    )
    allowed_text = json.dumps(allowed_metered, sort_keys=True)
    ok(allowed_metered["allowed"] and allowed_metered["metered"]
       and issued == ["authorized-metered"]
       and allowed_metered["cost_attribution"]["budget_id"] == "B-8",
       "separate customer credential, audited opt-in, budget, and attribution authorize one metered request")
    ok(raw_secret not in allowed_text,
       "authorized metered receipts expose cost attribution without provider or policy secrets")

    same_credential = repository.admission_decision(
        personal_binding,
        task_policy={"customer_user_id": USER_ID, "requested_provider": "codex"},
        lane_policy={
            **metered_policy,
            "metered_credential_reference": personal["credential_reference"],
            "personal_credential_reference": personal["credential_reference"],
        },
    )
    ok(not same_credential["allowed"]
       and same_credential["reason_code"] == "separate_metered_credential_required",
       "personal subscription credentials cannot be silently reused as a metered lane")

    with sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"]) as c:
        c.row_factory = sqlite3.Row
        capacity_rows = [dict(row) for table in (
            "provider_capacity_accounts", "provider_capacity_checkpoints",
            "provider_capacity_polls", "provider_capacity_events",
        ) for row in c.execute(f"SELECT * FROM {table}").fetchall()]
    ok(raw_secret not in json.dumps(capacity_rows, default=str),
       "persistent capacity, checkpoint, poll, and audit rows contain no raw provider secrets")

finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nCO-8 subscription capacity: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
