# Switchboard — Cursor adapter (ADAPTER-3)

Cursor is **MCP-native** (it can load the IXP tools directly) but exposes **no deterministic
pre-tool hook** we can use to *block* a call the way Claude Code's `PreToolUse` does. So the
Cursor adapter is **Tier-1 advisory + MCP**, over the same [`../switchboard_core.py`](../switchboard_core.py)
contract as every other runtime.

## How it works
1. **MCP** — point Cursor at `https://plan.taikunai.com/mcp` (same server). The IXP/board tools
   are then available in-session.
2. **A Cursor Rule** carries the session-start handshake + the hard rule, so the model runs it.
   This repo ships it at [`.cursor/rules/switchboard.mdc`](../../.cursor/rules/switchboard.mdc)
   (always-applied). It instructs: at session start call `prepare_agent_session` →
   `get_working_agreement` → `register_agent` → drain inbox; **never set a task to `Done`**
   (move only to `In Review`); claim files before editing. See [`.cursor/README.md`](../../.cursor/README.md)
   for local vs Cloud Agent MCP setup.
3. **Enforcement** — for a *managed* Cursor runner that can shell out before a tool, call the
   raw-loop guard (`adapters/openai-loop/openai_loop_adapter.guard_tool`) — identical core,
   identical verdicts. Without that, Cursor is advisory only.

## Fidelity (honest — PRD §10)
**Tier-1** (advisory via MCP `instructions` + the rule). **T2** only if a managed Cursor runner
invokes the shared-core `guard_tool` before each tool and honors deny. NMI = runner kill if
Switchboard owns the process. Set the `control` block accordingly at `register_agent`.

## Config
`PM_BASE`, `PM_PROJECT`, `PM_MCP_TOKEN`, `PM_AGENT_ID` (`cursor/<scope>`).

> Same `<runtime>/<scope>` id convention as the rest (IXP §2). One core, four runtimes
> (Claude Code, Codex, raw OpenAI loop, Cursor) — all on the identical contract.
