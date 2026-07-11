#!/usr/bin/env python3
"""UI-12: gateway-ingested spend rolls onto the task Economics panel with a
per-source confidence breakdown and a per-model mix."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="spend-mix-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_db(P)
    task = store.create_task({"workstream_id": "UI", "title": "real cost"}, project=P)
    tid = task["task_id"]

    # Two gateway-measured calls on different models + one agent self-report.
    store.report_usage(source="gateway", confidence="provider_actual", task_id=tid,
                       provider="openai", model="gpt-4o-mini", request_id="r1",
                       prompt_tokens=100, completion_tokens=20, cost_usd=0.0004, project=P)
    store.report_usage(source="narrator", confidence="provider_actual", task_id=tid,
                       provider="openai", model="gpt-4o-mini", request_id="r2",
                       prompt_tokens=200, completion_tokens=30, cost_usd=0.0009, project=P)
    store.report_usage(source="agent", confidence="provider_actual", task_id=tid,
                       provider="anthropic", model="claude-haiku-4-5", request_id="r3",
                       prompt_tokens=50, completion_tokens=10, cost_usd=0.0002, project=P)

    # Idempotency: replaying the same request_id must not double-count.
    store.report_usage(source="gateway", confidence="provider_actual", task_id=tid,
                       provider="openai", model="gpt-4o-mini", request_id="r1",
                       prompt_tokens=100, completion_tokens=20, cost_usd=0.0004, project=P)

    tally = store.task_tally(tid, project=P)
    spend = tally["spend"]
    ok(abs(spend["cost_usd"] - 0.0015) < 1e-9, "total spend sums real gateway cost (idempotent on request_id)")
    ok(spend["total_tokens"] == 410, "total tokens summed once per request_id")

    by_source = spend["by_source"]
    ok(set(by_source) == {"gateway", "narrator", "agent"}, "spend bucketed by source")
    ok(by_source["gateway"]["confidence"] == "provider_actual",
       "gateway bucket carries provider_actual confidence (drives the badge)")

    by_model = spend.get("by_model") or {}
    ok(set(by_model) == {"gpt-4o-mini", "claude-haiku-4-5"}, "spend bucketed by model for the model-mix line")
    ok(abs(by_model["gpt-4o-mini"]["cost_usd"] - 0.0013) < 1e-9, "per-model cost aggregates the two gpt calls")
    ok(by_model["claude-haiku-4-5"]["total_tokens"] == 60, "per-model tokens aggregate correctly")

    # by_model propagates through the deliverable/mission rollup merge.
    deliv = store.create_deliverable({"id": "real-cost-deliv", "title": "Real cost"}, project=P)
    store.link_task_to_deliverable(deliv["id"], P, tid, project=P)
    dtally = store.deliverable_tally(deliv["id"], project=P)
    combined = (dtally.get("totals") or {}).get("combined") or {}
    merged_models = (combined.get("spend") or {}).get("by_model") or {}
    ok("gpt-4o-mini" in merged_models, "deliverable rollup carries the model mix")
except Exception as exc:
    import traceback
    traceback.print_exc()
    failed += 1
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
