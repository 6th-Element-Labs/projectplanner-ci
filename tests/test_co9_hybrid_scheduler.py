#!/usr/bin/env python3
"""CO-9: hybrid placement, fail-closed affinity, burst, recovery, and audit proof."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from path_setup import ROOT


TMP = Path(tempfile.mkdtemp(prefix="co9-hybrid-scheduler-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_PROVIDER_VAULT_KEY"] = base64.urlsafe_b64encode(b"C" * 32).decode()
os.environ["PM_PROVIDER_VAULT_KEY_ID"] = "co9-test:v1"

import co_fleet  # noqa: E402
import store  # noqa: E402
from switchboard.domain.coordination.placement import (  # noqa: E402
    HOST_PLACEMENT_SCHEMA,
    order_wakes_fairly,
    plan_hybrid_placement,
)
from switchboard.domain.provider_credentials import CredentialPrincipal  # noqa: E402
from switchboard.storage.repositories.provider_capacity import (  # noqa: E402
    ProviderCapacityRepository,
)
from switchboard.storage.repositories.provider_credentials import (  # noqa: E402
    default_provider_credential_repository as credential_repository,
)


PROJECT = "switchboard"
BASE = time.time()
USER_ID = "user-co9-owner"
PROVIDER_ACCOUNT_ID = "co9-personal"
PRINCIPAL = CredentialPrincipal.from_mapping({
    "principal_id": "co9-system",
    "principal_kind": "system",
    "scopes": ["use:credentials"],
})
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def placement(kind, *, tenant="", provider="", affinity="", cost="already_paid"):
    return {
        "schema": HOST_PLACEMENT_SCHEMA,
        "host_class": kind,
        "cost_class": cost,
        "wakeable": True,
        "drain_state": "accepting",
        "tenant_ids": [tenant] if tenant else [],
        "projects": [PROJECT],
        "providers": [provider] if provider else [],
        "account_affinity_ids": [affinity] if affinity else [],
        "supports_credential_leases": True,
        "repositories": ["6th-Element-Labs/projectplanner"],
        "session_policies": ["code_strict"],
        "isolation_modes": ["task_worktree"],
        "runtime_binaries": ["codex", "git", "python3"],
        "provider_capacity_mode": "external_account_admission",
        "resources": {
            "cpu_available": 8,
            "memory_mb_available": 16384,
            "disk_gb_available": 100,
        },
        "concurrency": {"max_sessions": 1},
    }


def register(host_id, kind, *, active=0, tenant="", provider="", affinity="",
             ttl=10, cost="already_paid"):
    return store.register_host({
        "host_id": host_id,
        "hostname": host_id.replace("/", "-"),
        "repo_root": str(ROOT),
        "runtimes": [{
            "runtime": "codex",
            "lanes": ["CO"],
            "capabilities": ["co_fleet", "code"],
            "policy": {"allow_work": True},
        }],
        "limits": {"max_sessions": 1},
        "capacity": {
            "active_sessions": active,
            "placement": placement(
                kind, tenant=tenant, provider=provider, affinity=affinity, cost=cost),
        },
        "heartbeat_ttl_s": ttl,
    }, actor="co9-test", project=PROJECT)


def task(title):
    return store.create_task({
        "workstream_id": "CO", "workstream_name": "CO", "title": title,
        "status": "Not Started", "policy_profile": "code_strict",
    }, actor="co9-test", project=PROJECT)


def claim_context(task_id, suffix):
    work_session_id = f"worksession-co9-{suffix}"
    agent_id = f"codex/{task_id}-{suffix}"
    worktree = TMP / f"worktree-{suffix}"
    worktree.mkdir(exist_ok=True)
    store.create_work_session({
        "work_session_id": work_session_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "runtime": "codex",
        "repo_role": "canonical",
        "branch": f"codex/{task_id}-{suffix}",
        "upstream": f"origin/codex/{task_id}-{suffix}",
        "base_sha": "a" * 40,
        "head_sha": "a" * 40,
        "storage_mode": "worktree",
        "worktree_path": str(worktree),
        "status": "active",
        "dirty_status": "clean",
        "policy_profile": "code_strict",
        "hygiene": {"repo_preflight": {"ok": True, "verdict": "pass", "findings": []}},
    }, actor="co9-test", principal_id=PRINCIPAL.principal_id, project=PROJECT)
    claimed = store.claim_task(
        task_id, agent_id, actor="co9-test", principal_id=PRINCIPAL.principal_id,
        ttl_seconds=3600, idem_key=f"co9-claim:{suffix}",
        work_session_id=work_session_id, session_policy_profile="code_strict",
        require_work_session=True, project=PROJECT,
    )
    return work_session_id, str(claimed["claim_id"])


def account_binding(connection, task_id, claim_id, work_session_id):
    binding = {
        "tenant_id": store.DEFAULT_ORG_ID,
        "user_id": USER_ID,
        "project": PROJECT,
        "provider": "openai-codex",
        "provider_account_id": PROVIDER_ACCOUNT_ID,
        "credential_reference": connection["credential_reference"],
        "task_id": task_id,
        "claim_id": claim_id,
        "work_session_id": work_session_id,
    }
    source = {key: binding.get(key) for key in (
        "tenant_id", "user_id", "project", "provider", "provider_account_id",
        "credential_reference", "auth_lane",
    )}
    binding["account_affinity_id"] = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return binding


def hybrid_policy(*, bucket="deliverable-co9", binding=None, provider_capacity=None):
    value = {
        "mode": "co_fleet",
        "runtime_config_ref": "ssm:/switchboard/co/runtime/co9-test",
        "allow_on_demand": False,
        "scheduler": {
            "mode": "hybrid", "prefer_persistent": True,
            "allow_persistent": True, "allow_ephemeral": True,
            "burst_enabled": True, "fair_share_key": bucket,
            "max_host_loss_reschedules": 3,
        },
        "placement": {
            "canonical_repo": "6th-Element-Labs/projectplanner",
            "session_policy": "code_strict", "isolation": "task_worktree",
            "runtime_binaries": ["codex", "git"],
            "resources": {"cpu": 2, "memory_mb": 1024, "disk_gb": 5},
        },
    }
    if binding:
        value["account_binding"] = binding
        value["account_binding_required"] = True
    if provider_capacity is not None:
        value["provider_capacity"] = provider_capacity
    return value


def request(task_id, suffix, *, policy=None):
    return store.request_wake(
        selector={
            "runtime": "codex", "lane": "CO", "agent_id": f"codex/{task_id}-{suffix}",
            "capabilities": ["co_fleet"],
        },
        reason=f"CO-9 fixture {suffix}", source="co9-test", policy=policy or hybrid_policy(),
        task_id=task_id, actor="co9-test", project=PROJECT,
        idem_key=f"co9:{task_id}:{suffix}",
    )


try:
    store.ensure_org(store.DEFAULT_ORG_ID, "6th Element Labs", created_by="co9-test")
    store.set_project_access(
        PROJECT, store.DEFAULT_ORG_ID, purpose="CO-9 fixture", created_by="co9-test")
    store.init_db(PROJECT)
    store.ensure_user(USER_ID, "co9@example.test", "CO-9 owner", created_by="co9-test")
    store.add_org_member(
        store.DEFAULT_ORG_ID, USER_ID, role="member", created_by="co9-test")
    provider_connection = credential_repository.enroll(
        project=PROJECT,
        user_id=USER_ID,
        provider="codex",
        provider_account_id=PROVIDER_ACCOUNT_ID,
        auth_type="personal_subscription",
        credential="co9-provider-secret",
        project_allowlist=[PROJECT],
        actor="co9-test",
        expires_at=BASE + 7200,
        concurrency_policy={"mode": "exclusive", "max_parallel": 1},
    )
    capacity_repository = ProviderCapacityRepository()
    persistent = register("host/persistent-co9", "persistent", ttl=10)
    ok((persistent.get("capacity") or {}).get("placement", {}).get("host_class") == "persistent",
       "persistent Agent Host advertises placement, resource, policy, and cost inventory")

    first_task = task("CO-9 same-deliverable persistent half")
    second_task = task("CO-9 same-deliverable ephemeral half")
    first = request(first_task["task_id"], "persistent")
    second = request(second_task["task_id"], "burst")
    ok(first["placement"]["action"] == "assign_persistent"
       and first["placement"]["cost_class"] == "already_paid",
       "already-paid healthy persistent capacity is selected first with an explanation")
    ok(second["placement"]["action"] == "provision_ephemeral"
       and second["placement"]["reason_code"] == "persistent_capacity_saturated",
       "the pending persistent reservation saturates the local pool and triggers Spot-first burst")

    persistent_claim = store.claim_wake(
        "host/persistent-co9", first["wake_id"], actor="co9-persistent", project=PROJECT)
    ephemeral = register(
        "host/i-co9-burst", "ephemeral", ttl=10, cost="spot")
    ephemeral_claim = store.claim_wake(
        "host/i-co9-burst", second["wake_id"], actor="co9-ephemeral", project=PROJECT)
    ok(persistent_claim.get("claimed") and ephemeral_claim.get("claimed")
       and persistent_claim["wake"]["placement"]["selected_host_class"] == "persistent"
       and ephemeral_claim["wake"]["placement"]["selected_host_class"] == "ephemeral",
       "one deliverable can hold simultaneous persistent and ephemeral host placements")

    bootstrap_task = task("CO-9 two-phase BYOA bootstrap")
    bootstrap_binding = account_binding(
        provider_connection, bootstrap_task["task_id"], "", "")
    bootstrap_binding.pop("claim_id", None)
    bootstrap_binding.pop("work_session_id", None)
    bootstrap_binding["credential_admission_phase"] = "preclaim"
    bootstrap_host = "host/i-co9-bootstrap"
    register(
        bootstrap_host, "ephemeral", tenant=store.DEFAULT_ORG_ID,
        provider="openai-codex", affinity=bootstrap_binding["account_affinity_id"],
        ttl=3600, cost="spot")
    bootstrap_wake = request(
        bootstrap_task["task_id"], "bootstrap",
        policy=hybrid_policy(binding=bootstrap_binding))
    bootstrap_runner = "runner-co9-bootstrap"
    reserved = store.claim_wake(
        bootstrap_host, bootstrap_wake["wake_id"],
        runner_session_id=bootstrap_runner,
        actor="co9-bootstrap-reserve", project=PROJECT)
    bootstrap_work_session, bootstrap_claim_id = claim_context(
        bootstrap_task["task_id"], "bootstrap")
    bootstrap_lease = credential_repository.acquire_lease(
        project=PROJECT,
        credential_reference=provider_connection["credential_reference"],
        user_id=USER_ID,
        provider="codex",
        provider_account_id=PROVIDER_ACCOUNT_ID,
        task_id=bootstrap_task["task_id"],
        host_id=bootstrap_host,
        runner_session_id=bootstrap_runner,
        work_session_id=bootstrap_work_session,
        ttl_seconds=900,
        actor="co9-test",
        principal=PRINCIPAL,
    )
    admitted = store.claim_wake(
        bootstrap_host, bootstrap_wake["wake_id"],
        runner_session_id=bootstrap_runner,
        credential_lease_id=bootstrap_lease["lease_id"],
        claim_id=bootstrap_claim_id,
        work_session_id=bootstrap_work_session,
        actor="co9-bootstrap-admit", project=PROJECT)
    ok(reserved.get("reserved") is True
       and reserved.get("credential_admission_phase") == "pending"
       and admitted.get("claimed") is True
       and admitted.get("credential_admission_phase") == "ready",
       "BYOA wake reserves first and admits only after exact claim/session/lease binding")
    credential_repository.release_lease(
        bootstrap_lease["lease_id"], project=PROJECT, actor="co9-test",
        reason="bootstrap_fixture_complete", principal=PRINCIPAL)

    bound_task = task("CO-9 authoritative credential admission")
    bound_work_session, bound_claim_id = claim_context(bound_task["task_id"], "bound")
    binding = account_binding(
        provider_connection, bound_task["task_id"], bound_claim_id, bound_work_session)
    bound_host_id = "host/i-co9-bound"
    register(
        bound_host_id, "ephemeral", tenant=store.DEFAULT_ORG_ID,
        provider="openai-codex", affinity=binding["account_affinity_id"],
        ttl=10, cost="spot")
    bound = request(
        bound_task["task_id"], "bound", policy=hybrid_policy(binding=binding))
    register("host/i-co9-wrong-affinity", "ephemeral", tenant="wrong-tenant",
             provider="openai-codex", affinity="a" * 64, ttl=3600, cost="spot")
    wrong_claim = store.claim_wake(
        "host/i-co9-wrong-affinity", bound["wake_id"],
        actor="co9-wrong", project=PROJECT)
    ok(not wrong_claim.get("claimed")
       and {"tenant_not_allowed", "provider_account_affinity_mismatch"}.issubset(
           set(wrong_claim.get("reason_codes") or [])),
       "wrong tenant and provider-account affinity fail closed before lease use")
    fake_claim = store.claim_wake(
        bound_host_id, bound["wake_id"], runner_session_id="runner-co9-fake",
        credential_lease_id="lease-co9-fake", actor="co9-fake", project=PROJECT)
    ok(not fake_claim.get("claimed")
       and "credential_lease_not_available" in set(fake_claim.get("reason_codes") or []),
       "a syntactically valid but nonexistent lease fails authoritative claim admission")

    expired_runner = "runner-co9-expired"
    expired_lease = credential_repository.acquire_lease(
        project=PROJECT,
        credential_reference=provider_connection["credential_reference"],
        user_id=USER_ID,
        provider="codex",
        provider_account_id=PROVIDER_ACCOUNT_ID,
        task_id=bound_task["task_id"],
        host_id=bound_host_id,
        runner_session_id=expired_runner,
        work_session_id=bound_work_session,
        ttl_seconds=-1,
        actor="co9-test",
        principal=PRINCIPAL,
    )
    expired_claim = store.claim_wake(
        bound_host_id, bound["wake_id"], runner_session_id=expired_runner,
        credential_lease_id=expired_lease["lease_id"],
        actor="co9-expired", project=PROJECT)
    ok(not expired_claim.get("claimed")
       and "credential_lease_not_claimable" in set(
           expired_claim.get("reason_codes") or []),
       "an issued lease that expired before claim fails closed")

    bound_runner = "runner-co9-bound"
    bound_lease = credential_repository.acquire_lease(
        project=PROJECT,
        credential_reference=provider_connection["credential_reference"],
        user_id=USER_ID,
        provider="codex",
        provider_account_id=PROVIDER_ACCOUNT_ID,
        task_id=bound_task["task_id"],
        host_id=bound_host_id,
        runner_session_id=bound_runner,
        work_session_id=bound_work_session,
        ttl_seconds=900,
        actor="co9-test",
        principal=PRINCIPAL,
    )
    cross_boundary_claim = store.claim_wake(
        bound_host_id, bound["wake_id"], runner_session_id="runner-co9-other",
        credential_lease_id=bound_lease["lease_id"],
        actor="co9-cross-boundary", project=PROJECT)
    ok(not cross_boundary_claim.get("claimed")
       and "credential_lease_binding_mismatch" in set(
           cross_boundary_claim.get("reason_codes") or []),
       "a real issued lease cannot cross its runner-session boundary")

    capacity_binding = {
        **binding,
        "host_id": bound_host_id,
        "runner_session_id": bound_runner,
    }
    capacity_repository.observe(
        capacity_binding,
        {"status_code": 429, "error_code": "usage_limit_reached",
         "reset_at": BASE + 3600},
        checkpoint=None, actor="co9-test", now=BASE + 1,
    )
    capacity_blocked_claim = store.claim_wake(
        bound_host_id, bound["wake_id"], runner_session_id=bound_runner,
        credential_lease_id=bound_lease["lease_id"],
        actor="co9-capacity-blocked", project=PROJECT)
    ok(not capacity_blocked_claim.get("claimed")
       and capacity_blocked_claim.get("reason") == "provider_capacity_denied"
       and "personal_plan_capacity_exhausted" in set(
           capacity_blocked_claim.get("reason_codes") or []),
       "claim rechecks live provider capacity and fails closed after placement")
    capacity_repository.observe(
        capacity_binding, {"status_code": 200, "ok": True},
        checkpoint=None, actor="co9-test", now=BASE + 2,
    )
    bound_claim = store.claim_wake(
        bound_host_id, bound["wake_id"], runner_session_id=bound_runner,
        credential_lease_id=bound_lease["lease_id"],
        actor="co9-bound", project=PROJECT)
    ok(bound_claim.get("claimed")
       and bound_claim["wake"]["placement"]["credential_lease_state"] == "issued"
       and bound_claim["wake"]["policy"]["provider_capacity"]["allowed"] is True,
       "an exact host/runner/work-session lease passes claim-time capacity admission")

    ready_provider = hybrid_policy(provider_capacity={
        "allowed": True, "state": "ready", "reason_code": "provider_slot_ready",
    })
    full_host = dict(persistent)
    full_host["capacity"] = {**(full_host.get("capacity") or {}), "active_sessions": 1}
    burst_plan = plan_hybrid_placement(
        [full_host], {"runtime": "codex", "lane": "CO", "capabilities": ["co_fleet"]},
        ready_provider, project=PROJECT)
    provider_denied = hybrid_policy(provider_capacity={
        "allowed": False, "state": "waiting_for_plan_reset",
        "reason_code": "personal_plan_capacity_exhausted",
    })
    denied_plan = plan_hybrid_placement(
        [persistent], {"runtime": "codex", "lane": "CO", "capabilities": ["co_fleet"]},
        provider_denied, project=PROJECT)
    ok(burst_plan["action"] == "provision_ephemeral"
       and burst_plan["provider_capacity"]["state"] == "ready"
       and denied_plan["action"] == "deny"
       and denied_plan["reason_code"] == "provider_subscription_capacity_denied",
       "provider subscription admission remains independent from physical host headroom")

    fair = order_wakes_fairly([
        {"wake_id": "a1", "placement": {"scheduler_mode": "hybrid",
                                           "fair_share_bucket": "tenant-a"}},
        {"wake_id": "a2", "placement": {"scheduler_mode": "hybrid",
                                           "fair_share_bucket": "tenant-a"}},
        {"wake_id": "b1", "placement": {"scheduler_mode": "hybrid",
                                           "fair_share_bucket": "tenant-b"}},
    ])
    ok([wake["wake_id"] for wake in fair] == ["a1", "b1", "a2"],
       "fair-share queueing round-robins tenants without breaking per-tenant FIFO")

    provisioned = []
    original_provision = co_fleet.provision_wake
    co_fleet.provision_wake = lambda aws, client, config, wake: (
        provisioned.append(wake["wake_id"]) or {
            "schema": co_fleet.SCHEMA, "wake_id": wake["wake_id"],
            "task_id": wake.get("task_id"), "capacity_type": "spot",
        }
    )

    class QueueClient:
        def pending_wakes(self):
            return [first, second]

        def fail_wake(self, *args, **kwargs):
            raise AssertionError("fixture provisioning must not fail")

    config = co_fleet.load_config({
        "CO_IDLE_SECONDS": "600", "CO_STATE_PATH": str(TMP / "fleet-state.json"),
        "CO_LOCK_PATH": str(TMP / "fleet.lock"),
    })
    outcomes = co_fleet.process_once(object(), QueueClient(), config)
    co_fleet.provision_wake = original_provision
    ok(provisioned == [second["wake_id"]]
       and outcomes[0]["action"] == "defer_to_registered_host"
       and outcomes[1]["capacity_type"] == "spot",
       "fleet daemon defers persistent placements and provisions only the burst decision")

    old = BASE - 1000
    instance = {
        "InstanceId": "i-co9-idle", "State": {"Name": "running"},
        "LaunchTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(old)),
        "Tags": [{"Key": "CO:Pool", "Value": "co-general"}],
    }

    class ScaleAws:
        def __init__(self):
            self.calls = []

        def call(self, service, operation, *args, **kwargs):
            self.calls.append((service, operation))
            if operation == "describe-instances":
                return {"Reservations": [{"Instances": [instance]}]}
            if operation == "send-command":
                return {"Command": {"CommandId": "co9-drain-command"}}
            return {"TerminatingInstances": [{"InstanceId": "i-co9-idle"}]}

    class IdleClient:
        def __init__(self, receipt=None):
            self.receipt = receipt

        def hosts(self, include_stale=True):
            burst_host = {
                "host_id": "host/i-co9-idle", "status": "online", "stale": False,
                "capacity": {"active_sessions": 0},
            }
            if self.receipt:
                burst_host["status"] = "drained"
                burst_host["capacity"]["drain_receipt"] = self.receipt
            return [persistent, burst_host]

        def runners(self, host_id):
            return []

        def claimed_wakes(self, host_id):
            return []

    config.state_path.write_text(json.dumps({"idle_since": {"i-co9-idle": old}}))
    scale_aws = ScaleAws()
    draining = co_fleet.scale_in_once(scale_aws, IdleClient(), config, now=BASE)
    state = json.loads(config.state_path.read_text())
    drain = state["drains"]["i-co9-idle"]
    receipt = {"request_id": drain["request_id"], "status": "drained"}
    terminated = co_fleet.scale_in_once(
        scale_aws, IdleClient(receipt), config, now=BASE + 1)
    ok(draining[0]["action"] == "request_drain"
       and terminated[0]["action"] == "terminate_drained"
       and ("ec2", "terminate-instances") in scale_aws.calls,
       "idle ephemeral burst drains and scales to zero even while persistent hosts exist")

    pending_host_id = "host/00-co9-pending-short"
    register(pending_host_id, "persistent", ttl=10)
    pending_task = task("CO-9 pending selected-host loss")
    pending = request(pending_task["task_id"], "pending-host-loss")
    ok(pending["placement"]["selected_host_id"] == pending_host_id,
       "pending wake durably selects the short-lived host before claim")
    register("host/01-co9-pending-replacement", "persistent", ttl=3600)
    bound_replacement = "host/i-co9-bound-replacement"
    register(
        bound_replacement, "ephemeral", tenant=store.DEFAULT_ORG_ID,
        provider="openai-codex", affinity=binding["account_affinity_id"],
        ttl=3600, cost="spot")

    recovered = store.sweep_wake_intents(project=PROJECT, now=BASE + 30)
    wakes_after_loss = {
        wake["wake_id"]: wake for wake in store.list_wake_intents(project=PROJECT)
    }
    first_after_loss = wakes_after_loss[first["wake_id"]]
    pending_after_loss = wakes_after_loss[pending["wake_id"]]
    bound_after_loss = wakes_after_loss[bound["wake_id"]]
    ok(recovered["requeued"] >= 4 and first_after_loss["status"] == "pending"
       and first_after_loss["placement"]["checkpoint_required"] is True
       and first_after_loss["placement"]["workspace_reconstruction"]
       == "switchboard_claim_plus_git_provenance",
       "lost claimed hosts requeue with explicit checkpoint and reconstruction evidence")
    ok(pending_after_loss["status"] == "pending"
       and pending_after_loss["placement"]["lost_host_id"] == pending_host_id
       and pending_after_loss["placement"]["selected_host_id"] != pending_host_id,
       "a pending selected-host loss is detected and replanned before its deadline")
    ok(bound_after_loss["status"] == "pending"
       and bound_after_loss["placement"]["credential_rebind_required"] is True
       and bound_after_loss["placement"]["action"] in {
           "assign_persistent", "assign_ephemeral", "provision_ephemeral"}
       and not bound_after_loss["policy"]["account_binding"].get("credential_lease_id"),
       "host loss fences the old lease and immediately replans for fresh binding")

    missing_rebind = store.claim_wake(
        bound_replacement, bound["wake_id"], actor="co9-rebind-missing", project=PROJECT)
    ok(not missing_rebind.get("claimed")
       and "runner_session_required_for_credential_reservation" in set(
           missing_rebind.get("reason_codes") or []),
       "recovered work cannot reserve until the replacement runner identity is known")
    replacement_runner = "runner-co9-replacement"
    replacement_lease = credential_repository.acquire_lease(
        project=PROJECT,
        credential_reference=provider_connection["credential_reference"],
        user_id=USER_ID,
        provider="codex",
        provider_account_id=PROVIDER_ACCOUNT_ID,
        task_id=bound_task["task_id"],
        host_id=bound_replacement,
        runner_session_id=replacement_runner,
        work_session_id=bound_work_session,
        ttl_seconds=900,
        actor="co9-test",
        principal=PRINCIPAL,
    )
    rebound = store.claim_wake(
        bound_replacement, bound["wake_id"], runner_session_id=replacement_runner,
        credential_lease_id=replacement_lease["lease_id"],
        actor="co9-rebound", project=PROJECT)
    ok(rebound.get("claimed")
       and rebound["wake"]["placement"]["credential_rebind_required"] is False
       and rebound["wake"]["policy"]["account_binding"]["host_id"] == bound_replacement,
       "a fresh exact lease clears the recovery fence and resumes the wake")

    capacity_repository.observe(
        {
            **binding,
            "host_id": bound_replacement,
            "runner_session_id": replacement_runner,
        },
        {"status_code": 429, "error_code": "usage_limit_reached", "reset_at": BASE + 3600},
        checkpoint=None, actor="co9-test", now=BASE + 31,
    )
    denied_task = task("CO-9 authoritative provider capacity denial")
    denied_binding = account_binding(
        provider_connection, denied_task["task_id"], "claim-co9-denied",
        "worksession-co9-denied")
    denied_wake = request(
        denied_task["task_id"], "capacity-denied",
        policy=hybrid_policy(binding=denied_binding))
    ok(denied_wake["status"] == "failed"
       and denied_wake["placement"]["action"] == "deny"
       and denied_wake["placement"]["provider_capacity"]["state"]
       == "waiting_for_plan_reset",
       "live CO-8 provider capacity state denies placement independently of host slots")

    with store._control_plane_conn(PROJECT) as connection:
        kinds = {row[0] for row in connection.execute(
            "SELECT kind FROM activity WHERE kind LIKE 'wake.placement_%'"
        ).fetchall()}
    placement_text = json.dumps(
        [first_after_loss["placement"], bound_after_loss["placement"]], sort_keys=True)
    ok({"wake.placement_decided", "wake.placement_claimed",
        "wake.placement_recovered"}.issubset(kinds)
       and "credential_reference" not in placement_text
       and first_after_loss["placement"].get("reason_code"),
       "placement, cost, claim, and recovery reasons are durable, explainable, and redacted")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nCO-9 hybrid scheduler: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
