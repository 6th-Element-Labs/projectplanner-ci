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
| Claim ready scheduler work | ✅ `codex_adapter.py claim-next` and `session-start --claim-next` call `/txp/v1/claim_next` |
| Report completion evidence | ✅ `codex_adapter.py complete <claim_id>` calls `/txp/v1/complete_claim` with git evidence |
| Prove managed-runner deny enforcement | ✅ `runner_smoke.py --offline --deny-exit-code 0` blocks a self-Done call before execution |
| Own a process handle for runner kill | ✅ `supervisor.py start/status/kill` persists `runner_session_id`, log path, and kill snapshot |
| Prove native Codex hook lifecycle blocks tools | 🔲 not proven in this repo; keep Codex native hook status TBD until a real Codex launcher integrates this shim |

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

This repo currently proves managed-runner enforcement, not a native Codex product hook. Until a
Codex launcher invokes `pre-tool` before every call and honors `decision=deny`, leave native
Codex hook fidelity marked TBD/T1.

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

To start and immediately ask the scheduler for work:

```bash
PM_PROJECT=switchboard PM_AGENT_ID=codex/current \
  python3 adapters/codex/codex_adapter.py session-start --claim-next --lanes ADAPTER
```

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

## Scheduler lifecycle
```bash
PM_PROJECT=switchboard PM_AGENT_ID=codex/current \
  python3 adapters/codex/codex_adapter.py claim-next --lanes ADAPTER --capabilities python,docs

PM_PROJECT=switchboard PM_AGENT_ID=codex/current \
  python3 adapters/codex/codex_adapter.py complete taskclaim-abc123 --pr-url https://github.com/org/repo/pull/1

PM_PROJECT=switchboard PM_AGENT_ID=codex/current \
  python3 adapters/codex/codex_adapter.py abandon taskclaim-abc123 --reason "blocked on credentials"
```

`complete` records the current git branch and `HEAD` SHA by default, plus any PR fields supplied.
It moves the task to `In Review`; the GitHub webhook remains the only writer of `Done`.

## Managed runner smoke
```bash
PM_PROJECT=switchboard PM_AGENT_ID=codex/current \
  python3 adapters/codex/runner_smoke.py --offline --deny-exit-code 0
```

The default candidate is an attempted `update_task(status="Done")`. A passing smoke returns
`runner_action=blocked_before_execution` and `would_execute=false`. That proves a
Switchboard-owned runner can honor the adapter's deny verdict; it deliberately reports
`native_codex_hook_proven=false`.

## Managed process supervisor
```bash
PM_RUNNER_DIR=.switchboard/runner \
  python3 adapters/codex/supervisor.py start \
    --agent-id codex/current --task-id ADAPTER-8 -- \
    python3 adapters/codex/codex_adapter.py session-start --claim-next --lanes ADAPTER

python3 adapters/codex/supervisor.py status run_...
python3 adapters/codex/supervisor.py kill run_... --grace-seconds 5
```

The supervisor injects `PM_RUNNER_SESSION_ID` and `PM_AGENT_ID`, stores
`session.json` plus `stdout.log`, and snapshots cwd, branch, `HEAD`, task/claim ids, and log tail
before kill. This is the concrete process handle behind the `runner_kill` tier; it does not yet
claim native Codex hook support or implement a hosted `/runner/v1/*` API wrapper.
