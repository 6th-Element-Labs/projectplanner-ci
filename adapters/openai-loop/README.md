# Switchboard — raw OpenAI-loop adapter (ADAPTER-3)

The zero-hook case: a bare tool-calling loop (OpenAI or any provider) with **no harness to
intercept tool calls**. Enforcement is therefore **integrator-driven (Tier-1)** — the loop
itself calls the shared core. Same contract as the Claude/Codex adapters; all logic lives in
[`../switchboard_core.py`](../switchboard_core.py).

## Two integration points (that's the whole adapter)
```python
import openai_loop_adapter as sw
messages = [{"role": "system", "content": sw.on_session_start()}] + messages   # 1. at start
...
verdict = sw.guard_tool(call.name, call.args)                                  # 2. before each tool
if verdict["decision"] == "deny":
    result = f"[denied] {verdict['reason']}"   # don't run it; feed reason back to the model
else:
    result = run_the_tool(call.name, call.args)
```
`run_loop()` in the module is a full illustrative loop showing both.

## Fidelity (honest — PRD §10)
**Tier-1 only.** There is no way to *force* an integrator to call `guard_tool` — if they skip
it, the loop is ungoverned. So this adapter:
- ✅ discovers/registers/polls, surfaces the working agreement, and *can* deny/halt **if** the
  integrator wires `guard_tool` (including FR-14 stop/redirect — it lands as a deny on the next
  tool the loop checks);
- ❌ cannot self-enforce against a non-cooperating loop.
A **managed runner** that owns the process adds the T3/NMI kill on top.

## Config
`PM_BASE` (default `https://plan.taikunai.com`), `PM_PROJECT`, `PM_MCP_TOKEN`,
`PM_AGENT_ID` (IXP `<runtime>/<scope>`, e.g. `openai-loop/REVIEW-1`).

## Smoke
```bash
PM_PROJECT=switchboard PM_AGENT_ID=openai-loop/smoke python3 adapters/openai-loop/openai_loop_adapter.py
# → prints the agreement + a deny verdict (naked Done) + an allow verdict
```
