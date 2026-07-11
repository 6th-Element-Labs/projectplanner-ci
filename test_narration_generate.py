#!/usr/bin/env python3
"""NARRATE-12: generation policy, deterministic summaries, receipts, and budgets.

Proves the M3 exit criteria (no network — the provider is injected):
- golden_templates: a routine status-only transition uses a deterministic template with an EXACT
  golden string and ZERO LLM charge (the injected provider is never called);
- a material narrative change (and the first narration) uses the LLM and records tokens/cost;
- failure_fallback_receipts: provider outage/timeout, malformed response, and exhausted budget each
  produce an explicit visible fallback narration, and the failed LLM receipt is preserved, never
  overwritten or hidden;
- cost_cap_test: a per-project cost ceiling is configurable and, once exceeded, forces fallback
  with no further LLM call.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="narrate-gen-test-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import narration_generate as gen  # noqa: E402
import store  # noqa: E402

PROJECT = store.DEFAULT_PROJECT
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


class FakeLLM:
    """Injected provider: counts calls, returns canned prose + cost, or a scripted failure."""
    def __init__(self, text="A crisp CEO paragraph about the feature.", cost=0.01,
                 raise_exc=None, empty=False):
        self.calls = 0
        self.text = text
        self.cost = cost
        self.raise_exc = raise_exc
        self.empty = empty

    def __call__(self, prompt, *, model, prompt_version, max_tokens):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return {"text": "" if self.empty else self.text, "model": model,
                "tokens_in": 120, "tokens_out": 60, "cost_usd": self.cost}


def event_for(task_id, rev):
    return {"project": PROJECT, "entity_type": "task", "entity_id": task_id,
            "event_id": f"nrq-evt-{task_id}-{rev}", "source_revision": rev,
            "source_hash": "sha256:" + ("0" * 64)}


try:
    store.init_db(PROJECT)
    t = store.create_task({"workstream_id": "GEN", "title": "Ship the widget",
                           "description": "Build the thing", "status": "In Progress"},
                          actor="test", project=PROJECT)
    tid = t["task_id"]

    # 1. first narration = material change -> LLM, cost recorded.
    llm = FakeLLM(cost=0.02)
    r1 = gen.generate(event_for(tid, 1), llm_fn=llm)
    ok(r1["mode"] == "llm" and r1["outcome"] == "delivered" and llm.calls == 1
       and r1["cost_usd"] == 0.02 and r1["prompt_version"] == gen.PROMPT_VERSION,
       "first narration uses the LLM and records model/prompt-version/cost")

    # 2. golden_templates: a status-only change -> deterministic template, EXACT text, no LLM call.
    store.update_task(tid, {"status": "In Review"}, actor="test", project=PROJECT)
    llm2 = FakeLLM()
    r2 = gen.generate(event_for(tid, 2), llm_fn=llm2)
    ok(r2["mode"] == "deterministic" and r2["outcome"] == "delivered" and llm2.calls == 0
       and r2["cost_usd"] == 0.0,
       "a routine status-only transition uses a deterministic template with zero LLM charge")
    ok(r2["narration"] == "**Ship the widget** is now _In Review_.",
       "deterministic template matches the exact golden string")

    # 3. a material change (description) -> LLM again.
    store.update_task(tid, {"description": "Build the thing, now with telemetry"},
                      actor="test", project=PROJECT)
    llm3 = FakeLLM(cost=0.03)
    r3 = gen.generate(event_for(tid, 3), llm_fn=llm3)
    ok(r3["mode"] == "llm" and llm3.calls == 1,
       "a material narrative change re-invokes the LLM")

    # 4. failure_fallback_receipts: provider outage -> explicit fallback, failed receipt preserved.
    receipts_before = len(gen.list_receipts(PROJECT, entity_id=tid))
    store.update_task(tid, {"description": "Build it with even more telemetry"},
                      actor="test", project=PROJECT)
    outage = FakeLLM(raise_exc=TimeoutError("provider down"))
    r4 = gen.generate(event_for(tid, 4), llm_fn=outage)
    ok(r4["mode"] == "fallback" and r4["outcome"] == "fallback"
       and r4["fallback_reason"].startswith("provider_error")
       and "temporarily unavailable" in (r4["narration"] or ""),
       "a provider outage yields an explicit visible fallback narration")
    ok(len(gen.list_receipts(PROJECT, entity_id=tid)) == receipts_before + 1
       and r3["receipt_id"] in [x["id"] for x in gen.list_receipts(PROJECT, entity_id=tid)],
       "the fallback is a new receipt row; the prior delivered receipt is not overwritten")

    # 5. malformed response -> mode=llm outcome=error, cost preserved, explicit fallback text.
    store.update_task(tid, {"description": "Telemetry v3"}, actor="test", project=PROJECT)
    malformed = FakeLLM(cost=0.015, empty=True)
    r5 = gen.generate(event_for(tid, 5), llm_fn=malformed)
    ok(r5["mode"] == "llm" and r5["outcome"] == "error"
       and r5["fallback_reason"] == "malformed_response" and r5["cost_usd"] == 0.015
       and "temporarily unavailable" in (r5["narration"] or ""),
       "a malformed response records the failed LLM receipt (cost preserved), not a hidden success")

    # 6. cost_cap_test: a per-project ceiling forces fallback with no further LLM call.
    store.set_meta("narration_generation_config",
                   {"daily_cost_usd": 0.001, "model": "taikun-summarize"}, project=PROJECT)
    store.update_task(tid, {"description": "Telemetry v4 over budget"}, actor="test", project=PROJECT)
    capped = FakeLLM(cost=0.02)
    r6 = gen.generate(event_for(tid, 6), llm_fn=capped)
    ok(r6["mode"] == "fallback" and r6["fallback_reason"] == "budget_exhausted"
       and capped.calls == 0 and r6["cost_usd"] == 0.0,
       "an exhausted per-project cost ceiling forces fallback with no LLM call")

    # 7. raising the ceiling restores LLM generation (configurable per project).
    store.set_meta("narration_generation_config",
                   {"daily_cost_usd": 100.0, "model": "taikun-summarize"}, project=PROJECT)
    store.update_task(tid, {"description": "Telemetry v5 within budget"}, actor="test", project=PROJECT)
    within = FakeLLM(cost=0.02)
    r7 = gen.generate(event_for(tid, 7), llm_fn=within)
    ok(r7["mode"] == "llm" and within.calls == 1,
       "raising the per-project ceiling re-enables LLM generation")

    # 7b. malformed per-project config must NOT crash generate (it would loop the worker with no
    #     receipt). A null/non-numeric ceiling falls back to the default; generate still records.
    store.set_meta("narration_generation_config",
                   {"daily_cost_usd": None, "window_seconds": "oops"}, project=PROJECT)
    store.update_task(tid, {"description": "Telemetry v6 bad config"}, actor="test", project=PROJECT)
    safe = FakeLLM(cost=0.02)
    try:
        r7b = gen.generate(event_for(tid, 8), llm_fn=safe)
        crashed = False
    except Exception:
        r7b = None
        crashed = True
    ok(not crashed and r7b is not None and r7b.get("receipt_id"),
       "malformed per-project config falls back to defaults; generate never raises and records")
    store.set_meta("narration_generation_config", {"daily_cost_usd": 100.0}, project=PROJECT)

    # 7c. a deliverable milestone status-only flip is a routine transition (deterministic, no LLM).
    dv = store.create_deliverable({"id": "gen-deliv", "title": "Gen deliverable",
                                   "status": "in_progress"}, actor="test", project=PROJECT)
    did = dv["id"] if isinstance(dv, dict) and dv.get("id") else "gen-deliv"
    store.add_deliverable_milestone(did, {"title": "MS1", "status": "in_progress"},
                                    actor="test", project=PROJECT)
    dev = {"project": PROJECT, "entity_type": "deliverable", "entity_id": did,
           "event_id": "nrq-d-1", "source_revision": 1, "source_hash": "sha256:" + ("0" * 64)}
    first_d = gen.generate(dev, llm_fn=FakeLLM(cost=0.02))  # first narration -> LLM baseline
    store.add_deliverable_milestone(did, {"id": did + ":ms1", "title": "MS1", "status": "done"},
                                    actor="test", project=PROJECT)  # milestone status-only flip
    llm_ms = FakeLLM()
    dev2 = dict(dev, event_id="nrq-d-2", source_revision=2)
    r_ms = gen.generate(dev2, llm_fn=llm_ms)
    ok(first_d["mode"] == "llm" and r_ms["mode"] == "deterministic" and llm_ms.calls == 0,
       "a milestone status-only flip is deterministic (no LLM), consistent with linked-task status")

    # 8. every receipt carries the full audit fields and is append-only (no overwrites).
    all_r = gen.list_receipts(PROJECT, entity_id=tid)
    ok(len(all_r) >= 7 and all(x["source_hash"] is not None and x["created_at"] for x in all_r)
       and all(x["mode"] in {"deterministic", "llm", "fallback"} for x in all_r),
       "every attempt is an append-only receipt with source hash, mode, and timestamp")

except Exception as exc:  # pragma: no cover
    import traceback
    traceback.print_exc()
    ok(False, f"unexpected exception: {exc}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
