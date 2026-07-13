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
   identical verdicts. Set `PM_PRE_TOOL_CHECK=1` and pass `PM_WORK_SESSION_ID`/`PM_TASK_ID` when
   the runner has an active Work Session, so Switchboard validates side effects server-side.
   Without a managed boundary that honors `deny`, Cursor is advisory only.

## Fidelity (honest — PRD §10)
**Tier-1** (advisory via MCP `instructions` + the rule). **T2** only if a managed Cursor runner
invokes the shared-core `guard_tool` before each tool and honors deny. NMI = runner kill if
Switchboard owns the process. Set the `control` block accordingly at `register_agent`.

## Config
`PM_BASE`, `PM_PROJECT`, `PM_MCP_TOKEN`, `PM_AGENT_ID` (`cursor/<scope>`).

> Same `<runtime>/<scope>` id convention as the rest (IXP §2). One core, four runtimes
> (Claude Code, Codex, raw OpenAI loop, Cursor) — all on the identical contract.

## Cursor Cloud Agents (ADAPTER-20)

[`cloud_execution.py`](cloud_execution.py) implements the vendor-hosted Cursor path against the
current [Cloud Agents v1 API](https://cursor.com/docs/cloud-agent/api/endpoints). It:

1. validates the shared `switchboard.cloud_dispatch.v1` envelope;
2. verifies the Cloud Agents API key, canonical GitHub repository grant, scoped MCP token
   resolution, pushed task branch, and Switchboard's lower concurrency cap;
3. creates a deterministic `bc-...` agent with `POST /v1/agents`, the canonical repo/task branch,
   `workOnCurrentBranch=true`, `autoCreatePR=true`, and inline `taikun-plan` MCP access;
4. reads back the durable agent ID, app-visible `cursor.com/agents/...` URL, and latest run before
   returning a `switchboard.cloud_session_binding.v1` receipt; and
5. projects `/usage` token counts into a reported Tally receipt without inventing dollar cost.

The launch secret boundary is deliberate: `CURSOR_API_KEY` and the resolved short-lived MCP token
are constructor/host inputs, never dispatch-envelope fields or receipt fields. The adapter accepts
a token resolver so production can map the opaque `token_ref` to its secret store without logging
the credential.

Continuity is honest and typed: exact same-run resume is unsupported and fails closed. Cursor v1
does support `POST /v1/agents/{id}/runs`, which creates a **same-agent follow-up** with retained
conversation/workspace state; the adapter never labels that operation an exact resume.

Hermetic coverage lives in [`../../test_cursor_cloud_execution.py`](../../test_cursor_cloud_execution.py).
A real launch additionally requires an operator-owned Cursor Cloud Agents API key, the canonical
repository grant, and a pushed `cursor/<task>` or `codex/<task>` branch. Cloud Agent model usage is
billed at selected-model API pricing; provider dollar cost remains unknown until reconciliation.
