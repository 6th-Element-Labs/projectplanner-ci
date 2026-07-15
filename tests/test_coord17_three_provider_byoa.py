#!/usr/bin/env python3
"""COORD-17: three-provider proof aggregation and Cursor worker contract."""
from __future__ import annotations

import json
import os

from path_setup import ROOT

from adapters import cursor_personal_worker as cursor_worker
from switchboard.domain.coordination.coord17_proof import (
    REQUIRED_ISOLATION_CHECKS,
    REQUIRED_LEASE_CHECKS,
    REQUIRED_PROVIDERS,
    build_coord17_acceptance,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def provider_receipt(provider: str, auth_mode: str) -> dict:
    binding = {
        "tenant_id": "tenant-owner",
        "user_id": "user-owner",
        "provider": provider,
        "provider_account_id": f"{provider}-account",
        "provider_account_attribution": "acct-1234567890abcdef",
        "credential_reference": f"provider-cred-{provider}",
        "credential_lease_id": f"provider-lease-{provider}",
        "project": "switchboard",
        "task_id": "COORD-17",
        "host_id": f"host-{provider}",
        "runner_session_id": f"run-{provider}",
        "work_session_id": f"worksession-{provider}",
        "claim_id": f"claim-{provider}",
        "credential_values_redacted": True,
    }
    return {
        "started": True,
        "personal_subscription": True,
        "auth_mode": auth_mode,
        "api_key_fallback": False,
        "metered_fallback": False,
        "provider_output_redacted": True,
        "credential_values_redacted": True,
        "residue_purged": True,
        "provider_account_attribution": "acct-1234567890abcdef",
        "binding": binding,
        "durable_evidence": {
            "work_session_id": binding["work_session_id"],
            "branch": f"codex/COORD-17-{provider}",
            "head_sha": "a" * 40,
            "executed_test_run": f"testrun-{provider}",
            "remote_ref": f"refs/heads/codex/COORD-17-{provider}",
        },
    }


evidence = {
    "providers": {
        "anthropic-claude": provider_receipt("anthropic-claude", "oauth_personal"),
        "openai-codex": provider_receipt("openai-codex", "chatgpt_personal"),
        "cursor": provider_receipt("cursor", "personal_api_key"),
    },
    "isolation": {name: True for name in REQUIRED_ISOLATION_CHECKS},
    "lease_lifecycle": {name: True for name in REQUIRED_LEASE_CHECKS},
    "hybrid_placement": {
        "host_classes": ["persistent", "ephemeral"],
        "same_deliverable": True,
        "explainable_decisions": True,
    },
    "capacity": {
        "states": ["ready", "provider_capacity_exhausted",
                   "waiting_for_plan_reset", "ready"],
        "bounded_probes": True,
        "retry_storm": False,
        "metered_fallback": False,
    },
    "teardown": {
        "aws_active_instances": 0,
        "persistent_host_registered": True,
        "all_provider_residue_purged": True,
        "unauthorized_metered_spend": 0,
    },
}

result = build_coord17_acceptance(evidence)
ok(result["passed"] and result["blocker_count"] == 0,
   "complete three-provider, isolation, quota, hybrid, and teardown proof passes")
ok(set(result["providers"]) == set(REQUIRED_PROVIDERS)
   and result["credential_values_redacted"] is True,
   "aggregate emits only the required provider summaries and redaction verdict")

missing_cursor = json.loads(json.dumps(evidence))
missing_cursor["providers"].pop("cursor")
missing_result = build_coord17_acceptance(missing_cursor)
ok(not missing_result["passed"]
   and any(item.startswith("providers.cursor.") for item in missing_result["blockers"]),
   "missing Cursor proof fails closed instead of accepting the Claude smoke")

pooled = json.loads(json.dumps(evidence))
pooled["isolation"]["account_pooling_denied"] = False
ok("isolation.account_pooling_denied" in build_coord17_acceptance(pooled)["blockers"],
   "account-pooling acceptance must be explicit")

wrong_sequence = json.loads(json.dumps(evidence))
wrong_sequence["capacity"]["states"] = ["ready", "waiting_for_plan_reset"]
ok("capacity.pause_wait_resume_sequence"
   in build_coord17_acceptance(wrong_sequence)["blockers"],
   "plan exhaustion cannot pass without exhausted -> waiting -> ready recovery")

secret = json.loads(json.dumps(evidence))
secret["providers"]["cursor"]["api_key"] = "must-never-aggregate"
secret_result = build_coord17_acceptance(secret)
ok(not secret_result["credential_values_redacted"]
   and any(item.startswith("forbidden_secret_key:") for item in secret_result["blockers"])
   and "must-never-aggregate" not in json.dumps(secret_result),
   "secret-shaped evidence is rejected without copying the secret into the result")

malformed_lists = json.loads(json.dumps(evidence))
malformed_lists["hybrid_placement"]["host_classes"] = [{"class": "persistent"}]
malformed_lists["capacity"]["states"] = False
malformed_result = build_coord17_acceptance(malformed_lists)
ok(not malformed_result["passed"]
   and "hybrid_placement.host_classes.shape" in malformed_result["blockers"]
   and "capacity.states.shape" in malformed_result["blockers"],
   "malformed list-shaped evidence fails closed without raising")

boolean_zero = json.loads(json.dumps(evidence))
boolean_zero["teardown"]["aws_active_instances"] = False
boolean_zero["teardown"]["unauthorized_metered_spend"] = False
boolean_zero_result = build_coord17_acceptance(boolean_zero)
ok("teardown.aws_scale_to_zero" in boolean_zero_result["blockers"]
   and "teardown.zero_unauthorized_metered_spend" in boolean_zero_result["blockers"],
   "boolean false cannot masquerade as numeric zero teardown evidence")

old_binding = os.environ.get("PM_CO_ACCOUNT_BINDING_JSON")
old_host = os.environ.get("PM_HOST_ID")
old_runner = os.environ.get("PM_RUNNER_SESSION_ID")
try:
    os.environ["PM_CO_ACCOUNT_BINDING_JSON"] = json.dumps({
        "tenant_id": "tenant-owner", "user_id": "user-owner",
        "project": "switchboard", "provider": "cursor",
        "provider_account_id": "cursor-account",
        "credential_reference": "provider-cred-cursor",
        "account_affinity_id": "affinity-cursor",
    })
    os.environ["PM_HOST_ID"] = "host-persistent-owner"
    os.environ["PM_RUNNER_SESSION_ID"] = "run-cursor-owner"
    lease = cursor_worker._lease_body(
        cursor_worker._binding(),
        {"task_id": "COORD-17", "managed": {"work_session_id": "ws-cursor"}},
    )
finally:
    if old_binding is None:
        os.environ.pop("PM_CO_ACCOUNT_BINDING_JSON", None)
    else:
        os.environ["PM_CO_ACCOUNT_BINDING_JSON"] = old_binding
    if old_host is None:
        os.environ.pop("PM_HOST_ID", None)
    else:
        os.environ["PM_HOST_ID"] = old_host
    if old_runner is None:
        os.environ.pop("PM_RUNNER_SESSION_ID", None)
    else:
        os.environ["PM_RUNNER_SESSION_ID"] = old_runner
ok(lease["provider"] == "cursor" and lease["provider_account_id"] == "cursor-account",
   "Cursor worker preserves the enrolled provider/account binding")

source = (ROOT / "adapters" / "cursor_personal_worker.py").read_text()
ok("CURSOR_API_KEY" in source
   and "provider-credential-leases" in source
   and "materialize-envelope" in source
   and "metered_fallback\": False" in source,
   "Cursor worker uses the fenced vault envelope with no metered fallback")
ok("OPENAI_API_KEY" in source and "ANTHROPIC_API_KEY" in source
   and "shutil.rmtree" in source and '"failed"' in source,
   "Cursor worker strips unrelated provider keys, purges residue, and terminalizes failures")

auth_source = (
    ROOT / "src" / "switchboard" / "integrations" / "provider_runtime_auth.py"
).read_text()
ok('(\"cursor-agent\", \"agent\")' in auth_source,
   "Cursor preflight supports both fleet and current local binary names")

old_values = {
    name: os.environ.get(name) for name in (
        "PM_CO_ACCOUNT_BINDING_JSON", "PM_HOST_ID", "PM_RUNNER_SESSION_ID",
        "PM_CO_WAKE_ID", "PM_AGENT_ID",
    )
}
real_register = cursor_worker._register_runner
real_http = cursor_worker.sb._http
runner_statuses = []
wake_results = []
try:
    os.environ.update({
        "PM_CO_ACCOUNT_BINDING_JSON": json.dumps({
            "tenant_id": "tenant-owner", "user_id": "user-owner",
            "project": "switchboard", "provider": "cursor",
            "provider_account_id": "cursor-account",
            "credential_reference": "provider-cred-cursor",
            "account_affinity_id": "affinity-cursor",
        }),
        "PM_HOST_ID": "host-cursor",
        "PM_RUNNER_SESSION_ID": "run-cursor",
        "PM_CO_WAKE_ID": "wake-cursor",
        "PM_AGENT_ID": "cursor-agent/COORD-17",
    })
    cursor_worker._register_runner = (
        lambda _task, _body, status: runner_statuses.append(status))

    def failed_lease_http(method, path, body=None, **_kwargs):
        if path.endswith("/leases"):
            return {}
        if path == "/txp/v1/complete_wake":
            wake_results.append(dict((body or {}).get("result") or {}))
        return {"completed": True}

    cursor_worker.sb._http = failed_lease_http
    try:
        cursor_worker.run({
            "task_id": "COORD-17", "claim_id": "claim-cursor",
            "managed": {"work_session_id": "ws-cursor", "workspace_path": str(ROOT)},
        })
        lease_failure_closed = False
    except RuntimeError:
        lease_failure_closed = True
finally:
    cursor_worker._register_runner = real_register
    cursor_worker.sb._http = real_http
    for name, value in old_values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
ok(lease_failure_closed and runner_statuses == ["running", "failed"]
   and wake_results and wake_results[-1].get("started") is False,
   "Cursor lease failure closes the wake and terminalizes the runner before process launch")

print(f"\nCOORD-17 three-provider BYOA: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
