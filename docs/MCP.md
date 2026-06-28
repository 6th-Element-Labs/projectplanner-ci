# Taikun Plan — MCP server

plan.taikunai.com is an **MCP server** (Streamable HTTP). Connect from Claude Code,
Claude Desktop, Cursor, or any MCP client and drive the plan without opening the board.

**Endpoint:** `https://plan.taikunai.com/mcp`

## Tools

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
- `claim_next(...)`, `complete_claim(...)`, `abandon_claim(...)`
- `report_usage(...)`, `get_task_tally(...)`
- `reconcile(project)` — local provenance drift report; flags e.g. `Done` without `merged_sha`.

Agent completion rule:
- `complete_claim(evidence)` moves the task to `In Review` and records branch/SHA/PR evidence.
- Agents must not self-set `Done`; GitHub PR merge webhook stamps `merged_sha` and moves the
  task to `Done`.

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
- Shares the SQLite file (WAL) with the web app; reuses `store`/`rag`/`agent` in-process.
- Auth: reads may remain open; writes are bearer-authenticated when `PM_AUTH_MODE=required`.
  `PM_MCP_TOKEN` and `PM_AUTH_TOKEN` map to compatibility system principals until explicit
  per-agent principals are created.
- `PM_MCP_PUBLIC_HOST` (default `plan.taikunai.com`) is trusted by MCP's DNS-rebinding guard —
  set it if the public host changes, or you'll get HTTP 421.
