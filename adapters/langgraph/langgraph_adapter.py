#!/usr/bin/env python3
"""LangGraph runtime adapter for Switchboard (ADAPTER-4).

LangGraph is an in-process graph/workflow runtime. Switchboard is the cross-runtime
coordination plane. This adapter keeps that boundary crisp: LangGraph owns graph state and
node execution; Switchboard owns presence, inbox, leases, claim_next, interrupts, progress,
completion evidence, and cost/outcome reporting.

No hard LangGraph dependency is required to import this file. Use the wrappers around your
LangGraph nodes/tools, or use run_claim_loop() to let Switchboard claim work before invoking a
compiled graph.
"""
from __future__ import annotations

import argparse
import functools
import inspect
import json
import os
import re
import sys
import time
import urllib.parse
from typing import Any, Callable, Dict, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import switchboard_core as sb  # noqa: E402

PROJECT = os.environ.get("PM_PROJECT", "switchboard")
RUNTIME = "langgraph"


class SwitchboardInterrupt(RuntimeError):
    """Raised when Switchboard denies a LangGraph node/tool boundary."""

    def __init__(self, verdict: Dict[str, Any]):
        self.verdict = verdict
        super().__init__(verdict.get("reason") or "Switchboard interrupted this graph boundary")


def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", (value or "").strip()).strip("-")
    return value or "run"


def _split_csv(value: Any) -> Optional[list[str]]:
    if not value:
        return None
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value).replace("\n", ",").split(",") if x.strip()]


