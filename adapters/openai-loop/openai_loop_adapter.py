#!/usr/bin/env python3
"""Raw OpenAI tool-calling loop adapter — Switchboard (ADR-0004 / ADAPTER-3).

The hardest case: a runtime with NO hook surface at all. A bare OpenAI (or any) tool-calling
loop can't be intercepted by the harness — so enforcement is *integrator-driven* (Tier-1):
the loop itself calls the shared core. Proves the same contract works with zero runtime hooks,
just two function calls dropped into the loop:

  • once at start:  ctx = on_session_start()    → prepend `ctx` to your system prompt
  • before EVERY tool dispatch:  v = guard_tool(name, args)
        v["decision"]=="deny"  → DON'T run the tool; feed v["reason"] back to the model as the
                                  tool result so it self-corrects / halts (FR-14 interrupt lands here too)
        v["decision"]=="allow" → run the tool (a non-empty reason is a soft reminder)

All coordination logic lives in adapters/switchboard_core.py — identical to the Claude/Codex
adapters. Fidelity is honestly Tier-1: there is no way to *force* the integrator to call
guard_tool; if they don't, the loop is ungoverned. A managed runner that wraps the process can
add the T3/NMI kill. Config via env: PM_BASE, PM_PROJECT, PM_MCP_TOKEN, PM_AGENT_ID.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import switchboard_core as sb  # noqa: E402

PROJECT = os.environ.get("PM_PROJECT", "switchboard")
RUNTIME = "openai-loop"


def agent_id():
    return os.environ.get("PM_AGENT_ID") or sb.agent_id()


def on_session_start():
    """Call once before the loop starts. Registers + returns the working-agreement text to
    prepend to the system prompt. T1 control: this runtime can advise but not self-enforce."""
    me = agent_id()
    agreement = sb.handshake(PROJECT, me, RUNTIME,
                             lane=os.environ.get("PM_LANE", ""),
                             control={"interrupt": "integrator_poll", "deny": "integrator_loop",
                                      "kill": "runner", "tier": "T1", "verified": False})
    text = agreement.get("text") if isinstance(agreement, dict) else None
    if not text and isinstance(agreement, dict):
        import json
        text = json.dumps(agreement, indent=2, sort_keys=True)
    return (f"## Switchboard working agreement — project '{PROJECT}' (registered {me})\n\n"
            f"{text or '(unavailable; fail-open)'}\n\n"
            f"You are coordinating with other agents. Before each tool call your harness checks "
            f"the board; a denied call returns the reason instead of running — halt or redirect.")


def guard_tool(tool_name, tool_input, cwd=None):
    """Call before dispatching each tool in your loop. Returns {decision, reason}. On 'deny',
    do NOT execute the tool — surface reason to the model as the tool's result."""
    return sb.evaluate_tool(PROJECT, agent_id(), tool_name, tool_input or {}, cwd=cwd)


# ---- reference loop (illustrative; swap in your real OpenAI client) ----------------------
def run_loop(client, messages, tools, dispatch):
    """Skeleton of a governed tool-calling loop. `dispatch(name, args)->str` runs a tool.
    Shows the TWO integration points; everything else is your normal loop."""
    messages = [{"role": "system", "content": on_session_start()}] + messages   # point 1
    while True:
        resp = client.chat.completions.create(model="gpt-5.5", messages=messages, tools=tools)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content
        messages.append(msg)
        for tc in msg.tool_calls:
            import json
            args = json.loads(tc.function.arguments or "{}")
            v = guard_tool(tc.function.name, args)                                # point 2
            if v["decision"] == "deny":
                result = f"[SWITCHBOARD DENIED] {v['reason']}"                    # don't run it
            else:
                result = dispatch(tc.function.name, args)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})


if __name__ == "__main__":
    # Smoke: prove the two calls work against the live board (no OpenAI client needed).
    import json
    print(on_session_start()[:160])
    print(json.dumps(guard_tool("mcp__taikun-plan__update_task", {"status": "Done"})))
    print(json.dumps(guard_tool("search_tasks", {"project": PROJECT})))
