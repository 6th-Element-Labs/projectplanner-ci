#!/usr/bin/env python3
"""UI-12: the LiteLLM gateway callback translates a success event into a
`/tally/v1/spend/ingest` body — attribution, provider-actual defaults, idempotency key."""
import datetime
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "tally_callback", os.path.join(_HERE, "deploy", "gateway", "tally_callback.py"))
tally_callback = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tally_callback)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _resp(**over):
    base = {"id": "chatcmpl-provider-123", "model": "gpt-4o-mini",
            "usage": {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125}}
    base.update(over)
    return base


# 1. A tagged narrator call → task-attributed, provider-actual, gateway-measured cost.
start = datetime.datetime(2026, 7, 11, 12, 0, 0)
end = datetime.datetime(2026, 7, 11, 12, 0, 1)  # 1000 ms
kwargs = {
    "litellm_call_id": "call-abc",
    "model": "gpt-4o-mini",
    "response_cost": 0.0004,
    "custom_llm_provider": "openai",
    "litellm_params": {"metadata": {"source": "narrator", "task_id": "UI-12",
                                    "project": "switchboard", "agent_id": "narrate"}},
}
p = tally_callback.build_spend_payload(kwargs, _resp(), start, end)
ok(p["request_id"] == "call-abc", "request_id uses the stable LiteLLM call id")
ok(p["source"] == "narrator", "source threaded from metadata")
ok(p["task_id"] == "UI-12" and p["project"] == "switchboard", "task_id/project attributed")
ok(p["agent_id"] == "narrate", "agent_id attributed")
ok(p["confidence"] == "provider_actual", "gateway spend defaults to provider_actual confidence")
ok(p["provider"] == "openai" and p["model"] == "gpt-4o-mini", "provider + model captured")
ok(p["prompt_tokens"] == 100 and p["completion_tokens"] == 25 and p["total_tokens"] == 125,
   "token counts captured from usage")
ok(abs(p["cost_usd"] - 0.0004) < 1e-9, "provider-actual cost captured from response_cost")
ok(p["latency_ms"] == 1000.0, "latency computed from start/end")
ok(p["runtime"] == "litellm-gateway", "runtime tags the gateway")

# 2. Untagged call still lands, attributed to the gateway (nothing silently dropped).
p2 = tally_callback.build_spend_payload({"litellm_call_id": "call-xyz"}, _resp())
ok(p2["source"] == "gateway", "untagged call defaults source=gateway")
ok(p2["project"] == tally_callback.DEFAULT_PROJECT, "untagged call falls back to default project")
ok("task_id" not in p2, "untagged call carries no task attribution")
ok(p2["cost_usd"] == 0.0, "missing response_cost degrades to 0 without raising")

# 3. total_tokens derived when the provider omits it.
p3 = tally_callback.build_spend_payload(
    {"litellm_call_id": "c"},
    _resp(usage={"prompt_tokens": 10, "completion_tokens": 5}))
ok(p3["total_tokens"] == 15, "total_tokens derived from prompt+completion when absent")

# 4. request_id falls back to the provider response id when call id is missing.
p4 = tally_callback.build_spend_payload({}, _resp())
ok(p4["request_id"] == "chatcmpl-provider-123", "request_id falls back to response id")

# 5. Extra caller tags are preserved under metadata, off the first-class columns.
p5 = tally_callback.build_spend_payload(
    {"litellm_call_id": "c", "litellm_params": {"metadata": {"source": "agent", "cycle": "triage"}}},
    _resp())
ok(p5.get("metadata", {}).get("cycle") == "triage", "unknown caller tags preserved in metadata")
ok("source" not in p5.get("metadata", {}), "first-class keys not duplicated into metadata")

# 6. REGRESSION: the LiteLLM proxy injects non-serializable internal objects and
# sensitive key hashes into litellm_params.metadata. The payload must stay
# JSON-serializable and must not leak any of it (this dropped every spend row
# on prod: "Object of type UserAPIKeyAuth is not JSON serializable").
class _FakeUserAPIKeyAuth:  # opaque, non-JSON-serializable — mimics the proxy object
    def __init__(self): self.token = "sk-secret"

p6 = tally_callback.build_spend_payload(
    {"litellm_call_id": "c",
     "litellm_params": {"metadata": {
         "source": "narrator", "task_id": "UI-12",
         "user_api_key": _FakeUserAPIKeyAuth(),          # opaque object
         "user_api_key_hash": "hash-abc",                 # sensitive scalar
         "spend_logs_metadata": {"nested": "obj"},        # non-scalar
         "note": "keep-me"}}},                            # legit caller scalar
    _resp())
try:
    json.dumps(p6)
    serializable = True
except TypeError:
    serializable = False
ok(serializable, "payload is JSON-serializable even when proxy injects opaque objects")
md = p6.get("metadata", {})
ok("user_api_key" not in md, "opaque proxy object dropped from metadata")
ok("user_api_key_hash" not in md, "sensitive proxy key hash not leaked into metadata")
ok("spend_logs_metadata" not in md, "non-scalar proxy metadata dropped")
ok(md.get("note") == "keep-me", "legit scalar caller tag still preserved")
ok(p6["task_id"] == "UI-12" and p6["source"] == "narrator", "first-class attribution intact")

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
