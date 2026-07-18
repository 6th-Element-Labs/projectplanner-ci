#!/usr/bin/env python3
"""CO-16: personal + direct OpenAI API Codex conformance proof."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from path_setup import ROOT


TMP = Path(tempfile.mkdtemp(prefix="co16-codex-conformance-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_PROVIDER_VAULT_KEY"] = base64.urlsafe_b64encode(b"K" * 32).decode()
os.environ["PM_PROVIDER_VAULT_KEY_ID"] = "co16-test:v1"

import store  # noqa: E402
from switchboard.domain.provider_credentials import evaluate_codex_conformance  # noqa: E402
from switchboard.integrations.provider_runtime_auth import ProviderRuntimeAuth  # noqa: E402
from switchboard.storage.repositories.provider_capacity import (  # noqa: E402
    ProviderCapacityRepository,
)
from switchboard.storage.repositories.provider_credentials import (  # noqa: E402
    CredentialVaultError,
    default_provider_credential_repository as credential_repository,
)


PROJECT = "switchboard"
USER = "user-co16-owner"
TASK = "CO-16"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def execution(connection_id: str, host_class: str, suffix: str) -> dict:
    return {
        "native_cli": True,
        "cli_version": "codex-cli-test",
        "host_class": host_class,
        "mcp_registered": True,
        "scoped_read": True,
        "scoped_action": True,
        "cross_scope_denied": True,
        "residue_purged": True,
        "post_revoke_denied": True,
        "binding": {
            "task_id": TASK,
            "claim_id": f"claim-{suffix}",
            "work_session_id": f"worksession-{suffix}",
            "runner_session_id": f"runner-{suffix}",
            "host_id": f"host/{suffix}",
            "wake_id": f"wake-{suffix}",
            "source_sha": "a" * 40,
            "execution_connection_id": connection_id,
        },
    }


def surfaces(connection_id: str) -> dict:
    return {
        name: {"execution_connection_id": connection_id}
        for name in ("ui", "scheduler", "runner", "audit", "capacity", "error")
    }


def evidence() -> dict:
    personal = "execconn/personal-co16"
    api = "execconn/api-co16"
    return {
        "source_sha": "a" * 40,
        "rows": [
            {
                "provider": "openai-codex",
                "connection_kind": "personal_subscription",
                "execution_connection_id": personal,
                "billing_mode": "chatgpt_subscription",
                "metered": False,
                "api_key_fallback": False,
                "executions": [
                    execution(personal, "ephemeral", "personal-cloud"),
                    execution(personal, "persistent", "personal-host"),
                ],
                "surface_receipts": surfaces(personal),
            },
            {
                "provider": "openai-codex",
                "connection_kind": "direct_api",
                "execution_connection_id": api,
                "billing_mode": "openai_platform_api",
                "metered": True,
                "executions": [execution(api, "ephemeral", "api-cloud")],
                "surface_receipts": surfaces(api),
                "cost_receipt": {
                    "execution_connection_id": api,
                    "billing_account_fingerprint": "bill-co16-redacted",
                    "budget_id": "budget-co16",
                    "cost_usd": 0.001,
                },
            },
        ],
        "negative_proofs": {
            "personal_failure_did_not_activate_api": True,
            "api_failure_did_not_activate_personal": True,
            "cross_user_denied": True,
            "cross_project_denied": True,
        },
    }


try:
    result = evaluate_codex_conformance(evidence())
    ok(result["ok"] and result["finding_count"] == 0
       and {row["name"] for row in result["rows"]} == {"personal", "api"},
       "complete redacted two-row evidence passes the canonical evaluator")

    duplicate = evidence()
    duplicate["rows"][1]["execution_connection_id"] = "execconn/personal-co16"
    duplicate_result = evaluate_codex_conformance(duplicate)
    ok(not duplicate_result["ok"] and any(
        item["code"] == "execution_connections_not_distinct"
        for item in duplicate_result["findings"]),
       "personal and API rows must select distinct execution connections")

    extra = evidence()
    extra["rows"].append(dict(extra["rows"][0]))
    extra_result = evaluate_codex_conformance(extra)
    extra_codes = {item["code"] for item in extra_result["findings"]}
    ok({"conformance_row_count_invalid", "conformance_row_kind_duplicate"}.issubset(
        extra_codes), "the conformance matrix is exactly two uniquely typed rows")

    malformed = evidence()
    malformed["rows"].append("not-a-conformance-row")
    malformed_result = evaluate_codex_conformance(malformed)
    ok(not malformed_result["ok"] and any(
        item["code"] == "conformance_rows_malformed"
        for item in malformed_result["findings"]),
       "non-object evidence rows are rejected instead of silently discarded")

    wrong_source = evidence()
    wrong_source["rows"][0]["executions"][0]["binding"]["source_sha"] = "b" * 40
    wrong_source_result = evaluate_codex_conformance(wrong_source)
    ok(not wrong_source_result["ok"] and any(
        item["code"] == "native_execution_proof_incomplete"
        for item in wrong_source_result["findings"]),
       "execution receipts are bound to the exact matrix source SHA")

    unsafe = evidence()
    unsafe["api_key"] = "must-never-enter-conformance-evidence"
    unsafe["rows"][1]["cost_receipt"] = {}
    unsafe_result = evaluate_codex_conformance(unsafe)
    codes = {item["code"] for item in unsafe_result["findings"]}
    ok(not unsafe_result["ok"]
       and {"secret_shaped_evidence_denied", "api_cost_receipt_incomplete"}.issubset(codes)
       and "must-never-enter" not in json.dumps(unsafe_result),
       "secret-shaped evidence and an unattributed API smoke cost fail closed")

    invalid_costs_denied = True
    for invalid_cost in (True, "NaN", "Infinity", -1, 0):
        invalid_cost_evidence = evidence()
        invalid_cost_evidence["rows"][1]["cost_receipt"]["cost_usd"] = invalid_cost
        invalid_costs_denied = invalid_costs_denied and not evaluate_codex_conformance(
            invalid_cost_evidence)["ok"]
    ok(invalid_costs_denied,
       "API cost proof requires a finite positive number and rejects booleans")

    runtime = ProviderRuntimeAuth(
        runtime_parent=TMP / "runtime",
        base_environment={
            "PATH": os.environ.get("PATH", ""),
            "OPENAI_API_KEY": "wrong-inherited-key",
            "CODEX_ACCESS_TOKEN": "wrong-inherited-token",
        },
    )
    personal_root = runtime._runtime_root("openai-codex")
    personal_env, _ = runtime._materialize(
        "openai-codex", '{"tokens":{"access_token":"opaque-test"}}',
        personal_root, auth_mode="chatgpt_subscription")
    api_root = runtime._runtime_root("openai-codex")
    api_env, _ = runtime._materialize(
        "openai-codex", "sk-co16-not-real", api_root, auth_mode="api_key")
    ok("CODEX_HOME" in personal_env and "OPENAI_API_KEY" not in personal_env
       and api_env.get("OPENAI_API_KEY") == "sk-co16-not-real"
       and "CODEX_HOME" not in api_env and "CODEX_ACCESS_TOKEN" not in api_env,
       "native Codex materialization keeps personal capsule and API key modes separate")
    shutil.rmtree(runtime.runtime_parent, ignore_errors=True)

    store.ensure_org(store.DEFAULT_ORG_ID, "6th Element Labs", created_by="co16-test")
    store.set_project_access(
        PROJECT, store.DEFAULT_ORG_ID, purpose="CO-16 fixture", created_by="co16-test")
    store.init_db(PROJECT)
    store.ensure_user(USER, "co16@example.test", "CO-16", created_by="co16-test")
    store.add_org_member(store.DEFAULT_ORG_ID, USER, role="member", created_by="co16-test")
    missing_connection_receipt = runtime.run(
        {
            "project": PROJECT,
            "credential_reference": "provider-cred-missing-co16",
            "execution_connection_id": "provider-cred-missing-co16",
            "connection_kind": "direct_api",
            "user_id": USER,
            "provider": "openai-codex",
            "provider_account_id": "co16-api-missing",
            "task_id": TASK,
            "claim_id": "claim-co16-missing",
            "host_id": "host/co16-missing",
            "runner_session_id": "runner-co16-missing",
            "work_session_id": "worksession-co16-missing",
            "wake_id": "wake-co16-missing",
        },
        lease_id="",
        principal={"principal_id": "agent-co16-test", "principal_kind": "agent"},
        actor="co16-test",
        command=["codex", "--version"],
    )
    ok(not missing_connection_receipt["allowed"]
       and missing_connection_receipt["execution_connection_id"]
       == "provider-cred-missing-co16"
       and missing_connection_receipt["connection_kind"] == "direct_api"
       and missing_connection_receipt["task_id"] == TASK,
       "early API denial receipts preserve the selected connection and task binding")
    invalid_command_receipt = runtime.run(
        {
            "execution_connection_id": "execconn/api-invalid-command",
            "connection_kind": "direct_api",
            "provider": "openai-codex",
            "task_id": TASK,
        },
        lease_id="",
        principal={"principal_id": "agent-co16-test", "principal_kind": "agent"},
        actor="co16-test",
        command=[],
    )
    ok(not invalid_command_receipt["allowed"]
       and invalid_command_receipt["error_code"] == "provider_runtime_command_invalid"
       and invalid_command_receipt["execution_connection_id"]
       == "execconn/api-invalid-command"
       and invalid_command_receipt["connection_kind"] == "direct_api",
       "pre-launch API errors retain the selected execution connection")
    personal_connection = credential_repository.enroll(
        project=PROJECT, user_id=USER, provider="openai-codex",
        provider_account_id="co16-personal", auth_type="oauth_capsule",
        credential='{"tokens":{"access_token":"opaque-test"}}',
        project_allowlist=[PROJECT], actor="co16-test",
        concurrency_policy={"mode": "exclusive", "max_parallel": 1},
    )
    api_connection = credential_repository.enroll(
        project=PROJECT, user_id=USER, provider="openai-codex",
        provider_account_id="co16-api", auth_type="api_key",
        credential="sk-co16-not-real", project_allowlist=[PROJECT],
        actor="co16-test", connection_kind="direct_api",
        billing_account_id="billing-co16",
        budget_policy={"budget_id": "budget-co16", "currency": "USD", "ceiling": 5},
        concurrency_policy={"mode": "bounded", "max_parallel": 2},
    )
    binding = {
        "project": PROJECT,
        "credential_reference": api_connection["credential_reference"],
        "execution_connection_id": api_connection["execution_connection_id"],
        "user_id": USER,
        "provider": "openai-codex",
        "provider_account_id": "co16-api",
        "task_id": TASK,
        "host_id": "host/i-co16-api",
        "host_placement": {"host_class": "ephemeral", "bound_wake_id": "wake-co16"},
    }
    lane_policy = {
        "lane_kind": "api", "enabled": True,
        "personal_credential_reference": personal_connection["credential_reference"],
        "metered_credential_reference": api_connection["credential_reference"],
        "audited_opt_in": {
            "enabled": True, "actor": USER, "audit_id": "co16-api-smoke",
            "approved_at": time.time(),
        },
        "budget_ceiling": 5,
        "cost_attribution": {
            "budget_id": "budget-co16", "cost_center": "CO", "currency": "USD",
        },
    }
    decision = ProviderCapacityRepository().admission_decision(
        binding,
        task_policy={"customer_user_id": USER, "requested_provider": "codex"},
        lane_policy=lane_policy, require_execution_binding=False)
    ok(decision["allowed"] and decision["metered"]
       and decision["execution_connection_id"] == api_connection["execution_connection_id"]
       and decision["connection_kind"] == "direct_api",
       "scheduler/capacity receipt preserves the exact direct API execution connection")
    try:
        ProviderCapacityRepository().admission_decision(
            {**binding, "execution_connection_id": "execconn/wrong"},
            task_policy={"customer_user_id": USER, "requested_provider": "codex"},
            lane_policy=lane_policy, require_execution_binding=False)
        mismatch_denied = False
    except CredentialVaultError as exc:
        mismatch_denied = exc.code == "provider_capacity_binding_mismatch"
    ok(mismatch_denied,
       "capacity admission denies a credential/execution-connection mismatch")

    personal_worker = (ROOT / "adapters" / "codex_personal_worker.py").read_text()
    settings = (ROOT / "static" / "js" / "settings.js").read_text()
    ok('auth_mode="chatgpt_subscription"' in personal_worker
       and 'expected_auth_mode="chatgpt_subscription"' in personal_worker
       and '"execution_connection_id"' in personal_worker,
       "personal vault worker selects ChatGPT auth explicitly and binds the connection id")
    ok("_settingsAiAccountApiConnectionRows" in settings
       and "execution_connection_id" in settings
       and "billing_account_fingerprint" in settings
       and "no personal fallback" in settings,
       "Settings readback identifies the API execution connection, billing, budget, and no-fallback state")

    evidence_path = TMP / "evidence.json"
    evidence_path.write_text(json.dumps(evidence()), encoding="utf-8")
    cli = subprocess.run(
        [sys.executable,
         str(ROOT / "scripts" / "co16_codex_conformance.py"), str(evidence_path)],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    ok(cli.returncode == 0
       and json.loads(cli.stdout)["schema"] == "switchboard.codex_execution_conformance.v1",
       "operator CLI emits the canonical machine-checkable matrix")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nCO-16 Codex execution conformance: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
