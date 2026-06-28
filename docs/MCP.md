# Taikun Plan — MCP server

plan.taikunai.com is an **MCP server** (Streamable HTTP). Connect from Claude Code,
Claude Desktop, Cursor, or any MCP client and drive the plan without opening the board.

**Endpoint:** `https://plan.taikunai.com/mcp`

## Tools

Every task/board tool accepts `project`. Use `maxwell` for the TEEP Barnett plan, `helm` for
the marine chartplotter board, and `switchboard` for the live dogfood board that coordinates this
agent-collaboration product itself.

Reads (open):
- `search_tasks(workstream?, status?, owner_person?, blocking?, query?)` — filter the live plan.
- `get_task(task_id)` — full detail (description, fields, recent activity).
- `board_summary()` — project + rollups + one line per task.
- `get_working_agreement(project)` — connect-time rules: definition of done, branch convention,
  merge strategy, canonical main SHA, and session-start sequence.
- `doc_search(query)` — cited snippets from the plan docs.
- `ask_plan(question)` — the **full plan-wide agent** as one tool: a reasoned, doc-grounded
  answer (with sources), and a proposed task change when relevant (NOT applied).

Writes (authenticated when `PM_AUTH_MODE=required`; audited as the authenticated actor):
- `create_task(workstream_id, title, ...)`
- `update_task(task_id, ...only the fields you pass...)`
- `add_comment(task_id, text)`
- `register_agent(...)`, `heartbeat(...)`, `list_active_agents(...)`
- `claim_resource(...)`, `release_resource(...)`, `send_agent_message(...)`, `ack_message(...)`
- `list_pending_acks(project, agent_id?)`, `list_monitors(project, status?, kind?)`,
  `sweep_monitors(project)`, `resolve_monitor(...)`, `cancel_monitor(...)`
- `claim_next(...)`, `complete_claim(...)`, `abandon_claim(...)`
- `report_usage(...)`, `get_task_tally(...)`
- `reconcile(project)` — provenance drift report; always flags board contradictions like
  `Done` without `merged_sha`, and when canonical main / GitHub config is available, checks
  recorded SHAs and PR state against git/GitHub.

Agent completion rule:
- `complete_claim(evidence)` moves the task to `In Review` and records branch/SHA/PR evidence.
- Agents must not self-set `Done`; GitHub PR merge webhook stamps `merged_sha` and moves the
  task to `Done`.
- Bootstrap repair: `jobs.py backfill_default_branch_provenance` can stamp legacy direct-to-default
  commits that already landed before PR-only flow was enforced. It is a system/reconcile action,
  not a normal agent completion path. Use `PM_BACKFILL_DRY_RUN=1` first to inspect candidates.

Durable ack rule:
- `send_agent_message(... requires_ack=true ...)` creates a durable `ack_deadline` monitor.
- `list_pending_acks` and `get_message_status` expose monitor state; `sweep_monitors` resolves
  acked messages and fires timed-out monitors. The production host should run
  `jobs.py sweep_monitors` through `projectplanner-monitors.timer`.

Runner kill rule:
- Runner kill is outside `IXP-core`. Only Switchboard-managed sessions with a
  `runner_session_id` may advertise `runner_kill=true`.
- Kill requests target the runner session, snapshot state first, write `runner.*` audit events,
  and do not silently mark work complete. See `docs/INTERRUPT-TIERS-SPEC.md`.

## Connect

**Claude Code (CLI):**
```bash
claude mcp add --transport http taikun-plan https://plan.taikunai.com/mcp
```

**Claude Desktop / Cursor (JSON config):**
```json
{
  "mcpServers": {
    "taikun-plan": { "url": "https://plan.taikunai.com/mcp" }
  }
}
```

If writes are token-gated (`PM_AUTH_MODE=required`), add a bearer token header. Existing
deployments can keep using `PM_MCP_TOKEN`; new deployments should create per-agent
principals or set `PM_AUTH_TOKEN` during bootstrap:
```json
{
  "mcpServers": {
    "taikun-plan": {
      "url": "https://plan.taikunai.com/mcp",
      "headers": { "Authorization": "Bearer <SWITCHBOARD_TOKEN>" }
    }
  }
}
```

## Ops

- Runs as its own process: `projectplanner-mcp.service` (uvicorn, `127.0.0.1:8111`).
  Caddy routes `/mcp*` → `:8111`, everything else → the web app (`:8110`).
- The coordination monitor sweep is host-owned: enable `projectplanner-monitors.timer` so
  `requires_ack` messages can time out and notify senders even if no Codex thread is awake.
- Shares the SQLite file (WAL) with the web app; reuses `store`/`rag`/`agent` in-process.
- Auth: reads may remain open; writes are bearer-authenticated when `PM_AUTH_MODE=required`.
  `PM_MCP_TOKEN` and `PM_AUTH_TOKEN` map to compatibility system principals until explicit
  per-agent principals are created.
- `PM_MCP_PUBLIC_HOST` (default `plan.taikunai.com`) is trusted by MCP's DNS-rebinding guard —
  set it if the public host changes, or you'll get HTTP 421.
