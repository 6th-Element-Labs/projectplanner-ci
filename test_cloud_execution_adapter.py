#!/usr/bin/env python3
"""ADAPTER-17: executable cloud-execution adapter contract tests."""

from copy import deepcopy

from adapters.cloud_execution import (
    CANONICAL_REPO,
    REQUIRED_VENDORS,
    evaluate_trigger,
    load_contract,
    project_dev_status,
    refresh_session,
    validate_contract,
    validate_dispatch_envelope,
    validate_usage_receipt,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


contract = load_contract()
ok(not validate_contract(contract), "checked-in cloud contract validates")
ok({vendor["id"] for vendor in contract["vendors"]} == REQUIRED_VENDORS,
   "contract covers Claude, Codex, and Cursor cloud runtimes")
claude_contract = next(vendor for vendor in contract["vendors"]
                       if vendor["id"] == "claude-code-cloud")
ok(claude_contract["launch_operation"] == "claude --cloud <dev-brief>",
   "Claude contract uses the current cloud-launch command")

dispatch = {
    "project": "switchboard",
    "task_id": "ADAPTER-17",
    "claim_id": "claim-17",
    "wake_id": "wake-17",
    "dev_brief": "Read ADAPTER-17 from Switchboard, implement it, test, and open a PR.",
    "canonical_repo": CANONICAL_REPO,
    "branch": "codex/adapter-17-cloud-execution",
    "mcp_access": {
        "endpoint": "https://plan.taikunai.com/mcp",
        "token_ref": "vault://switchboard/task/ADAPTER-17",
        "scopes": ["read:task", "write:claim", "write:evidence"],
        "expires_at": 1783890000,
    },
}
ok(not validate_dispatch_envelope(dispatch), "valid dispatch envelope passes")
ok(validate_dispatch_envelope([]) == ["dispatch envelope must be an object"],
   "non-object dispatch envelope fails closed")

bad_repo = deepcopy(dispatch)
bad_repo["canonical_repo"] = "someone/fork"
ok("canonical_repo is not the configured code-truth repo" in validate_dispatch_envelope(bad_repo),
   "wrong repository fails closed")
for default_branch in ("main", "master"):
    bad_branch = deepcopy(dispatch)
    bad_branch["branch"] = default_branch
    ok(bool(validate_dispatch_envelope(bad_branch)),
       f"direct {default_branch} dispatch fails closed")
raw_token = deepcopy(dispatch)
raw_token["mcp_access"]["token"] = "secret-value"
ok("raw MCP tokens are forbidden in dispatch envelopes" in validate_dispatch_envelope(raw_token),
   "raw MCP token is rejected")
missing_expiry = deepcopy(dispatch)
missing_expiry["mcp_access"].pop("expires_at")
ok("mcp_access requires an expiry" in validate_dispatch_envelope(missing_expiry),
   "unbounded MCP credential is rejected")

for vendor in contract["vendors"]:
    vendor_id = vendor["id"]
    requirements = vendor["requirements"]
    if vendor["trigger_support"] == "unsupported":
        denied = evaluate_trigger(vendor_id, dispatch, requirements, 0, contract=contract)
        ok(denied["allowed"] is False and denied["reason"] == "provider_trigger_unsupported",
           f"{vendor_id} does not invent an undocumented cloud trigger")
        continue
    no_setup = evaluate_trigger(vendor_id, dispatch, (), 0, contract=contract)
    ok(no_setup["allowed"] is False and set(no_setup["missing"]) == set(requirements),
       f"{vendor_id} fails closed with no provider setup")
    for missing_requirement in requirements:
        partial = [item for item in requirements if item != missing_requirement]
        denied = evaluate_trigger(vendor_id, dispatch, partial, 0, contract=contract)
        ok(denied["allowed"] is False and missing_requirement in denied["missing"],
           f"{vendor_id} denies missing {missing_requirement}")
    ready = evaluate_trigger(vendor_id, dispatch, requirements, 0, contract=contract)
    ok(ready["allowed"] is True and ready["adopted"] is False
       and ready["dev_status"] == "queued",
       f"{vendor_id} trigger readiness is queued, not running")
    cap = vendor["concurrency"]["switchboard_default_cap"]
    capped = evaluate_trigger(vendor_id, dispatch, requirements, cap, contract=contract)
    ok(capped["allowed"] is False and capped["reason"] == "provider_concurrency_cap_reached",
       f"{vendor_id} honors Switchboard concurrency cap")
    api_error = evaluate_trigger(
        vendor_id, dispatch, requirements, 0,
        provider_result={"ok": False, "error": "provider unavailable"}, contract=contract)
    ok(api_error["allowed"] is False and api_error["reason"] == "vendor_api_error",
       f"{vendor_id} exposes provider errors")
    malformed = evaluate_trigger(
        vendor_id, dispatch, requirements, 0, provider_result=[], contract=contract)
    ok(malformed["allowed"] is False and malformed["reason"] == "provider_response_malformed",
       f"{vendor_id} rejects malformed provider responses")
    bad_count = evaluate_trigger(vendor_id, dispatch, requirements, "many", contract=contract)
    ok(bad_count["allowed"] is False and bad_count["reason"] == "active_session_count_invalid",
       f"{vendor_id} rejects malformed concurrency counts")

    session = vendor["session"]
    incomplete = evaluate_trigger(
        vendor_id, dispatch, requirements, 0,
        provider_result={"ok": True, session["id_field"]: "session-17"}, contract=contract)
    ok(incomplete["allowed"] is False and incomplete["reason"] == "adoption_receipt_incomplete",
       f"{vendor_id} requires app-visible session URL before adoption")
    adopted = evaluate_trigger(
        vendor_id, dispatch, requirements, 0,
        provider_result={
            "ok": True,
            session["id_field"]: "session-17",
            session["url_field"]: "https://vendor.example/session-17",
            "status": "running",
        },
        contract=contract,
    )
    ok(adopted["allowed"] is True and adopted["adopted"] is True
       and adopted["wake_id"] == "wake-17"
       and adopted["runner_session_id"].endswith("/session-17"),
       f"{vendor_id} binds complete session receipt to wake and runner session")
    queued_receipt = evaluate_trigger(
        vendor_id, dispatch, requirements, 0,
        provider_result={
            "ok": True,
            session["id_field"]: "session-queued",
            session["url_field"]: "https://vendor.example/session-queued",
            "status": "provisioning" if "provisioning" in vendor["status_map"] else "queued",
        },
        contract=contract,
    )
    ok(queued_receipt["allowed"] is True and queued_receipt["adopted"] is True
       and queued_receipt["dev_status"] == "queued",
       f"{vendor_id} keeps adopted provisioning session queued until it runs")
    expired = evaluate_trigger(
        vendor_id, dispatch, requirements, 0,
        provider_result={
            "ok": True,
            session["id_field"]: "session-17",
            session["url_field"]: "https://vendor.example/session-17",
            "status": "expired",
        },
        contract=contract,
    )
    ok(expired["allowed"] is False and expired["reason"] == "vendor_session_expired",
       f"{vendor_id} refuses expired session adoption")

ok(refresh_session("claude-code-cloud", {"status": "running"}, contract=contract)["dev_status"]
   == "running", "provider running readback projects to Dev running")
ok(refresh_session("cursor-background-agent", {"status": "lost"}, contract=contract)["reason"]
   == "vendor_session_lost", "lost cloud session is visibly failed")
ok(refresh_session("cursor-background-agent", {"status": "mystery"}, contract=contract)["reason"]
   == "provider_status_unknown", "unknown provider status fails closed")
ok(refresh_session("cursor-background-agent", [], contract=contract)["reason"]
   == "provider_response_malformed", "malformed status readback fails closed")

ok(project_dev_status(wake_status="pending") == "queued", "pending wake projects to queued")
ok(project_dev_status(wake_status="claimed") == "queued",
   "claimed wake stays queued until provider adoption")
ok(project_dev_status(session_active=True) == "running", "adopted session projects to running")
ok(project_dev_status(session_active=True, pr_url="https://github.com/org/repo/pull/1") == "pr",
   "PR provenance wins over running session state")
ok(project_dev_status(failed=True) == "failed", "failed provider state stays visible")

subscription = {
    "source": "agent_report",
    "confidence": "unknown",
    "billing_mode": "subscription",
    "cost_usd": 0,
    "task_id": "ADAPTER-17",
    "vendor_id": "claude-code-cloud",
}
ok(not validate_usage_receipt(subscription), "honest unknown subscription usage is accepted")
invented_cost = {**subscription, "confidence": "exact", "cost_usd": 4.25}
ok(len(validate_usage_receipt(invented_cost)) == 2,
   "fabricated exact subscription cost is rejected")
api_usage = {
    "source": "agent_report",
    "confidence": "reported",
    "billing_mode": "api_usage",
    "cost_usd": 1.25,
    "task_id": "ADAPTER-17",
    "vendor_id": "cursor-background-agent",
}
ok(not validate_usage_receipt(api_usage), "reported provider API usage is accepted")
ok(validate_usage_receipt([]) == ["usage receipt must be an object"],
   "malformed usage receipt fails closed")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
