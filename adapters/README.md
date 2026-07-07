# Switchboard Runtime Adapters

Adapter packs make the Switchboard lifecycle automatic inside each runtime: handshake,
presence, inbox, leases, dispatch, completion evidence, Tally, reconcile, and truthful control
fidelity.

## P0 Conformance

Run the shared P0 smoke against an isolated throwaway board:

```bash
python3 adapters/conformance.py
```

The command prints pass/fail checks plus a capability statement:

```bash
python3 adapters/conformance.py --json
```

Current reference transport:

- `local-store`: direct `store.py` calls against temporary SQLite files.

Runtime-specific packs should reuse `run_p0_conformance(...)` with their own REST, MCP, or SDK
client instead of inventing new smoke semantics per adapter.

Badge language and public-package boundaries live in
[`docs/IXP-CONFORMANCE.md`](../docs/IXP-CONFORMANCE.md) and
[`docs/IXP-PUBLIC-PACKAGE.md`](../docs/IXP-PUBLIC-PACKAGE.md). Do not call an adapter
`IXP-core conformant` unless it publishes the fixture command, protocol/profile, verification
date, and known deviations.

## Pre-tool Work Session Gate

Managed/hook-capable adapters should call the shared server-side `pre_tool_check` before file
writes, git/PR commands, `complete_claim`, merge, server start/kill, or other external effects.
Set `PM_PRE_TOOL_CHECK=1` and pass `PM_WORK_SESSION_ID`, `PM_TASK_ID`, and `PM_CLAIM_ID` when a
runner has those values. The server returns `allow`/`warn`/`deny`; hook-capable runtimes must
block on `deny`, while advisory runtimes must surface the remediation and advertise reduced
control fidelity. Denied unsafe attempts are audited as `principal.unbound_write` or
`work_session.unsafe_session`.

## Runtime Packs

- `claude-code/` - Claude Code session start and pre-tool guard.
- `codex/` - Codex adapter harness and managed supervisor proof.
- `cursor/` - Cursor adapter guidance.
- `openai-loop/` - raw tool-calling loop integration points.
- `langgraph/` - LangGraph node/tool wrappers plus claim-loop helper.