def _brief(value: Any, limit: int = 1200) -> Any:
    """Return a JSON-friendly, bounded summary for progress/evidence payloads."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:20]:
            if k in {"messages", "prompt", "completion", "content"}:
                out[k] = f"<{k}:{len(str(v))} chars>"
            else:
                out[k] = _brief(v, max(120, limit // 4))
        return out
    if isinstance(value, (list, tuple)):
        return [_brief(x, max(120, limit // 4)) for x in list(value)[:10]]
    text = repr(value)
    return text[:limit] + ("..." if len(text) > limit else "")


def langgraph_agent_id(graph_name: str = "", run_id: str = "") -> str:
    if os.environ.get("PM_AGENT_ID"):
        return os.environ["PM_AGENT_ID"]
    run = run_id or os.environ.get("PM_LANGGRAPH_RUN_ID") or os.environ.get("LANGGRAPH_RUN_ID")
    graph = graph_name or os.environ.get("PM_LANGGRAPH_GRAPH") or "graph"
    return f"langgraph/{_slug(graph)}-{_slug(run or str(int(time.time())))}"


def control_fidelity(mode: str = "") -> Dict[str, Any]:
    """Truthfully advertise how this LangGraph process is governed."""
    mode = (mode or os.environ.get("PM_LANGGRAPH_CONTROL_MODE") or "hook_deny").strip().lower()
    runner_kill = bool(os.environ.get("PM_RUNNER_SESSION_ID"))
    if mode in {"observe", "observe_only", "advisory", "advisory_poll", "poll"}:
        return {
            "tier": "T1",
            "mode": "advisory_poll",
            "discover": "rest_or_mcp",
            "interrupt": "advisory_poll",
            "deny": "not_enforced",
            "kill": "runner" if runner_kill else "none",
            "verified": False,
        }
    return {
        "tier": "T2",
        "mode": "hook_deny",
        "discover": "rest_or_mcp",
        "interrupt": "node_or_tool_boundary",
        "deny": "langgraph_wrapper",
        "kill": "runner" if runner_kill else "none",
        "verified": True,
    }


class LangGraphSwitchboardAdapter:
    def __init__(self, project: str = PROJECT, graph_name: str = "", run_id: str = "",
                 agent_id: str = "", lane: str = "", model: str = "",
                 base: str = "", token: str = "", control_mode: str = ""):
        self.project = project
        self.graph_name = graph_name or os.environ.get("PM_LANGGRAPH_GRAPH", "graph")
        self.run_id = run_id or os.environ.get("PM_LANGGRAPH_RUN_ID", "")
        self.agent_id = agent_id or langgraph_agent_id(self.graph_name, self.run_id)
        self.lane = lane or os.environ.get("PM_LANE", "")
        self.model = model or os.environ.get("PM_AGENT_MODEL", "")
        self.base = base or None
        self.token = token or None
        self.control = control_fidelity(control_mode)
        self.current_claim_id = ""
        self.current_task_id = ""

    def on_graph_start(self, ack_inbox: bool = False) -> Dict[str, Any]:
        agreement = sb.handshake(
            self.project,
            self.agent_id,
            RUNTIME,
            base=self.base,
            token=self.token,
            model=self.model,
            lane=self.lane,
            control=self.control,
        )
        messages = self.inbox(ack=ack_inbox)
        text = agreement.get("text") if isinstance(agreement, dict) else None
        if not text and isinstance(agreement, dict):
            text = json.dumps(agreement, indent=2, sort_keys=True)
        return {
            "event": "graph_start",
            "project": self.project,
            "runtime": RUNTIME,
            "agent_id": self.agent_id,
            "graph_name": self.graph_name,
            "run_id": self.run_id,
            "control": self.control,
            "unacked_messages": messages,
            "additional_context": (
                f"## Switchboard working agreement - project '{self.project}'\n\n"
                f"Registered as `{self.agent_id}` for LangGraph graph `{self.graph_name}`. "
                f"Control fidelity: {self.control['tier']} ({self.control['mode']}).\n\n"
                f"{text or '(working agreement unavailable; fail-open)'}"
            ),
        }

    def inbox(self, ack: bool = False) -> list[Dict[str, Any]]:
        q = urllib.parse.quote(self.agent_id, safe="")
        try:
            r = sb._http("GET", f"/ixp/v1/inbox?project={self.project}&to_agent={q}&unacked=true",
                         base=self.base, token=self.token)
            messages = r.get("messages") or []
        except Exception:
            return []
        if ack:
            for m in messages:
                if m.get("signal") not in {"stop", "redirect", "claim_revoked"}:
                    self.ack_message(m.get("id"), "seen by LangGraph adapter")
        return messages

    def ack_message(self, message_id: Any, response: str = "") -> Optional[Dict[str, Any]]:
        if not message_id:
            return None
        try:
            return sb._http("POST", "/ixp/v1/ack",
                            {"project": self.project, "message_id": message_id,
                             "response": response},
                            base=self.base, token=self.token)
        except Exception:
            return None

    def guard_node(self, node_name: str, state: Any = None,
                   raise_on_deny: bool = True) -> Dict[str, Any]:
        sb.heartbeat(self.project, self.agent_id, base=self.base, token=self.token)
        state_keys = list(state.keys())[:40] if isinstance(state, dict) else []
        payload = {
            "runtime": RUNTIME,
            "graph_name": self.graph_name,
            "run_id": self.run_id,
            "boundary": "node",
            "node": node_name,
            "task_id": self.current_task_id or (state.get("task_id") if isinstance(state, dict) else ""),
            "state_keys": state_keys,
        }
        verdict = sb.evaluate_tool(
            self.project,
            self.agent_id,
            "LangGraphNode",
            payload,
            base=self.base,
            token=self.token,
        )
        verdict.update({"boundary": "node", "node": node_name, "agent_id": self.agent_id})
        if verdict.get("decision") == "deny" and raise_on_deny:
            raise SwitchboardInterrupt(verdict)
        return verdict

    def guard_tool(self, tool_name: str, tool_args: Optional[Dict[str, Any]] = None,
                   cwd: str = "", raise_on_deny: bool = True) -> Dict[str, Any]:
        verdict = sb.evaluate_tool(
            self.project,
            self.agent_id,
            tool_name,
            tool_args or {},
            cwd=cwd or None,
            base=self.base,
            token=self.token,
        )
        verdict.update({"boundary": "tool", "tool_name": tool_name, "agent_id": self.agent_id})
        if verdict.get("decision") == "deny" and raise_on_deny:
            raise SwitchboardInterrupt(verdict)
        return verdict

    def report_progress(self, task_id: str = "", node_name: str = "", status: str = "running",
                        detail: Any = None) -> Optional[Dict[str, Any]]:
        task_id = task_id or self.current_task_id
        if not task_id:
            return None
        text = (f"LangGraph {self.graph_name}:{node_name or 'graph'} {status} "
                f"(agent {self.agent_id}).")
        if detail not in (None, ""):
            text += f" Detail: {json.dumps(_brief(detail), sort_keys=True)}"
        try:
            return sb._http("POST", f"/api/tasks/{urllib.parse.quote(task_id, safe='')}/comment"
                            f"?project={urllib.parse.quote(self.project, safe='')}",
                            {"actor": self.agent_id, "text": text},
                            base=self.base, token=self.token)
        except Exception:
            return None

    def wrap_node(self, node_name: str, fn: Optional[Callable[..., Any]] = None):
        """Decorator/wrapper for a LangGraph node callable."""
        def decorate(func: Callable[..., Any]):
            if inspect.iscoroutinefunction(func):
                @functools.wraps(func)
                async def async_wrapped(state, *args, **kwargs):
                    self.guard_node(node_name, state)
                    self.report_progress(node_name=node_name, status="started")
                    result = await func(state, *args, **kwargs)
                    self.report_progress(node_name=node_name, status="completed",
                                         detail={"result": _brief(result)})
                    return result
                return async_wrapped

            @functools.wraps(func)
            def wrapped(state, *args, **kwargs):
                self.guard_node(node_name, state)
                self.report_progress(node_name=node_name, status="started")
                result = func(state, *args, **kwargs)
                self.report_progress(node_name=node_name, status="completed",
                                     detail={"result": _brief(result)})
                return result
            return wrapped

        return decorate(fn) if fn is not None else decorate

    def claim_next(self, lanes: Any = None) -> Dict[str, Any]:
        res = sb.claim_next(self.project, self.agent_id, lanes=lanes or self.lane,
                            base=self.base, token=self.token)
        if res.get("claimed"):
            self.current_claim_id = res.get("claim_id") or ""
            task = res.get("task") or {}
            self.current_task_id = task.get("task_id") or res.get("task_id") or ""
        return res

    def complete_claim(self, evidence: Dict[str, Any], final_status: str = "") -> Dict[str, Any]:
        return sb.complete_claim(self.project, self.current_claim_id, evidence,
                                 base=self.base, token=self.token, final_status=final_status)

    def abandon_claim(self, reason: str) -> Optional[Dict[str, Any]]:
        return sb.abandon_claim(self.project, self.current_claim_id, reason,
                                base=self.base, token=self.token)

    def run_claim_loop(self, graph: Any, lanes: Any = None, max_tasks: int = 1) -> Dict[str, Any]:
        def work_fn(claim: Dict[str, Any]) -> Dict[str, Any]:
            task = claim.get("task") or {}
            self.current_claim_id = claim.get("claim_id") or ""
            self.current_task_id = claim.get("task_id") or task.get("task_id") or ""
            self.report_progress(node_name="graph", status="claimed",
                                 detail={"task_id": self.current_task_id})
            result = invoke_graph(graph, {"task": task, "switchboard": {
                "agent_id": self.agent_id,
                "claim_id": self.current_claim_id,
                "task_id": self.current_task_id,
            }})
            evidence = evidence_from_result(result)
            evidence.update({
                "runtime": RUNTIME,
                "graph_name": self.graph_name,
                "run_id": self.run_id,
            })
            return evidence

        return sb.run_session(self.project, self.agent_id, RUNTIME, work_fn,
                              lanes=lanes or self.lane, base=self.base, token=self.token,
                              max_tasks=max_tasks, register=True)


def invoke_graph(graph: Any, state: Dict[str, Any]) -> Any:
    """Invoke a LangGraph compiled graph or a plain callable."""
    if hasattr(graph, "invoke"):
        return graph.invoke(state)
    if callable(graph):
        return graph(state)
    raise TypeError("graph must be callable or expose invoke(state)")


def evidence_from_result(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        evidence = result.get("switchboard_evidence") or result.get("evidence")
        if isinstance(evidence, dict):
            return dict(evidence)
        return {"result": _brief(result)}
    return {"result": _brief(result)}


def _read_json(value: str) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {"raw": value}


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def run_conformance(json_only: bool = False) -> int:
    from conformance import LocalStoreClient, print_result, run_p0_conformance

    with LocalStoreClient(adapter="langgraph", runtime=RUNTIME) as client:
        result = run_p0_conformance(client, control_mode="hook_deny")
        print_result(result, json_only=json_only)
        return 0 if result.ok else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Switchboard LangGraph adapter")
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--graph-name", default=os.environ.get("PM_LANGGRAPH_GRAPH", "graph"))
    parser.add_argument("--run-id", default=os.environ.get("PM_LANGGRAPH_RUN_ID", ""))
    parser.add_argument("--agent-id", default=os.environ.get("PM_AGENT_ID", ""))
    parser.add_argument("--lane", default=os.environ.get("PM_LANE", ""))
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("session-start", help="register graph run and print context JSON")
    guard = sub.add_parser("guard-node", help="check one graph node boundary")
    guard.add_argument("node_name")
    guard.add_argument("--state-json", default="")
    tool = sub.add_parser("guard-tool", help="check one tool boundary")
    tool.add_argument("tool_name")
    tool.add_argument("--args-json", default="")
    sub.add_parser("fidelity", help="print advertised control fidelity")
    conf = sub.add_parser("conformance", help="run local P0 conformance as langgraph")
    conf.add_argument("--json", action="store_true")
    smoke = sub.add_parser("smoke", help="run wrapper smoke without requiring LangGraph")
    smoke.add_argument("--skip-session", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "conformance":
        return run_conformance(json_only=args.json)

    adapter = LangGraphSwitchboardAdapter(project=args.project, graph_name=args.graph_name,
                                          run_id=args.run_id, agent_id=args.agent_id,
                                          lane=args.lane)
    if args.command == "session-start":
        _print(adapter.on_graph_start())
        return 0
    if args.command == "guard-node":
        _print(adapter.guard_node(args.node_name, _read_json(args.state_json),
                                  raise_on_deny=False))
        return 0
    if args.command == "guard-tool":
        _print(adapter.guard_tool(args.tool_name, _read_json(args.args_json),
                                  raise_on_deny=False))
        return 0
    if args.command == "fidelity":
        _print(adapter.control)
        return 0
    if args.command == "smoke":
        if not args.skip_session:
            _print(adapter.on_graph_start())

        @adapter.wrap_node("example")
        def example_node(state):
            return {**state, "ok": True}

        _print({"node_result": example_node({"task_id": "ADAPTER-4", "input": "smoke"})})
        _print(adapter.guard_tool("mcp__taikun_plan__update_task", {"status": "Done"},
                                  raise_on_deny=False))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
