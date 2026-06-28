# Switchboard — Claude Code adapter (reference implementation)

The **Tier-2 adapter** from [ADR-0004](../../docs/decisions/0004-adoption-and-enforcement.md):
two deterministic Claude Code hooks that make the IXP session-start handshake a *guarantee*
rather than a suggestion. The harness runs these — not the model — so adoption doesn't depend
on the agent remembering anything.

Both hooks are **thin shims over [`../switchboard_core.py`](../switchboard_core.py)** — the
same handshake + `evaluate_tool` the Codex adapter uses. The runtime-specific scripts only map
Claude Code's hook I/O to/from the shared core, so Claude and Codex provably run *one*
contract (no per-runtime drift — the class of bug that bit ADAPTER-1 on `/ixp` vs `/ixp/v1`).

## What it does

| Hook | File | Effect |
|---|---|---|
| `SessionStart` | `session_start.py` | Fetches the live working agreement (`get_working_agreement`, falls back to bundled [`docs/WORKING-AGREEMENT.md`](../../docs/WORKING-AGREEMENT.md)), **injects it as first-turn context**, and best-effort `register_agent`s the session. The agent starts in-contract. |
| `PreToolUse` | `pretooluse_guard.py` | **Denies** an agent setting a task to `Done` (only the merge webhook may) — covers the MCP `update_task` tool and the Bash/curl back-channel. **Warns** on a file edit to remind "claim + push first" (soft; see ADR-0004). |

This is the physical form of the ADR-0003 inversion: an agent literally *can't* self-declare
`Done` — the boundary refuses it and tells it to use `In Review` instead.

## Install

Merge `settings.json` into your project `.claude/settings.json` (or user-level settings).
The hook commands use `$CLAUDE_PROJECT_DIR`, which Claude Code sets at hook time, so they work
from any cwd as long as this repo is the project dir. The scripts need only `python3` (stdlib).

```bash
# from the repo root, one way to merge (jq):
jq -s '.[0] * .[1]' .claude/settings.json adapters/claude-code/settings.json > /tmp/m \
  && mv /tmp/m .claude/settings.json
```

## Configure (env)

| Var | Default | Meaning |
|---|---|---|
| `PM_BASE` | `https://plan.taikunai.com` | board base URL |
| `PM_PROJECT` | `helm` | which board this agent works |
| `PM_MCP_TOKEN` | — | bearer token (once `PM_AUTH_MODE=required`) |

## Fidelity & limits (be honest — ADR-0004 / PRD §10)

- **Nothing reaches the model mid-token.** Denials land at the next tool-call boundary
  (seconds) — enough to stop a bad write, not to freeze a running thought.
- **Fail-open by design:** if the board is unreachable, `SessionStart` still injects the
  bundled agreement and the session proceeds — the adapter never bricks an agent.
- **`write-before-claim` is a warning, not a deny** (a hard check is a board round-trip per
  file). Promote to deny later if latency is acceptable.
- **Non-MCP / non-hookable runtimes** (raw API loops) get **Tier 1 only** (the MCP
  `instructions` advisory) — there's no hook surface to enforce on. Publish that, don't pretend.
- **Tier 3** (board-launched agents) installs this bundle + registers *before* handoff via
  `dispatch.py` — that wiring is the next slice.

## Test locally

```bash
# naked Done is denied:
echo '{"tool_name":"mcp__taikun-plan__update_task","tool_input":{"status":"Done"}}' \
  | python3 adapters/claude-code/pretooluse_guard.py        # -> permissionDecision: deny

# evidence-backed Done goes through complete_claim:
echo '{"tool_name":"mcp__taikun-plan__complete_claim","tool_input":{"claim_id":"taskclaim-123","final_status":"Done","evidence":"{\"done\":true,\"verification\":\"checks passed\"}"}}' \
  | python3 adapters/claude-code/pretooluse_guard.py        # -> permissionDecision: allow

# In Review is allowed:
echo '{"tool_name":"mcp__taikun-plan__update_task","tool_input":{"status":"In Review"}}' \
  | python3 adapters/claude-code/pretooluse_guard.py        # -> (no output = allow)

# session start injects the agreement:
echo '{"session_id":"abc","cwd":"'"$PWD"'"}' | python3 adapters/claude-code/session_start.py
```
