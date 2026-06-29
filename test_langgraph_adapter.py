#!/usr/bin/env python3
"""Smoke tests for the LangGraph Switchboard adapter pack."""
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MOD_PATH = ROOT / "adapters" / "langgraph" / "langgraph_adapter.py"

spec = importlib.util.spec_from_file_location("langgraph_adapter", MOD_PATH)
langgraph_adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(langgraph_adapter)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


orig_evaluate = langgraph_adapter.sb.evaluate_tool
orig_heartbeat = langgraph_adapter.sb.heartbeat
orig_agent_id = os.environ.pop("PM_AGENT_ID", None)

try:
    os.environ["PM_LANGGRAPH_CONTROL_MODE"] = "hook_deny"
    adapter = langgraph_adapter.LangGraphSwitchboardAdapter(
        project="switchboard",
        graph_name="unit graph",
        run_id="run 1",
        agent_id="langgraph/unit",
    )
    adapter.report_progress = lambda **_kwargs: {"ok": True}

    ok(adapter.agent_id == "langgraph/unit", "explicit LangGraph agent id is stable")
    ok(adapter.control["tier"] == "T2" and adapter.control["deny"] == "langgraph_wrapper",
       "default LangGraph wrapper advertises hook-deny fidelity")

    langgraph_adapter.sb.heartbeat = lambda *a, **k: None
    langgraph_adapter.sb.evaluate_tool = lambda *a, **k: {"decision": "allow", "reason": ""}

    @adapter.wrap_node("plan")
    def plan_node(state):
        return {**state, "planned": True}

    result = plan_node({"task_id": "ADAPTER-4"})
    ok(result["planned"] is True, "wrapped node runs when Switchboard allows")

    langgraph_adapter.sb.evaluate_tool = lambda *a, **k: {
        "decision": "deny",
        "reason": "stop requested",
    }
    denied_ran = False

    @adapter.wrap_node("write")
    def denied_node(state):
        nonlocal_denied["ran"] = True
        return state

    nonlocal_denied = {"ran": False}
    try:
        denied_node({"task_id": "ADAPTER-4"})
        ok(False, "wrapped node raises on Switchboard deny")
    except langgraph_adapter.SwitchboardInterrupt as exc:
        ok(exc.verdict["reason"] == "stop requested" and not nonlocal_denied["ran"],
           "wrapped node halts before execution on deny")

    verdict = adapter.guard_node("observe", {"foo": "bar"}, raise_on_deny=False)
    ok(verdict["decision"] == "deny" and verdict["boundary"] == "node",
       "guard_node can return deny verdict without raising")

    evidence = langgraph_adapter.evidence_from_result({
        "switchboard_evidence": {"branch": "langgraph/demo", "head_sha": "abc123"},
        "other": "ignored",
    })
    ok(evidence["head_sha"] == "abc123", "evidence_from_result extracts graph evidence")

    invoked = langgraph_adapter.invoke_graph(lambda state: {"evidence": {"task": state["task"]["task_id"]}},
                                             {"task": {"task_id": "LG-1"}})
    ok(invoked["evidence"]["task"] == "LG-1", "invoke_graph supports plain callables")

    rc = langgraph_adapter.run_conformance(json_only=True)
    ok(rc == 0, "LangGraph adapter passes shared P0 conformance fixture")

    print("\n%d passed, %d failed" % (passed, failed))
    raise SystemExit(1 if failed else 0)
finally:
    langgraph_adapter.sb.evaluate_tool = orig_evaluate
    langgraph_adapter.sb.heartbeat = orig_heartbeat
    if orig_agent_id is not None:
        os.environ["PM_AGENT_ID"] = orig_agent_id
