# Switchboard Runtime Adapters

Adapter packs make the Switchboard lifecycle automatic inside each runtime: handshake,
presence, inbox, leases, dispatch, completion evidence, Tally, reconcile, and truthful control
fidelity.

## Install in one command

Install a self-contained runtime bundle into an existing project (Python 3.10+; no package
manager required):

```bash
python3 adapters/marketplace.py install claude-code --target /path/to/project
python3 adapters/marketplace.py install codex --target /path/to/project
python3 adapters/marketplace.py install cursor --target /path/to/project
```

The installer preserves unrelated Claude/Cursor configuration, writes the runtime assets below
`.switchboard/adapters/`, and creates `.switchboard/adapter.env.example`. Copy that example into
your secret-aware environment and set a scoped `PM_MCP_TOKEN`; never commit the real token.

Available bundles also include `openai-loop`, `langgraph`, and `agent-host`:

```bash
python3 adapters/marketplace.py list
python3 adapters/marketplace.py install langgraph --target .
```

The `agent-host` selection installs its config/manifest adoption profile only. Production Agent
Host binaries must come from the server-issued signed enrollment bundle; the marketplace command
does not bypass host verification or manufacture bootstrap credentials.

Each install writes a machine-readable `.switchboard/<runtime>.json` manifest containing its
honest control-fidelity profile. Claude Code is T2 when hooks are honored; Cursor and unwrapped
Codex/raw loops are T1; LangGraph is T2 only when every relevant boundary is wrapped; the local
Agent Host is T3 because it owns the managed process and runner-kill path.

Verify any pack with the shared isolated conformance fixture:

```bash
python3 adapters/marketplace.py smoke claude-code
python3 adapters/marketplace.py smoke codex
python3 adapters/marketplace.py smoke cursor
```

The smoke command does not need live credentials and does not mutate a real board. After it
passes, follow the selected runtime README for session start and any runtime-specific caveats.

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
