# Switchboard - LangGraph adapter (ADAPTER-4)

LangGraph owns in-process graph state and node execution. Switchboard owns cross-agent
coordination: handshake, presence, inbox, interrupts, `claim_next`, progress, completion
evidence, Tally, and reconcile.

This pack has no hard LangGraph dependency. Import it into a LangGraph app and wrap the nodes or
tools you want governed.

## Node wrapper

```python
from adapters.langgraph.langgraph_adapter import LangGraphSwitchboardAdapter

sw = LangGraphSwitchboardAdapter(graph_name="triage", lane="ADAPTER")
context = sw.on_graph_start()

@sw.wrap_node("plan")
def plan_node(state):
    ...
    return state
```

At each wrapped node boundary the adapter heartbeats, polls directed Switchboard messages, and
raises `SwitchboardInterrupt` on a `stop`, `redirect`, or `claim_revoked` signal before the node
runs.

## Tool boundary

```python
verdict = sw.guard_tool("mcp__taikun_plan__update_task", {"status": "Done"})
```

The shared core denies naked `Done` updates and file-lease conflicts exactly like the Claude,
Codex, Cursor, and raw-loop packs.

## Claim loop

```python
summary = sw.run_claim_loop(compiled_graph, lanes="ADAPTER", max_tasks=1)
```

`run_claim_loop` uses the runtime-agnostic Switchboard loop:

```text
handshake -> heartbeat -> claim_next -> graph.invoke(state) -> complete_claim(evidence)
```

The graph receives:

```python
{
  "task": task_dict,
  "switchboard": {
    "agent_id": "...",
    "claim_id": "...",
    "task_id": "..."
  }
}
```

If the graph returns `{"switchboard_evidence": {...}}` or `{"evidence": {...}}`, that object is
used as completion evidence. Otherwise the result is summarized.

## Fidelity

Default fidelity is `hook_deny` / Tier 2 because the adapter wrapper can stop a node/tool before
it executes. Set `PM_LANGGRAPH_CONTROL_MODE=advisory_poll` if the graph run is only reading
Switchboard but not wrapping every relevant boundary. Set `PM_RUNNER_SESSION_ID` when a
Switchboard-managed supervisor owns the process and can provide runner kill.

## Config

- `PM_BASE` - Switchboard base URL, default `https://plan.taikunai.com`
- `PM_PROJECT` - board id, default `switchboard`
- `PM_MCP_TOKEN` - bearer token for authenticated writes
- `PM_AGENT_ID` - stable IXP id, for example `langgraph/triage-run-42`
- `PM_LANGGRAPH_GRAPH` / `PM_LANGGRAPH_RUN_ID` - used to derive an agent id
- `PM_LANE` - optional lane for `claim_next`

## Smoke

```bash
python3 adapters/langgraph/langgraph_adapter.py smoke --skip-session
python3 adapters/langgraph/langgraph_adapter.py conformance --json
```
