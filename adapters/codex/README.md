# Switchboard — Codex adapter (ADAPTER-2)

Tier-2 adapter for the **Codex** runtime (ADR-0004). The coordination logic is **already
built and proven** in [`../switchboard_core.py`](../switchboard_core.py) — this directory is
the thin Codex-specific wiring. Authored as a scaffold by `claude-code` per decision #2
(claude owns the adapter core; **Codex fills the two runtime hooks below**).

## What's done vs. what Codex fills
| Piece | State |
|---|---|
| Handshake (working_agreement → register → inbox) | ✅ in `switchboard_core.handshake()` |
| Enforce: FR-14 interrupt-consume, self-Done deny, lease-conflict deny | ✅ in `switchboard_core.evaluate_tool()` (live-verified via the Claude adapter) |
| Wire handshake to a Codex session/launcher | ✅ `codex_adapter.py session-start` prints first-turn context JSON, registers, and drains unacked inbox |
| Wire `evaluate_tool` to a Codex pre-tool hook | ✅ `codex_adapter.py pre-tool` accepts pending tool JSON and prints allow/deny verdicts |
| Prove native Codex hook lifecycle blocks tools | 🔲 still TBD; set `PM_CODEX_PRETOOL_MODE=deny` only when a runner actually honors denials |

The Codex-specific surface is now a stable JSON stdin/stdout shim. A native Codex hook, wrapper,
or launcher can call it without reimplementing Switchboard logic. The remaining unknown is only
whether a given Codex runtime can invoke this shim before every tool call and honor a deny.

## The adapter contract (ADR-0004 — same for every runtime)
1. **Session start:** surface the working agreement as first-turn context + `register_agent`.
2. **Per tool call:** call `evaluate_tool(...)`; on `deny` block the tool and surface the reason
   so the model self-corrects/halts; on `allow` permit (a non-empty reason is a soft reminder).
3. **Advertise fidelity:** `handshake(..., control={...})` tells the board how strongly this
   runtime is governed (discover / pre-tool-deny / runner-kill).

## Fidelity (be honest — PRD §10)
Codex's per-tool-call interrupt fidelity is **runtime-dependent**. The adapter defaults to
truthful **T1 advisory** (`deny=not_verified`, `interrupt=advisory_poll`) because this repo
cannot prove every Codex surface exposes a blocking pre-tool hook. If a launcher does invoke
`pre-tool` before every call and honors `decision=deny`, set:

```bash
export PM_CODEX_PRETOOL_MODE=deny
```

Then `session-start` advertises **T2** (`deny=adapter_cli_pre_tool`,
`interrupt=tool_boundary`). Runner kill remains the T3/NMI path when Switchboard owns the
process.

## Config
`PM_BASE` (default `https://plan.taikunai.com`), `PM_PROJECT` (`switchboard`), `PM_MCP_TOKEN`,
`PM_AGENT_ID` (use the IXP `<runtime>/<scope>` convention, e.g. `codex/ADAPTER-2`),
`PM_AGENT_MODEL`, `PM_LANE`, and `PM_CODEX_PRETOOL_MODE`.

> Note (agent_id drift, found live): the Claude adapter currently registers as `claude-code`
> and Codex as `codex/current`; IXP §2 wants `<runtime>/<scope>`. Align both when convenient —
> mismatched ids are why an early cross-agent IM missed its inbox.

## Smoke
```bash
PM_PROJECT=switchboard python3 adapters/codex/codex_adapter.py smoke --skip-session
```

## Session start
```bash
PM_PROJECT=switchboard PM_AGENT_ID=codex/ADAPTER-2 \
  python3 adapters/codex/codex_adapter.py session-start
```

The output includes `additional_context` plus `unacked_messages`. A Codex launcher should place
that into first-turn context before model work begins. The adapter reads the inbox but does not
auto-ack; the agent should ack after it understands or acts on the message.

## Pre-tool verdict
```bash
printf '%s\n' '{"toolCall":{"name":"mcp__taikun_plan__update_task","arguments":{"status":"Done"}}}' \
  | PM_PROJECT=switchboard PM_AGENT_ID=codex/ADAPTER-2 \
    python3 adapters/codex/codex_adapter.py pre-tool
```

The output is neutral JSON:

```json
{
  "decision": "deny",
  "reason": "Working agreement ...",
  "tool_name": "mcp__taikun_plan__update_task"
}
```
